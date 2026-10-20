@echo off
rem Removes Odoo Edu Manager and its Linux system (asks first).
title Odoo Edu Manager uninstaller
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1"
echo.
pause
