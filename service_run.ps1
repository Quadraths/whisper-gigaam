# Лаунчер сервера транскрибации GigaAM для Планировщика заданий Windows.
# Не запускайте вручную — используйте service.ps1 (install/status/stop/start).
Set-Location -LiteralPath $PSScriptRoot
$ErrorActionPreference = "Continue"

$logDir = Join-Path $PSScriptRoot "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$log = Join-Path $logDir "service.log"

# Простая ротация: если лог больше 10 МБ, переименовываем в .old
if ((Test-Path $log) -and ((Get-Item $log).Length -gt 10MB)) {
    Move-Item $log (Join-Path $logDir "service.old.log") -Force
}

$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Add-Content $log "[$(Get-Date -Format o)] ОШИБКА: не найден .venv - выполните один раз start.ps1"
    exit 1
}

# ffmpeg: локальный в tools\ (системный PATH в службе может не содержать ffmpeg)
$ffmpegDir = Get-ChildItem -Path (Join-Path $PSScriptRoot "tools") -Directory -Filter "ffmpeg-*" -ErrorAction SilentlyContinue |
    Select-Object -First 1 -ExpandProperty FullName
if ($ffmpegDir) { $env:PATH = (Join-Path $ffmpegDir "bin") + ";" + $env:PATH }

Add-Content $log "[$(Get-Date -Format o)] === Запуск сервера GigaAM (PID $PID) ==="
& $py -m whisper_gigaam.server *>> $log
Add-Content $log "[$(Get-Date -Format o)] === Сервер остановлен (код $LASTEXITCODE) ==="
