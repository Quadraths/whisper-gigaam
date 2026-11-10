# Голосовой ввод: микрофон -> GigaAM-сервер -> текст в активное окно.
# Запуск:  .\start_voice.ps1     (или двойным кликом по start_voice.bat)
# Полезные команды:
#   .\start_voice.ps1 --list-devices          # список микрофонов
#   .\start_voice.ps1 --device "USB Condenser"  # выбрать микрофон разово
#   .\start_voice.ps1 --file запись.wav --print-only  # проверка без микрофона
Set-Location -LiteralPath $PSScriptRoot
$ErrorActionPreference = "Stop"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "Устанавливаю uv..."
    winget install --id astral-sh.uv -e --accept-package-agreements --accept-source-agreements
    Write-Host "Готово. Запустите start_voice.ps1 ещё раз (PATH обновится в новом окне)."
    exit 0
}

$ffmpegLocal = Get-ChildItem -Path (Join-Path $PSScriptRoot "tools") -Directory -Filter "ffmpeg-*" -ErrorAction SilentlyContinue
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue) -and -not $ffmpegLocal) {
    Write-Host "Устанавливаю ffmpeg..."
    winget install --id Gyan.FFmpeg -e --accept-package-agreements --accept-source-agreements
    Write-Host "Готово. Запустите start_voice.ps1 ещё раз (PATH обновится в новом окне)."
    exit 0
}

uv sync
if ($LASTEXITCODE -ne 0) {
    Write-Host "Не удалось установить зависимости - см. сообщение выше."
    exit 1
}

uv run python -m whisper_gigaam.voice @args
