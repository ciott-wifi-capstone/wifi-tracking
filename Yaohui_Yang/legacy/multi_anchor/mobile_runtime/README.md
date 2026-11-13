# ESP32-S3 five-anchor mobile runtime package

Package date: 2026-08-27

This package runs the mobile ESP32-S3 FTM + CSI dashboard on another Windows computer. It does not contain historical measurement sessions or ESP-IDF build caches.

## Quick start

1. Install 64-bit Python 3.11 and select **Add Python to PATH**.
2. Double-click `INSTALL_FIRST.cmd` once while connected to the internet.
3. Connect the mobile ESP32-S3 with a USB data cable and power A1-A5.
4. Double-click `LIST_PORTS.cmd` to confirm the USB COM port.
5. For COM7, double-click `START_HERE.cmd`.
6. For another port, open Command Prompt in this directory and run, for example:

   ```bat
   START_HERE.cmd COM8
   ```

Closing the dashboard safely ends the current session. New data is stored under `results\ftm_csi_3d_live` inside this package.

## Current calibration warning

The included `anchors_3d.json` uses the experimental six-point calibration profile created on the original computer. It improved the preliminary cross-validated mean 3-D error from about 7.26 m to 1.54 m, but A1, A2 and A5 remain weakly correlated with ground truth. It is suitable for continued testing, not final thesis accuracy claims.

The original uncalibrated configuration is included for rollback.

## Reflashing the mobile board

The mobile board is already flashed in normal use. Only use `firmware_tag_flash\FLASH_MOBILE_BOARD.cmd` if its firmware must be restored. Flashing replaces the current application on the connected ESP32-S3.

More detail: `host_tools\ftm_csi_3d\README_OTHER_PC.md`.
