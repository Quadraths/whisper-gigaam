# Локальный сервер транскрибации GigaAM (русский).
# Запуск:  .\start.ps1    (или двойным кликом по start.bat)
# Модель/устройство разово:  .\start.ps1 -Model e2e_ctc -Device cuda
param(
    [string]$Model,
    [string]$Device
)
Set-Location -LiteralPath $PSScriptRoot
$ErrorActionPreference = "Stop"

if ($Model) { $env:GIGAAM_MODEL = $Model }
if ($Device) { $env:GIGAAM_DEVICE = $Device }

# uv - менеджер пакетов
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "Устанавливаю uv..."
    winget install --id astral-sh.uv -e --accept-package-agreements --accept-source-agreements
    Write-Host "Готово. Запустите start.ps1 ещё раз (PATH обновится в новом окне)."
    exit 0
}

# ffmpeg - нужен для чтения mp3/m4a/ogg; ищем системный или локальный в tools\
$ffmpegLocal = Get-ChildItem -Path (Join-Path $PSScriptRoot "tools") -Directory -Filter "ffmpeg-*" -ErrorAction SilentlyContinue
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue) -and -not $ffmpegLocal) {
    Write-Host "Устанавливаю ffmpeg..."
    winget install --id Gyan.FFmpeg -e --accept-package-agreements --accept-source-agreements
    Write-Host "Готово. Запустите start.ps1 ещё раз (PATH обновится в новом окне)."
    exit 0
}

uv sync
if ($LASTEXITCODE -ne 0) {
    Write-Host "Не удалось установить зависимости - см. сообщение выше."
    exit 1
}

uv run python -m whisper_gigaam.server
