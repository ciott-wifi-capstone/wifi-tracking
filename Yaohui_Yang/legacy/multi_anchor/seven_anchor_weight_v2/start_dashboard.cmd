@echo off
cd /d "%~dp0"
python dashboard_7anchor_epoch.py --port COM5
if errorlevel 1 pause
