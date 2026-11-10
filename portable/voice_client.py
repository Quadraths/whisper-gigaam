#!/usr/bin/env python3
"""Портативный голосовой клиент whisper-gigaam (для удалённых машин).

Микрофон -> удалённый сервер транскрибации -> текст в активное окно.
Работает с обоими API сервера: родным /transcribe и OpenAI /v1/audio/transcriptions.

Зависимости всего две: sounddevice, pynput (см. requirements.txt).
Всё остальное — стандартная библиотека Python 3.10+.

Использование:
    python voice_client.py                       # голосовой ввод (Ctrl+Alt+Z)
    python voice_client.py --url http://192.168.130.155:8010
    python voice_client.py --list-devices        # список микрофонов
    python voice_client.py --device "USB"        # выбрать микрофон разово
    python voice_client.py --file rec.wav --print-only   # проверка без микрофона

Настройки (CLI > переменные окружения > файл client.env):
    --url / VOICE_URL      адрес сервера (по умолчанию http://127.0.0.1:8010)
    --api / VOICE_API      native | openai      (по умолчанию native)
    --device / VOICE_DEVICE   микрофон: фрагмент имени или индекс
    --hotkey / VOICE_HOTKEY   комбинация pynput (по умолчанию <ctrl>+<alt>+z)
    VOICE_PASTE          0 = только буфер обмена, без авто-вставки
    VOICE_SEND_ENTER     1 = нажимать Enter после вставки
"""
from __future__ import annotations

import argparse
import ctypes
import io
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import wave
from pathlib import Path

# Консоли на разных машинах используют разные кодировки (cp866/cp1251);
# errors="replace" гарантирует, что вывод никогда не уронит клиент.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")

CLIENT_DIR = Path(__file__).resolve().parent
SAMPLE_RATE = 16_000  # сервер принимает 16 кГц моно


# ---------- настройки: CLI > окружение > client.env ----------

