@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found.
    echo Install 64-bit Python 3.11 from https://www.python.org/downloads/
    echo Select "Add Python to PATH" during installation, then run this file again.
    pause
    exit /b 1
)
python --version
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo Dependency installation failed. Check the network and the messages above.
    pause
    exit /b 1
)
echo.
echo Dependencies installed successfully.
python -m serial.tools.list_ports -v
pause
