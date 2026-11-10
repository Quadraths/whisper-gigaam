"""Голосовой ввод: микрофон -> GigaAM-сервер -> текст в активное окно.

Использование:
    uv run python -m whisper_gigaam.voice     (или start_voice.bat)

Горячая клавиша (по умолчанию Ctrl+Alt+Z) переключает запись:
    1. Кликните в поле ввода (чат DeepSeek Harness или любое окно).
    2. Нажмите горячую клавишу и говорите.
    3. Нажмите её снова — речь распознаётся, текст вставляется (Ctrl+V).
    4. Enter — отправить (или VOICE_SEND_ENTER=1 сделает это сам).

Проверка без микрофона:
    uv run python -m whisper_gigaam.voice --file запись.wav
    uv run python -m whisper_gigaam.voice --file запись.wav --print-only

Микрофон:
    uv run python -m whisper_gigaam.voice --list-devices      # список устройств
    uv run python -m whisper_gigaam.voice --device "USB Condenser"  # разово выбрать

Настройки в .env:
    VOICE_DEVICE       - микрофон: фрагмент имени или индекс из --list-devices
    VOICE_HOTKEY       - комбинация pynput (по умолчанию <ctrl>+<alt>+z)
    TRANSCRIBE_URL     - адрес сервера (по умолчанию http://127.0.0.1:PORT/transcribe)
    VOICE_PASTE        - 1 = вставлять текст в активное окно, 0 = только в буфер обмена
    VOICE_SEND_ENTER   - 1 = нажимать Enter после вставки
"""
from __future__ import annotations

import argparse
import ctypes
import io
import os
import sys
import threading
import time
import wave
from pathlib import Path

from .envutil import PROJECT_DIR, load_env_file

load_env_file(PROJECT_DIR / ".env")

# Консоли бывают разной кодировки (cp866/cp1251); вывод не должен ронять клиент.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")

LOG_DIR = PROJECT_DIR / "logs"
LOG_FILE = LOG_DIR / "voice_client.log"


def log(msg: str) -> None:
    """Пишет строку в журнал и в консоль (консоль может быть недоступна)."""
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 1_000_000:
            LOG_FILE.replace(LOG_DIR / "voice_client.old.log")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print(line)

SAMPLE_RATE = 16_000  # GigaAM работает с 16 кГц моно
PORT = os.environ.get("PORT", "8010")
TRANSCRIBE_URL = os.environ.get("TRANSCRIBE_URL", f"http://127.0.0.1:{PORT}/transcribe")
HOTKEY = os.environ.get("VOICE_HOTKEY", "<ctrl>+<alt>+z")
PASTE = os.environ.get("VOICE_PASTE", "1").lower() not in ("0", "false", "no")
SEND_ENTER = os.environ.get("VOICE_SEND_ENTER", "0").lower() in ("1", "true", "yes")

_state_lock = threading.Lock()
_recording = False
_busy = False

# ---------- Windows: буфер обмена и имитация клавиш (без лишних зависимостей) ----------

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
user32.SetClipboardData.restype = ctypes.c_void_p

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


# cbSize в SendInput обязан равняться sizeof(INPUT) (40 байт на x64, 28 на x86),
# иначе Windows молча отклоняет ВСЕ события — вставка "не работает" без ошибок.
user32.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(_INPUT), ctypes.c_int]
user32.SendInput.restype = ctypes.c_uint
kernel32.GetLastError.restype = ctypes.c_uint


def _send(*events: _INPUT) -> int:
    """Отправляет события клавиатуры; возвращает число принятых, падает при отказе."""
    arr = (_INPUT * len(events))(*events)
    sent = user32.SendInput(len(events), arr, ctypes.sizeof(_INPUT))
    if sent != len(events):
        err = kernel32.GetLastError()
        raise RuntimeError(f"SendInput отклонён (принято {sent}/{len(events)}, код ошибки {err})")
    return sent


