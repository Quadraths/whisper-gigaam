"""Локальный сервер транскрибации русской речи на базе GigaAM.

Запуск:
    uv run python -m whisper_gigaam.server     (или start.bat)
Интерфейс:
    http://127.0.0.1:8010   (порт настраивается в .env или переменной PORT)

Настройки — файл .env рядом с проектом (или переменные окружения):
    HOST           - адрес сервера        (по умолчанию 127.0.0.1)
    PORT           - порт сервера         (по умолчанию 8010)
    GIGAAM_MODEL   - модель               (по умолчанию e2e_rnnt = v3_e2e_rnnt, с пунктуацией)
    GIGAAM_DEVICE  - cuda/cpu             (по умолчанию cuda, если GPU есть)
    GIGAAM_CACHE   - папка кэша моделей   (по умолчанию models/ в проекте)
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
import soundfile as sf
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from .envutil import PROJECT_DIR, load_env_file

load_env_file(PROJECT_DIR / ".env")

MODELS_DIR = Path(os.environ.get("GIGAAM_CACHE", PROJECT_DIR / "models"))
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8010"))
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "2048"))  # лимит размера загрузки
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024


def _ensure_ffmpeg() -> None:
    """Находит ffmpeg: системный или локальный в tools/ (кладётся рядом с проектом)."""
    if shutil.which("ffmpeg"):
        return
    for bin_dir in sorted((PROJECT_DIR / "tools").glob("ffmpeg-*/bin")):
        if (bin_dir / "ffmpeg.exe").exists():
            os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
            return


_ensure_ffmpeg()

import gigaam  # noqa: E402  (после _ensure_ffmpeg, чтобы gigaam.load_audio нашёл ffmpeg)

# --- параметры ---
SAMPLE_RATE = 16_000            # GigaAM работает только с 16 кГц, моно
MAX_CHUNK_SEC = 20.0            # transcribe() принимает до 25 секунд -> режем до 20
MIN_CHUNK_SEC = 0.3
SILENCE_RMS = 0.008             # порог тишины (сигнал нормирован в [-1, 1])
MIN_SILENCE_SEC = 0.25          # минимальная пауза, по которой можно разрезать

# --- настройки ---
MODEL_NAME = os.environ.get("GIGAAM_MODEL", "e2e_rnnt")  # e2e_rnnt = v3_e2e_rnnt
DEVICE = os.environ.get("GIGAAM_DEVICE")                  # None -> cuda, если доступен

_model = None
_device = "cpu"
_gpu_name = "CPU"

_lock = threading.Lock()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _model, _device, _gpu_name
    import torch

    _device = DEVICE or ("cuda" if torch.cuda.is_available() else "cpu")
    if torch.cuda.is_available():
        _gpu_name = torch.cuda.get_device_name(0)
    print(f"Загрузка модели GigaAM '{MODEL_NAME}' ({_device}, {_gpu_name})...")
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    _model = gigaam.load_model(MODEL_NAME, device=_device, download_root=str(MODELS_DIR))
    print(f"Модель готова. Указатель: http://{HOST}:{PORT}")
    yield


app = FastAPI(
    title="GigaAM Transcription Server",
    version="0.1.0",
    lifespan=lifespan,
)


def split_on_silence(samples: np.ndarray) -> list[tuple[int, int]]:
    """Разбивает сигнал на куски <= MAX_CHUNK_SEC, стараясь резать по паузам."""
    n = len(samples)
    if n == 0:
        return []
    max_len = int(MAX_CHUNK_SEC * SAMPLE_RATE)
    min_len = int(MIN_CHUNK_SEC * SAMPLE_RATE)

    cut_points: list[int] = []
    frame = int(0.03 * SAMPLE_RATE)  # 30 мс фрейм
    n_frames = max(1, n // frame)
    if n_frames > 1:
        audio = samples[: n_frames * frame]
        rms = np.sqrt(np.mean(audio.reshape(n_frames, frame) ** 2, axis=1))
        silent = rms < SILENCE_RMS
        start = None
        for i, s in enumerate(silent):
            if s and start is None:
                start = i
            elif not s and start is not None:
                if (i - start) * frame >= MIN_SILENCE_SEC * SAMPLE_RATE:
                    cut_points.append((start + i) * frame // 2)
                start = None
        if start is not None:
            cut_points.append((start + n_frames) * frame // 2)

    segments: list[tuple[int, int]] = []
    pos = 0
    while n - pos > max_len:
        window_end = pos + max_len
        cut = None
        for c in cut_points:
            if pos + min_len <= c <= window_end:
                cut = c
            elif c > window_end:
                break
        if cut is None:
            cut = window_end
        segments.append((pos, cut))
        pos = cut
    segments.append((pos, n))
    return segments


def _transcribe_data(data: bytes, word_timestamps: bool = False) -> dict:
    """Общий конвейер: декодирование -> нарезка -> распознавание."""
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="gigaam_") as tmp:
        src = Path(tmp) / "input"
        src.write_bytes(data)
        try:
            wav = gigaam.load_audio(str(src))
        except Exception as exc:
            raise ValueError(
                "Не удалось прочитать аудио. Поддерживаются любые форматы (через ffmpeg): wav, mp3, m4a, ogg, mp4 и др."
            ) from exc
        samples = wav.numpy()
        segments: list[dict] = []
        words: list[dict] = []
        with _lock:
            for i, (a, b) in enumerate(split_on_silence(samples)):
                chunk = Path(tmp) / f"chunk{i}.wav"
                sf.write(chunk, samples[a:b], SAMPLE_RATE)
                try:
                    res = _model.transcribe(str(chunk), word_timestamps=word_timestamps)
                except ValueError as exc:
                    if "Too long" in str(exc):
                        raise RuntimeError("Ошибка сегментации (слишком длинный кусок)") from exc
                    raise
                text = res.text.strip()
                if not text:
                    continue
                seg_start, seg_end = round(a / SAMPLE_RATE, 2), round(b / SAMPLE_RATE, 2)
                segments.append({"start": seg_start, "end": seg_end, "text": text})
                if word_timestamps and res.words:
                    words.extend(
                        {
                            "word": w.text,
                            "start": round(w.start + a / SAMPLE_RATE, 2),
                            "end": round(w.end + a / SAMPLE_RATE, 2),
                        }
                        for w in res.words
                    )
    return {
        "text": " ".join(s["text"] for s in segments),
        "segments": segments,
        "words": words,
        "audio_sec": round(len(samples) / SAMPLE_RATE, 2),
        "processing_sec": round(time.perf_counter() - started, 2),
    }


HTML = r"""
<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8"><title>GigaAM — транскрибация</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{font-family:system-ui,sans-serif;max-width:720px;margin:40px auto;padding:0 16px}
button{padding:8px 16px;cursor:pointer}select{padding:6px;max-width:100%}
#result{width:100%;min-height:120px;margin-top:12px}pre{white-space:pre-wrap}
.rec-on{background:#c62828;color:#fff}</style></head><body>
<h1>GigaAM — локальная транскрибация (русский)</h1>

<div style="background:#f5f5f5;border-radius:8px;padding:12px 16px;margin-bottom:16px">
<b>API-эндпоинт:</b>
<label style="margin-left:12px"><input type="radio" name="api" value="old" checked> Старый <code>/transcribe</code> (родной)</label>
<label style="margin-left:12px"><input type="radio" name="api" value="new"> Новый <code>/v1/audio/transcriptions</code> (OpenAI)</label>
</div>

<h3>Запись с микрофона</h3>
<select id="mic"></select>
<button id="mics-refresh" onclick="initMics()" title="Обновить список микрофонов">↻</button>
<br><br>
<button id="rec" onclick="toggleRec()">● Записать</button>
<span id="rectimer" style="margin-left:8px;font-variant-numeric:tabular-nums"></span>
<p style="color:#666;font-size:13px">Нажмите «Записать», говорите, затем «■ Остановить» — распознавание начнётся автоматически.</p>

<h3>Или загрузите файл</h3>
<p style="color:#666;font-size:13px">Форматы: всё, что читает ffmpeg (wav, mp3, m4a, ogg, mp4 и др.). Файл любой длины.</p>
<input type="file" id="file" accept="audio/*,video/*"><br><br>
<button id="run" onclick="go()">Распознать</button>

<div id="error" style="color:#c33;margin-top:8px"></div>
<h3>Результат</h3>
<pre id="result"></pre><br>
<button id="copy" onclick="copy()" style="display:none">Копировать</button>

<script>
const $ = id => document.getElementById(id);

async function initMics(){
  try {
    // один запрос разрешения — после него enumerateDevices отдаёт имена устройств
    const s = await navigator.mediaDevices.getUserMedia({audio:true});
    s.getTracks().forEach(t=>t.stop());
    const devs = await navigator.mediaDevices.enumerateDevices();
    const sel = $('mic'); const cur = sel.value;
    sel.innerHTML = '';
    devs.filter(d=>d.kind==='audioinput').forEach((d,i)=>{
      const o = document.createElement('option');
      o.value = d.deviceId; o.textContent = d.label || ('Микрофон ' + (i+1));
      sel.appendChild(o);
    });
    if (cur && [...sel.options].some(o=>o.value===cur)) sel.value = cur;
  } catch(e){ $('error').innerText = 'Нет доступа к микрофону: ' + e.message; }
}

function api(){ return document.querySelector('input[name="api"]:checked').value; }

function render(text, segments, note){
  const out = (segments||[]).map(s=>`[${Math.round(s.start*10)/10}-${Math.round(s.end*10)/10}] ${s.text}`).join('\n');
  $('result').innerText = (text||'') + (out ? '\n---\n' + out : '') + (note ? '\n\n' + note : '');
  $('copy').style.display='inline'; $('copy').dataset.t = text||'';
}

function post(blobOrFile, name){
  $('error').innerText = '';
  $('result').innerText = 'Распознаю...';
  $('run').disabled = true; $('rec').disabled = true;
  const fd = new FormData();
  fd.append('file', blobOrFile, name);
  const mode = api();
  if (mode === 'new'){
    // OpenAI-совместимый эндпоинт: verbose_json даёт текст + сегменты с таймкодами
    fd.append('model', 'whisper-1');
    fd.append('response_format', 'verbose_json');
    fd.append('timestamp_granularities[]', 'segment');
  }
  fetch(mode === 'new' ? '/v1/audio/transcriptions' : '/transcribe', {method:'POST', body:fd})
    .then(async r => {
      const j = await r.json();
      if (!r.ok) throw new Error((j.error && j.error.message) || j.detail || ('HTTP ' + r.status));
      if (mode === 'new'){
        render(j.text, j.segments, '[/v1/audio/transcriptions · verbose_json · usage: ' + (j.usage ? j.usage.seconds + 'с' : '—') + ']');
      } else {
        if (!j.ok) throw new Error(j.detail || 'unknown error');
        render(j.text, j.segments, '[/transcribe · ' + j.model + ' · ' + j.device + ' · ' + j.processing_sec + 'с]');
      }
    })
    .catch(e=>{ $('result').innerText=''; $('error').innerText = 'Ошибка: ' + e.message; })
    .finally(()=>{ $('run').disabled = false; $('rec').disabled = false; });
}

function go(){
  const f = $('file').files[0];
  if (!f){ alert('Выберите файл'); return; }
  post(f, f.name);
}

// --- запись с микрофона (MediaRecorder) ---
let rec = null, chunks = [], recTimer = null, recStart = 0;

function fmt(sec){
  const m = Math.floor(sec/60), s = Math.floor(sec%60);
  return String(m).padStart(2,'0') + ':' + String(s).padStart(2,'0');
}

async function toggleRec(){
  if (rec && rec.state === 'recording'){ rec.stop(); return; }
  $('error').innerText = '';
  try {
    const devId = $('mic').value;
    const stream = await navigator.mediaDevices.getUserMedia(
      devId ? {audio:{deviceId:{exact:devId}}} : {audio:true}
    );
    chunks = [];
    const mime = ['audio/webm;codecs=opus','audio/webm','audio/ogg;codecs=opus','']
      .find(t => t === '' || MediaRecorder.isTypeSupported(t));
    rec = new MediaRecorder(stream, mime ? {mimeType:mime} : undefined);
    rec.ondataavailable = e => { if (e.data.size) chunks.push(e.data); };
    rec.onstop = () => {
      clearInterval(recTimer); $('rectimer').innerText = '';
      stream.getTracks().forEach(t=>t.stop());
      $('rec').textContent = '● Записать'; $('rec').classList.remove('rec-on');
      const type = (rec.mimeType || 'audio/webm').split(';')[0];
      const ext = type.includes('ogg') ? 'ogg' : 'webm';
      post(new Blob(chunks, {type}), 'record.' + ext);
    };
    rec.start();
    recStart = Date.now();
    $('rectimer').innerText = '00:00';
    recTimer = setInterval(()=>{ $('rectimer').innerText = fmt((Date.now()-recStart)/1000); }, 250);
    $('rec').textContent = '■ Остановить'; $('rec').classList.add('rec-on');
  } catch(e){ $('error').innerText = 'Не удалось начать запись: ' + e.message; }
}

function copy(){ navigator.clipboard.writeText($('copy').dataset.t || ''); }

initMics();
</script></body></html>
"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(HTML)


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "model": MODEL_NAME, "device": _device, "gpu": _gpu_name}


