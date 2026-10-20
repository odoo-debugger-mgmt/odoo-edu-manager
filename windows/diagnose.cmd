@echo off
rem Collects information for support: only reads, changes nothing.
title Odoo Edu Manager diagnostics
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0diagnose.ps1"
echo.
pause