def load_env_file(path: Path) -> None:
    """Читает KEY=VALUE из файла; уже заданные переменные не перезаписывает."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file(CLIENT_DIR / "client.env")

# ---------- Windows: буфер обмена и имитация клавиш ----------

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
user32.SetClipboardData.restype = ctypes.c_void_p
kernel32.GetLastError.restype = ctypes.c_uint

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK_CONTROL, VK_V, VK_RETURN = 0x11, 0x56, 0x0D


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_uint),
        ("time", ctypes.c_uint),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_uint),
        ("dwFlags", ctypes.c_uint),
        ("time", ctypes.c_uint),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", ctypes.c_uint), ("wParamL", ctypes.c_ushort), ("wParamH", ctypes.c_ushort)]


class _INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT), ("hi", _HARDWAREINPUT)]

    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_uint), ("u", _U)]


# cbSize в SendInput обязан равняться sizeof(INPUT) (40 байт на x64),
# иначе Windows молча отклоняет ВСЕ события.
user32.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(_INPUT), ctypes.c_int]
user32.SendInput.restype = ctypes.c_uint


def _send(*events: _INPUT) -> int:
    arr = (_INPUT * len(events))(*events)
    sent = user32.SendInput(len(events), arr, ctypes.sizeof(_INPUT))
    if sent != len(events):
        raise RuntimeError(f"SendInput отклонён (принято {sent}/{len(events)}, код {kernel32.GetLastError()})")
    return sent


def _key_event(vk: int, up: bool = False, unicode_char: str = "") -> _INPUT:
    if unicode_char:
        return _INPUT(
            type=INPUT_KEYBOARD,
            ki=_KEYBDINPUT(wVk=0, wScan=ord(unicode_char),
                           dwFlags=KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)),
        )
    return _INPUT(type=INPUT_KEYBOARD,
                  ki=_KEYBDINPUT(wVk=vk, dwFlags=KEYEVENTF_KEYUP if up else 0))


def foreground_window_title() -> str:
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return "<нет активного окна>"
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value or "<окно без заголовка>"


def set_clipboard(text: str) -> None:
    data = text.encode("utf-16-le") + b"\x00\x00"
    if not user32.OpenClipboard(None):
        raise RuntimeError("Не удалось открыть буфер обмена")
    try:
        user32.EmptyClipboard()
        h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h_mem:
            raise RuntimeError("Не удалось выделить память для буфера обмена")
        ptr = kernel32.GlobalLock(h_mem)
        ctypes.memmove(ptr, data, len(data))
        kernel32.GlobalUnlock(h_mem)
        if not user32.SetClipboardData(CF_UNICODETEXT, h_mem):
            raise RuntimeError("Не удалось записать буфер обмена")
    finally:
        user32.CloseClipboard()


def _tap(vk: int) -> None:
    _send(_key_event(vk), _key_event(vk, up=True))


def press_ctrl_v() -> None:
    """Ctrl+V по виртуальным кодам клавиш — не зависит от раскладки."""
    _send(_key_event(VK_CONTROL), _key_event(VK_V))
    _send(_key_event(VK_V, up=True), _key_event(VK_CONTROL, up=True))


def type_text(text: str) -> None:
    """Запасной способ вставки: прямой ввод юникод-символов."""
    events = []
    for ch in text:
        events.append(_key_event(0, unicode_char=ch))
        events.append(_key_event(0, up=True, unicode_char=ch))
    for i in range(0, len(events), 20):
        _send(*events[i:i + 20])


# ---------- выбор микрофона ----------

def _input_devices() -> list[tuple[int, str, str]]:
    import sounddevice as sd

    out = []
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            api = sd.query_hostapis(d["hostapi"])["name"]
            out.append((i, api, d["name"]))
    return out


def _device_name(index: int) -> str:
    import sounddevice as sd

    return sd.query_devices(index)["name"]


def _match_device(spec: str) -> int | None:
    devs = _input_devices()
    if spec.isdigit():
        idx = int(spec)
        return idx if any(i == idx for i, _, _ in devs) else None
    needle = spec.lower()
    for i, _api, name in devs:
        if needle in name.lower():
            return i
    return None


def _candidate_devices(spec: str) -> list[int]:
    import sounddevice as sd

    devs = _input_devices()
    ordered = [i for i, _, _ in devs]
    if spec:
        idx = _match_device(spec)
        if idx is None:
            raise RuntimeError(f"Микрофон '{spec}' не найден. См. --list-devices.")
        ordered.remove(idx)
        ordered.insert(0, idx)
    else:
        default = sd.default.device[0]
        if default is not None and default >= 0 and default in ordered:
            ordered.remove(default)
            ordered.insert(0, default)
    return ordered


def list_devices() -> None:
    import sounddevice as sd

    print("Входные устройства (укажите имя или индекс в VOICE_DEVICE / --device):\n")
    for i, api, name in _input_devices():
        d = sd.query_devices(i)
        print(f"  {i:>3}  [{api}] {name}  (частота {int(d['default_samplerate'])} Гц)")


# ---------- запись с микрофона ----------

class Recorder:
    def __init__(self) -> None:
        self._chunks: list[bytes] = []
        self._stream = None

    def start(self, device_spec: str) -> None:
        import sounddevice as sd

        self._chunks = []
        last_err = None
        for device in _candidate_devices(device_spec):
            try:
                self._stream = sd.InputStream(
                    samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                    device=device, callback=self._on_audio,
                )
                self._stream.start()
                print(f"Микрофон: [{device}] {_device_name(device)}")
                return
            except Exception as exc:  # noqa: BLE001
                last_err = exc
        raise RuntimeError(f"Не удалось открыть микрофон: {last_err}")

    def _on_audio(self, indata, frames, time_info, status) -> None:
        self._chunks.append(bytes(indata))

    def stop(self) -> bytes:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        return b"".join(self._chunks)


def to_wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)  # int16
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


# ---------- отправка на сервер (только стандартная библиотека) ----------

def transcribe(data: bytes, filename: str, content_type: str, base_url: str, api: str) -> tuple[str, list[dict], str]:
    """Отправляет аудио на сервер; возвращает (text, segments, note)."""
    boundary = "----whispergigaam" + uuid.uuid4().hex
    fields: list[bytes] = []

    def field(name: str, value: str) -> None:
        fields.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
        )

    if api == "openai":
        url = base_url.rstrip("/") + "/v1/audio/transcriptions"
        field("model", "whisper-1")
        field("response_format", "verbose_json")
        field("timestamp_granularities[]", "segment")
    else:
        url = base_url.rstrip("/") + "/transcribe"

    head = (
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode()
    body = b"".join(fields) + head + data + f"\r\n--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            j = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        try:
            err = json.loads(detail)
            detail = (err.get("error") or {}).get("message") or err.get("detail") or detail
        except ValueError:
            pass
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Сервер недоступен ({base_url}): {exc.reason}. Проверьте адрес и сеть."
        ) from exc

    text = (j.get("text") or "").strip()
    segments = j.get("segments") or []
    if api == "openai":
        usage = j.get("usage") or {}
        note = f"[/v1/audio/transcriptions · {j.get('language', '?')} · usage: {usage.get('seconds', '—')}с]"
    else:
        note = f"[/transcribe · {j.get('model', '?')} · {j.get('device', '?')} · {j.get('processing_sec', '—')}с]"
    return text, segments, note


# ---------- доставка текста в активное окно ----------

def deliver(text: str, paste: bool, send_enter: bool) -> None:
    set_clipboard(text)
    if not paste:
        print("[OK] Текст в буфере обмена (Ctrl+V)")
        return
    print(f"-> Активное окно: '{foreground_window_title()}'")
    time.sleep(0.15)
    try:
        press_ctrl_v()
        print("[OK] Ctrl+V отправлен в активное окно")
    except RuntimeError as exc:
        print(f"[!] Ctrl+V не прошёл: {exc}")
        print("    Вставляю текст напрямую (юникод-символы)...")
        type_text(text)
        print("[OK] Текст введён в активное окно")
    if send_enter:
        time.sleep(0.05)
        _tap(VK_RETURN)
        print("[OK] Enter отправлен")


# ---------- обработка аудио ----------

def handle_audio(data: bytes, filename: str, content_type: str, cfg) -> None:
    try:
        started = time.perf_counter()
        print("Распознаю...")
        text, segments, note = transcribe(data, filename, content_type, cfg.url, cfg.api)
        elapsed = time.perf_counter() - started
        if not text:
            print("[!] Ничего не распознано")
            return
        print(f"[{elapsed:.1f}с] {text}")
        if segments:
            out = "\n".join(
                f"[{s.get('start', 0)}-{s.get('end', 0)}] {s.get('text', '')}" for s in segments
            )
            print("---\n" + out)
        print(note)
        if not cfg.print_only:
            deliver(text, cfg.paste, cfg.send_enter)
    except Exception as exc:  # noqa: BLE001
        print(f"[!] Ошибка: {exc}")


# ---------- режим голосового ввода с горячей клавишей ----------

def run_hotkey(cfg) -> None:
    from pynput import keyboard

    state_lock = threading.Lock()
    rec = Recorder()
    recording = False
    busy = False

    def toggle() -> None:
        nonlocal recording, busy
        with state_lock:
            if busy:
                print("[...] Подождите, идёт распознавание")
                return
            if not recording:
                try:
                    rec.start(cfg.device)
                except Exception as exc:  # noqa: BLE001
                    print(f"[!] Не удалось открыть микрофон: {exc}")
                    return
                recording = True
                print("[REC] Запись... (нажмите горячую клавишу, чтобы остановить)")
                return
            recording = False
            busy = True
        pcm = rec.stop()
        print(f"[STOP] Остановлено ({len(pcm) / (2 * SAMPLE_RATE):.1f} сек)")

        def work():
            nonlocal busy
            handle_audio(to_wav(pcm), "voice.wav", "audio/wav", cfg)
            with state_lock:
                busy = False

        threading.Thread(target=work, daemon=True).start()

    print(f"Голосовой ввод готов: {cfg.hotkey}")
    print(f"Сервер: {cfg.url}  (api: {cfg.api})")
    if cfg.device:
        idx = _match_device(cfg.device)
        print(f"Микрофон: {cfg.device} -> [{idx}] {_device_name(idx)}" if idx is not None
              else f"Микрофон: {cfg.device} (будет найден при старте записи)")
    else:
        print("Микрофон: авто")
    print("Кликните в поле ввода и нажмите горячую клавишу. Ctrl+C — выход.\n")

    with keyboard.GlobalHotKeys({cfg.hotkey: toggle}) as h:
        h.join()


# ---------- CLI ----------

def main() -> None:
    parser = argparse.ArgumentParser(description="Портативный голосовой клиент whisper-gigaam")
    parser.add_argument("--url", help="адрес сервера (по умолчанию из VOICE_URL или http://127.0.0.1:8010)")
    parser.add_argument("--api", choices=["native", "openai"], help="какой эндпоинт использовать")
    parser.add_argument("--device", help="микрофон: имя (фрагмент) или индекс")
    parser.add_argument("--hotkey", help="комбинация клавиш старт/стоп (синтаксис pynput)")
    parser.add_argument("--file", help="распознать файл вместо записи с микрофона")
    parser.add_argument("--print-only", action="store_true", help="только напечатать текст, без вставки")
    parser.add_argument("--list-devices", action="store_true", help="показать входные устройства и выйти")
    args = parser.parse_args()

    if args.list_devices:
        list_devices()
        return

    cfg = argparse.Namespace(
        url=(args.url or os.environ.get("VOICE_URL") or "http://127.0.0.1:8010").rstrip("/"),
        api=(args.api or os.environ.get("VOICE_API") or "native").lower(),
        device=(args.device or os.environ.get("VOICE_DEVICE") or "").strip(),
        hotkey=(args.hotkey or os.environ.get("VOICE_HOTKEY") or "<ctrl>+<alt>+z").strip(),
        paste=os.environ.get("VOICE_PASTE", "1").lower() not in ("0", "false", "no"),
        send_enter=os.environ.get("VOICE_SEND_ENTER", "0").lower() in ("1", "true", "yes"),
        print_only=args.print_only,
    )
    if cfg.device and _match_device(cfg.device) is None:
        sys.exit(f"Микрофон '{cfg.device}' не найден. См. --list-devices.")

    if args.file:
        f = Path(args.file)
        if not f.exists():
            sys.exit(f"Файл не найден: {f}")
        handle_audio(f.read_bytes(), f.name, "audio/wav", cfg)
        return

    run_hotkey(cfg)


if __name__ == "__main__":
    main()