def _key_event(vk: int, up: bool = False, unicode_char: str = "") -> _INPUT:
    if unicode_char:
        return _INPUT(
            type=INPUT_KEYBOARD,
            ki=_KEYBDINPUT(
                wVk=0,
                wScan=ord(unicode_char),
                dwFlags=KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0),
            ),
        )
    return _INPUT(type=INPUT_KEYBOARD, ki=_KEYBDINPUT(wVk=vk, dwFlags=KEYEVENTF_KEYUP if up else 0))


def foreground_window_title() -> str:
    """Заголовок активного окна — для диагностики «куда уйдёт вставка»."""
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
    """Запасной способ: прямой ввод символов (юникод-события) в активное окно."""
    events = []
    for ch in text:
        events.append(_key_event(0, unicode_char=ch))
        events.append(_key_event(0, up=True, unicode_char=ch))
    for i in range(0, len(events), 20):  # SendInput ограничен ~1024 событиями, шлём порциями
        _send(*events[i : i + 20])


def deliver(text: str) -> None:
    """Кладёт текст в буфер обмена и (опционально) вставляет в активное окно."""
    set_clipboard(text)
    if not PASTE:
        print("✓ Текст в буфере обмена (Ctrl+V)")
        return
    print(f"→ Активное окно: «{foreground_window_title()}»")
    time.sleep(0.15)  # дать окну сохранить фокус
    try:
        press_ctrl_v()
        print("✓ Ctrl+V отправлен в активное окно" + (" (Enter — отправить)" if not SEND_ENTER else ""))
    except RuntimeError as exc:
        print(f"! Ctrl+V не прошёл: {exc}")
        print("  Вставляю текст напрямую (юникод-события)...")
        type_text(text)
        print("✓ Текст введён в активное окно" + (" (Enter — отправить)" if not SEND_ENTER else ""))
    if SEND_ENTER:
        time.sleep(0.05)
        _tap(VK_RETURN)
        print("✓ Enter отправлен")


# ---------- Выбор микрофона ----------

# VOICE_DEVICE в .env или --device в CLI:
#   имя (или его фрагмент, без учёта регистра) — например "USB Condenser"
#   либо индекс из --list-devices
VOICE_DEVICE = os.environ.get("VOICE_DEVICE", "").strip()


def _input_devices() -> list[tuple[int, str, str]]:
    """Список входных устройств: (индекс, host API, имя)."""
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
    """Находит устройство по индексу или фрагменту имени."""
    devs = _input_devices()
    if spec.isdigit():
        idx = int(spec)
        return idx if any(i == idx for i, _, _ in devs) else None
    needle = spec.lower()
    for i, _api, name in devs:
        if needle in name.lower():
            return i
    return None


def _candidate_devices(spec: str = "") -> list[int]:
    """Кандидаты для открытия: выбранное устройство, затем все прочие входы."""
    import sounddevice as sd

    spec = spec or VOICE_DEVICE
    devs = _input_devices()
    ordered = [i for i, _, _ in devs]
    if spec:
        idx = _match_device(spec)
        if idx is None:
            raise RuntimeError(
                f"Микрофон '{spec}' не найден. Запустите --list-devices и укажите имя или индекс "
                f"в .env (VOICE_DEVICE) или через --device."
            )
        ordered.remove(idx)
        ordered.insert(0, idx)
    else:
        default = sd.default.device[0]
        if default is not None and default >= 0 and default in ordered:
            ordered.remove(default)
            ordered.insert(0, default)
    return ordered


def list_devices() -> None:
    """Печатает входные устройства для --list-devices."""
    import sounddevice as sd

    print("Входные устройства (укажите имя или индекс в VOICE_DEVICE / --device):\n")
    for i, api, name in _input_devices():
        d = sd.query_devices(i)
        print(f"  {i:>3}  [{api}] {name}  (частота {int(d['default_samplerate'])} Гц)")
    print(f"\nТекущая настройка VOICE_DEVICE: {VOICE_DEVICE or '<<авто>>'}")


