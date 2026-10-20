@echo off
rem Odoo Edu Manager - Windows installer. Double-click this file.
title Odoo Edu Manager installer
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
echo.
pause
