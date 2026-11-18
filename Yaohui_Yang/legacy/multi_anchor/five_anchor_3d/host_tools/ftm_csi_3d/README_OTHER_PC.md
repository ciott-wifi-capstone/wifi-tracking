# Run the mobile ESP32-S3 on another Windows computer

## Requirements

- Windows 10/11 64-bit;
- 64-bit Python 3.11 with **Add Python to PATH** selected during installation;
- the five Anchor boards powered on;
- the already-flashed mobile board connected by a data-capable USB cable.

## First use

1. Double-click `install_dependencies.cmd` while connected to the internet.
2. Double-click `list_serial_ports.cmd` and find the USB serial port of the mobile board.
3. If it is COM7, double-click `start_live_dashboard.cmd`.
4. If it is another port, open Command Prompt in this folder and run, for example:

   ```bat
   start_live_dashboard.cmd COM8
   ```

The program automatically creates a timestamped session under:

```text
results\ftm_csi_3d_live\session-YYYYMMDD-HHMMSS
```

## Included calibration

`anchors_3d.json` contains the current A1-A5 coordinates and the experimental six-point calibration profile. The uncalibrated configuration is retained as `anchors_3d.before_preliminary_6point_calibration.json`.

## If the board does not appear

- try another USB data cable and USB port;
- press RESET once;
- close ESP-IDF Monitor and any serial terminal;
- run `list_serial_ports.cmd` again;
- Bluetooth COM ports are not the mobile board.

The precompiled firmware in `firmware_tag_flash` is only needed if the mobile board must be reflashed.
