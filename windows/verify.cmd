@echo off
rem Checks that Odoo Edu Manager works: only reads, changes nothing.
title Odoo Edu Manager verification
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0verify.ps1"
echo.
pause