@app.post("/transcribe")
async def transcribe(request: Request, file: UploadFile = File(...)) -> dict:
    """Родной формат сервера (используется веб-страницей и голосовым клиентом)."""
    if _too_large(request):
        raise HTTPException(status_code=413, detail=f"Файл больше {MAX_UPLOAD_MB} МБ (настройка MAX_UPLOAD_MB)")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Пустой файл")
    if _too_large(request, data):
        raise HTTPException(status_code=413, detail=f"Файл больше {MAX_UPLOAD_MB} МБ (настройка MAX_UPLOAD_MB)")
    try:
        result = _transcribe_data(data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": True, "model": MODEL_NAME, "device": _device, "gpu": _gpu_name, **result}


# ---------- OpenAI-совместимый API (/v1/audio/transcriptions) ----------


def _openai_error(status: int, message: str, err_type: str = "invalid_request_error",
                  param: str | None = None, code: str | None = None) -> JSONResponse:
    """Ошибка в формате OpenAI: {"error": {"message", "type", "param", "code"}}."""
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": err_type, "param": param, "code": code}},
    )


def _too_large(request: Request, data: bytes | None = None) -> bool:
    """Лимит размера загрузки: по Content-Length (до чтения) и по факту (после)."""
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    if data is not None:
        return len(data) > MAX_UPLOAD_BYTES
    return declared > MAX_UPLOAD_BYTES + 1024 * 1024  # +1 МБ на multipart-обёртку


