@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "DASHBOARD_PORT=%~1"
if "%DASHBOARD_PORT%"=="" set "DASHBOARD_PORT=COM22"
echo Starting dashboard on %DASHBOARD_PORT%...
"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_live_dashboard.ps1" -Port "%DASHBOARD_PORT%"
if errorlevel 1 (
    echo.
    echo Dashboard stopped with an error. Read the message above for the exact cause.
    pause
)
