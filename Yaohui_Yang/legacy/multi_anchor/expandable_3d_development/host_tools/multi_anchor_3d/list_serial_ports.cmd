@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -m serial.tools.list_ports -v
echo.
echo The mobile ESP32-S3 normally appears as a USB serial device, not a Bluetooth COM port.
pause