@app.exception_handler(RequestValidationError)
async def _validation_handler(request: Request, exc: RequestValidationError):
    # Для /v1/* приводим ошибки валидации FastAPI к формату OpenAI ({"error": ...});
    # остальные маршруты оставляем с родным {"detail": ...}
    if not request.url.path.startswith("/v1/"):
        return JSONResponse(status_code=422, content={"detail": exc.errors()})
    err = exc.errors()[0] if exc.errors() else {}
    param = ".".join(str(p) for p in err.get("loc", [])[1:]) or None
    return _openai_error(400, err.get("msg", "Invalid request"), param=param)


def _fmt_srt_time(sec: float) -> str:
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1_000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def _fmt_vtt_time(sec: float) -> str:
    return _fmt_srt_time(sec).replace(",", ".")


def _to_srt(segments: list[dict]) -> str:
    blocks = [
        f"{i}\n{_fmt_srt_time(s['start'])} --> {_fmt_srt_time(s['end'])}\n{s['text']}\n"
        for i, s in enumerate(segments, 1)
    ]
    return "\n".join(blocks)


def _to_vtt(segments: list[dict]) -> str:
    blocks = [
        f"{_fmt_vtt_time(s['start'])} --> {_fmt_vtt_time(s['end'])}\n{s['text']}\n"
        for s in segments
    ]
    return "WEBVTT\n\n" + "\n".join(blocks)


