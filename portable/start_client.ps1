# Портативный голосовой клиент whisper-gigaam.
# Первый запуск создаёт .venv и ставит 2 зависимости, дальше работает сразу.
# Запуск:  .\start_client.ps1    (или двойным кликом по start_client.bat)
# Полезно:
#   .\start_client.ps1 --list-devices
#   .\start_client.ps1 --device "USB Condenser"
#   .\start_client.ps1 --url http://192.168.130.155:8010
#   .\start_client.ps1 --file запись.wav --print-only
Set-Location -LiteralPath $PSScriptRoot
$ErrorActionPreference = "Stop"

# Python 3.10+ обязателен; uv не нужен
$py = $null
foreach ($candidate in @("python", "py -3")) {
    $parts = $candidate.Split(" ")
    $cmd = Get-Command $parts[0] -ErrorAction SilentlyContinue
    if ($cmd) { $py = $candidate; break }
}
if (-not $py) {
    Write-Host "Не найден Python 3.10+. Установите: winget install --id Python.Python.3.12"
    exit 1
}

# Первый запуск: виртуальное окружение + зависимости
if (-not (Test-Path (Join-Path $PSScriptRoot ".venv\Scripts\python.exe"))) {
    Write-Host "Создаю виртуальное окружение (первый запуск)..."
    Invoke-Expression "$py -m venv .venv"
    & ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
    & ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
}

# Конфиг: при первом запуске предлагаем скопировать шаблон
if (-not (Test-Path (Join-Path $PSScriptRoot "client.env"))) {
    Copy-Item (Join-Path $PSScriptRoot "client.env.example") (Join-Path $PSScriptRoot "client.env")
    Write-Host "Создан client.env - проверьте адрес сервера (VOICE_URL)!"
    notepad (Join-Path $PSScriptRoot "client.env")
}

& ".venv\Scripts\python.exe" (Join-Path $PSScriptRoot "voice_client.py") @args
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "Клиент завершился с ошибкой (код $LASTEXITCODE)."
    exit $LASTEXITCODE
}
