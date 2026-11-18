@echo off
chcp 65001 >nul
call "%~dp0host_tools\ftm_csi_3d\start_live_dashboard.cmd" %*
