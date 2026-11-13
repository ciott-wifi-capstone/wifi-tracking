GRU V2 ultra-low-latency live patch

This patch DOES NOT replace temporal_fusion_v2.pt.

Main latency reductions:
1. GRU warm start: each Anchor is ready after its first valid measurement instead of waiting for 20 readings.
2. Fresh-link window: max_measurement_age_s = 0.9 s, reducing stale ranges that drag the position behind the moving Tag.
3. WLS motion prior: 0.35 -> 0.03, so raw WLS can move quickly instead of being anchored to the previous position.
4. Kalman response: accel_std=8.0, lower R weights, wider gate.
5. GUI refresh: 100 ms -> 30 ms.
6. Serial read timeout: 120 ms -> 30 ms.
7. Disk I/O: flush every 5 rounds, state/radio output every 10 rounds; radio voxel updates disabled during live positioning.

Tradeoff: faster response may introduce more jitter. If too jittery, first lower accel_std_mps2 from 8.0 to 5.0.

Important physical limit:
This patch speeds the PC-side pipeline. It cannot shorten the ESP32 firmware FTM polling/burst cycle. If the measured range itself is still slow after this patch, firmware FTM parameters must be changed and reflashed.
