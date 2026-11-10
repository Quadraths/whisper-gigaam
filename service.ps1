# Управление автозапуском сервера транскрибации GigaAM (переживает перезагрузку).
#
#   .\service.ps1 install     установить сервис (запуск при старте Windows, до входа в систему)
#   .\service.ps1 status      состояние (задача + здоровье сервера)
#   .\service.ps1 stop        остановить
#   .\service.ps1 start       запустить
#   .\service.ps1 uninstall   удалить сервис
#
# Реализация - задача Планировщика заданий Windows из service_task.xml
# (встроенная служба ОС, без сторонних утилит): запуск при буте, работа от SYSTEM,
# автоматический перезапуск при сбое.
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet("install", "uninstall", "status", "start", "stop")]
    [string]$Action
)
Set-Location -LiteralPath $PSScriptRoot
$ErrorActionPreference = "Stop"

$TaskName = "WhisperGigaAM Server"
$TaskXml = Join-Path $PSScriptRoot "service_task.xml"
$Launcher = Join-Path $PSScriptRoot "service_run.ps1"

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

# schtasks требует повышения; вывод перенаправляем в файл, чтобы показать результат.
# Кодировка консоли cmd на русской Windows - OEM cp866, перечитываем её как cp866.
function Invoke-Elevated([string]$cmdLine) {
    $log = Join-Path $PSScriptRoot "logs\elevated.log"
    New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null
    Remove-Item $log -Force -ErrorAction SilentlyContinue
    Start-Process cmd.exe -Verb RunAs -Wait -ArgumentList "/c $cmdLine >> `"$log`" 2>&1"
    if (Test-Path $log) {
        $oem = [System.Text.Encoding]::GetEncoding(866)
        Write-Host $oem.GetString([System.IO.File]::ReadAllBytes($log))
    }
}

switch ($Action) {
    "install" {
        if (-not (Test-Path $TaskXml)) { throw "Не найден $TaskXml" }
        if (-not (Test-Path $Launcher)) { throw "Не найден $Launcher" }
        if (-not (Test-Path (Join-Path $PSScriptRoot ".venv\Scripts\python.exe"))) {
            throw "Не найден .venv - выполните один раз start.ps1 (установка зависимостей)."
        }
        # удаляем перед созданием: schtasks /create /f не обновляет права существующей задачи
        if (Test-Admin) {
            cmd /c "schtasks /delete /tn `"$TaskName`" /f & schtasks /create /tn `"$TaskName`" /xml `"$TaskXml`" /f & schtasks /run /tn `"$TaskName`""
        } else {
            Write-Host "Требуется подтверждение UAC..."
            Invoke-Elevated "schtasks /delete /tn `"$TaskName`" /f & schtasks /create /tn `"$TaskName`" /xml `"$TaskXml`" /f & schtasks /run /tn `"$TaskName`""
        }
        Write-Host ""
        Write-Host "Готово. Проверка: .\service.ps1 status ; логи: logs\service.log"
    }
    "uninstall" {
        if (Test-Admin) {
            cmd /c "schtasks /end /tn `"$TaskName`" 2>nul & schtasks /delete /tn `"$TaskName`" /f"
        } else {
            Write-Host "Требуется подтверждение UAC..."
            Invoke-Elevated "schtasks /end /tn `"$TaskName`" & schtasks /delete /tn `"$TaskName`" /f"
        }
        Write-Host "Сервис '$TaskName' удалён."
    }
    "start" {
        if (Test-Admin) { cmd /c "schtasks /run /tn `"$TaskName`"" } else { Invoke-Elevated "schtasks /run /tn `"$TaskName`"" }
        Write-Host "Сервис запущен (модель грузится ~20-30 с)."
    }
    "stop" {
        if (Test-Admin) { cmd /c "schtasks /end /tn `"$TaskName`"" } else { Invoke-Elevated "schtasks /end /tn `"$TaskName`"" }
        Write-Host "Сервис остановлен."
    }
    "status" {
        # cmd /c + 2>&1: иначе stderr schtasks роняет скрипт при ErrorActionPreference=Stop
        $q = cmd /c "schtasks /query /tn `"$TaskName`" /fo LIST 2>&1"
        $taskVisible = ($q -join " ") -notmatch "ERROR|не найдена|cannot find|Access is denied|Отказано в доступе"
        try {
            $h = Invoke-RestMethod "http://127.0.0.1:8010/health" -TimeoutSec 5
            $serverUp = $true
        } catch {
            $serverUp = $false
        }
        if (-not $taskVisible) {
            # Задача может быть нечитаема из ограниченной среды (например, песочницы),
            # поэтому живой сервер на порту - независимое подтверждение установки.
            if ($serverUp) {
                Write-Host "Задача: $TaskName (не читается из этой среды, но сервер отвечает - сервис установлен)"
            } else {
                Write-Host "Сервис НЕ установлен. Установка: .\service.ps1 install"
                return
            }
        } else {
            Write-Host "Задача: $TaskName"
            $q | Select-String -Pattern "Status|Last Run Time|Last Result|Next Run Time|Состояние|Время последнего запуска|Последний результат" | ForEach-Object { Write-Host $_.Line.Trim() }
        }
        if ($serverUp) {
            Write-Host "Сервер: работает (model=$($h.model), device=$($h.device), gpu=$($h.gpu))"
        } else {
            Write-Host "Сервер: не отвечает на http://127.0.0.1:8010/health (см. logs\service.log)"
        }
    }
}