# ---------- Запись с микрофона ----------


class Recorder:
    def __init__(self) -> None:
        self._chunks: list[bytes] = []
        self._stream = None

    def start(self) -> None:
        import sounddevice as sd

        self._chunks = []
        last_err = None
        for device in _candidate_devices():
            try:
                self._stream = sd.InputStream(
                    samplerate=SAMPLE_RATE,
                    channels=1,
                    dtype="int16",
                    device=device,
                    callback=self._on_audio,
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


# ---------- Распознавание ----------


def transcribe(data: bytes, filename: str, content_type: str) -> str:
    import httpx

    resp = httpx.post(
        TRANSCRIBE_URL,
        files={"file": (filename, data, content_type)},
        timeout=600,
    )
    resp.raise_for_status()
    return resp.json().get("text", "").strip()


def handle_audio(data: bytes, filename: str, content_type: str, print_only: bool) -> None:
    global _busy
    try:
        started = time.perf_counter()
        print("Распознаю...")
        text = transcribe(data, filename, content_type)
        elapsed = time.perf_counter() - started
        if not text:
            print("× Ничего не распознано")
            return
        print(f"[{elapsed:.1f}с] {text}")
        if not print_only:
            deliver(text)
    except Exception as exc:  # noqa: BLE001
        print(f"× Ошибка: {exc}")
    finally:
        with _state_lock:
            _busy = False


_recorder = Recorder()


def toggle() -> None:
    """Обработчик горячей клавиши: старт/стоп записи."""
    global _recording, _busy
    with _state_lock:
        if _busy:
            print("… Подождите, идёт распознавание")
            return
        if not _recording:
            try:
                _recorder.start()
            except Exception as exc:  # noqa: BLE001
                print(f"× Не удалось открыть микрофон: {exc}")
                return
            _recording = True
            print("● Запись... (нажмите горячую клавишу, чтобы остановить)")
            return
        _recording = False
        _busy = True
    pcm = _recorder.stop()
    print(f"■ Остановлено ({len(pcm) / (2 * SAMPLE_RATE):.1f} сек)")
    threading.Thread(
        target=handle_audio, args=(to_wav(pcm), "voice.wav", "audio/wav", False), daemon=True
    ).start()


def main() -> None:
    global VOICE_DEVICE
    parser = argparse.ArgumentParser(description="Голосовой ввод через GigaAM-сервер")
    parser.add_argument("--file", help="распознать файл вместо записи с микрофона")
    parser.add_argument("--print-only", action="store_true", help="только напечатать текст, без вставки")
    parser.add_argument("--device", help="микрофон: имя (фрагмент) или индекс — см. --list-devices")
    parser.add_argument("--list-devices", action="store_true", help="показать входные устройства и выйти")
    args = parser.parse_args()

    if args.list_devices:
        list_devices()
        return
    if args.device:
        VOICE_DEVICE = args.device.strip()
        if _match_device(VOICE_DEVICE) is None:
            sys.exit(f"Микрофон '{VOICE_DEVICE}' не найден. См. --list-devices.")

    if args.file:
        f = Path(args.file)
        if not f.exists():
            sys.exit(f"Файл не найден: {f}")
        data = f.read_bytes()
        handle_audio(data, f.name, "audio/wav", args.print_only)
        return

    print(f"Голосовой ввод готов: {HOTKEY}")
    print(f"Сервер: {TRANSCRIBE_URL}")
    if VOICE_DEVICE:
        idx = _match_device(VOICE_DEVICE)
        print(f"Микрофон: {VOICE_DEVICE} -> [{idx}] {_device_name(idx)}")
    else:
        print("Микрофон: авто")
    print("Кликните в поле ввода и нажмите горячую клавишу. Ctrl+C — выход.\n")
    from pynput import keyboard

    with keyboard.GlobalHotKeys({HOTKEY: toggle}) as h:
        h.join()


if __name__ == "__main__":
    main()