@app.post("/v1/audio/transcriptions", response_model=None)
async def openai_transcription(
    request: Request,
    file: UploadFile = File(...),
    model: str = Form(...),
    language: str | None = Form(None),
    prompt: str | None = Form(None),
    response_format: str = Form("json"),
    temperature: float | None = Form(None),
    timestamp_granularities: list[str] = Form(default=[]),
    # OpenAI SDK шлёт поле с квадратными скобками: timestamp_granularities[]=word
    timestamp_granularities_sq: list[str] = Form(default=[], alias="timestamp_granularities[]"),
) -> JSONResponse | PlainTextResponse:
    """Совместимо с OpenAI Audio API (createTranscription).

    Принимает: file*, model*, language, prompt, response_format
    (json | text | srt | verbose_json | vtt), temperature, timestamp_granularities[]
    (word | segment). Поля language/prompt/temperature принимаются, но игнорируются:
    модель русскоязычная, декодирование жадное.
    """
    if not model.strip():
        return _openai_error(400, "The model parameter is required.", param="model")
    fmt = response_format.lower()
    if fmt not in ("json", "text", "srt", "verbose_json", "vtt"):
        return _openai_error(400, f"Invalid response_format: {response_format}", param="response_format")
    granularities = {g.lower() for g in [*timestamp_granularities, *timestamp_granularities_sq]}

    if _too_large(request):
        return _openai_error(413, f"Audio file exceeds {MAX_UPLOAD_MB} MB limit.", param="file", code="request_too_large")
    data = await file.read()
    if not data:
        return _openai_error(400, "The audio file is empty.", param="file")
    if _too_large(request, data):
        return _openai_error(413, f"Audio file exceeds {MAX_UPLOAD_MB} MB limit.", param="file", code="request_too_large")
    try:
        result = _transcribe_data(data, word_timestamps="word" in granularities)
    except ValueError as exc:
        return _openai_error(400, str(exc), param="file")
    except Exception as exc:  # noqa: BLE001
        return _openai_error(500, f"Transcription failed: {exc}", err_type="server_error")

    if fmt == "text":
        return PlainTextResponse(result["text"] + "\n", media_type="text/plain")
    if fmt == "srt":
        return PlainTextResponse(_to_srt(result["segments"]), media_type="text/plain")
    if fmt == "vtt":
        return PlainTextResponse(_to_vtt(result["segments"]), media_type="text/plain")

    if fmt == "json":
        return JSONResponse({"text": result["text"]})

    # verbose_json — схема OpenAI TranscriptionVerbose
    segments = [
        {
            "id": i,
            "seek": 0,
            "start": s["start"],
            "end": s["end"],
            "text": s["text"],
            "tokens": [],
            "temperature": 0.0,
            "avg_logprob": 0.0,
            "compression_ratio": 0.0,
            "no_speech_prob": 0.0,
        }
        for i, s in enumerate(result["segments"])
    ]
    payload: dict = {
        "task": "transcribe",
        "language": language or "russian",
        "duration": result["audio_sec"],
        "text": result["text"],
    }
    if "segment" in granularities or not granularities:
        payload["segments"] = segments
    if "word" in granularities:
        payload["words"] = result["words"]
    payload["usage"] = {"type": "duration", "seconds": round(result["audio_sec"])}
    return JSONResponse(payload)


@app.get("/v1/models")
async def openai_models() -> dict:
    """Список моделей в формате OpenAI — многие клиенты запрашивают его при подключении."""
    return {
        "object": "list",
        "data": [
            {"id": MODEL_NAME, "object": "model", "created": 0, "owned_by": "local"},
            {"id": "whisper-1", "object": "model", "created": 0, "owned_by": "local"},
        ],
    }


def main() -> None:
    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()
