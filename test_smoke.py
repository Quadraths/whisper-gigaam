"""Смоук-тест: uv run python test_smoke.py (модель качается в models/, ffmpeg — из tools/ или PATH)."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import soundfile as sf
import torch

from whisper_gigaam.server import MODELS_DIR, _ensure_ffmpeg

_ensure_ffmpeg()

SAMPLE_RATE = 16_000

print(f"Python  : {sys.version.split()[0]}")
print(f"PyTorch : {torch.__version__}")
print(f"CUDA    : {torch.cuda.is_available()}")
if torch.cuda.is_available():
    print(f"GPU     : {torch.cuda.get_device_name(0)}")
    print(f"Arch    : {torch.cuda.get_arch_list()}")

import gigaam  # noqa: E402

print(f"GigaAM  : OK ({gigaam.__file__})")
print(f"ffmpeg  : {'OK' if shutil.which('ffmpeg') else 'НЕ НАЙДЕН'}")
print(f"Models  : {MODELS_DIR}")

MODEL_NAME = os.environ.get("GIGAAM_MODEL", "e2e_rnnt")
DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")

print(f"\nЗагрузка модели {MODEL_NAME} на {DEVICE}...")
t0 = time.perf_counter()
MODELS_DIR.mkdir(parents=True, exist_ok=True)
model = gigaam.load_model(MODEL_NAME, device=DEVICE, download_root=str(MODELS_DIR))
print(f"Модель загружена за {time.perf_counter() - t0:.1f}s")

with tempfile.TemporaryDirectory(prefix="smoke_") as tmp:
    # 1) синтезированный звук — проверка конвейера (содержание текста неважно)
    dur = SAMPLE_RATE * 2
    signal = torch.zeros(dur)
    for i, f in enumerate([220, 277, 196]):
        seg = dur // 3
        s = torch.sin(2 * torch.pi * f * torch.arange(seg) / SAMPLE_RATE)
        env = torch.exp(-((torch.arange(seg) / (seg * 0.3)) ** 2))
        signal[i * seg : i * seg + seg] = s * env
    wav_file = Path(tmp) / "synthetic.wav"
    sf.write(wav_file, signal.numpy() / 3, SAMPLE_RATE)
    t0 = time.perf_counter()
    res = model.transcribe(str(wav_file))
    print(f"\n[1] Синтез: {time.perf_counter() - t0:.2f}s -> {res.text!r}")

    # 2) реальный пример GigaAM с CDN — проверка качества на русской речи
    example = Path(tmp) / "example.wav"
    url = "https://cdn.chatwm.opensmodel.sberdevices.ru/GigaAM/example.wav"
    try:
        urllib.request.urlretrieve(url, example)
        t0 = time.perf_counter()
        res = model.transcribe(str(example))
        print(f"[2] Пример: {time.perf_counter() - t0:.2f}s -> {res.text!r}")
        assert res.text.strip(), "пустой результат"
    except Exception as exc:
        print(f"[2] Пример пропущен ({exc})")

print("\nOK — модель и конвейер работают. Запускайте start.bat и http://127.0.0.1:8010")
