@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0service.ps1" %*
if not "%1"=="status" pause
