GRU V2 faster-response tracking patch

Purpose:
- Keep temporal_fusion_v2.pt unchanged.
- Keep GRU sequence length 20 unchanged.
- Make the final Kalman-tracked yellow point follow the latest GRU+WLS position faster.

Files:
- fused_3d_mapper.py
- anchors_3d.json

Recommended:
1. Back up your current files.
2. Copy these two files into host_tools\multi_anchor_3d and replace.
3. Run:
   python -m py_compile .\fused_3d_mapper.py
   python .\fast_dashboard.py --port COM3

Key changes:
- accel_std_mps2: 1.5 -> 4.0
- gate_mahalanobis2: 16 -> 36
- lower measurement-noise weights, so Kalman trusts current WLS fixes more
- max measurement std: 5.0 -> 2.5

Tradeoff:
- Yellow point responds faster.
- It may jitter a little more than the previous conservative mode.

If still too slow:
- accel_std_mps2 can be raised to 5.0 or 6.0.
If too jittery:
- reduce it to 3.0, or increase base_measurement_std_m from 0.15 to 0.25.
