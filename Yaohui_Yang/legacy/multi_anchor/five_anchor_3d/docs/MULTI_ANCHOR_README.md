# ESP32-S3 multi-anchor FTM prototype

This version uses one tag and 3-8 responder anchors. Four anchors are
recommended for 2-D positioning. Existing projects are unchanged.

## Build each anchor

Open firmware/ftm_only_baseline/responder, run idf.py set-target esp32s3, then
idf.py menuconfig. Under Multi-anchor responder, assign a unique ID
from 1 to 4. Keep all anchors on channel 6 and HT40 for the first test.
Build and flash each board separately.

Each anchor advertises FTM_A1, FTM_A2, etc. Do not flash two boards
with the same ID.

## Build the tag

Open firmware/ftm_only_baseline/tag, set the ESP32-S3 target, and enable Wi-Fi FTM
and FTM initiator support. The tag scans for the anchor prefix and
performs FTM directly by BSSID, without associating or waiting for DHCP.
Default FTM frame count is 16.

The main serial record has these fields:

    MRANGE,round,esp_us,anchor,status,rtt_raw_ns,dist_est_cm,corrected_cm,filtered_cm,rssi,entries,ok,fail

## Run the host localizer

Copy host_tools/ftm_only_baseline/anchors.example.json to
host_tools/ftm_only_baseline/anchors.json and enter
the measured coordinates of all anchors in metres. Then run:

    python -m pip install numpy pyserial
    python host_tools/ftm_only_baseline/multi_anchor_localizer.py --config host_tools/ftm_only_baseline/anchors.json --port COM7

The solver uses median link filtering plus iteratively reweighted
Gauss-Newton multilateration. RSSI, FTM report count, temporal spread,
and Huber residuals affect each anchor's weight.

## First-version limitations

- scale and bias_m are host-side per-anchor calibration values.
- Anchor coordinates must be measured accurately.
- Use four anchors surrounding the test area; avoid a collinear layout.
- This is the multi-anchor baseline. CSI/CIR correction, quick
  calibration, EKF, and visualization should be evaluated as separate
  layers so their contributions can be measured.
