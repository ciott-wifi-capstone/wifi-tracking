FAST CSI + FTM REAL-TIME PATCH

Purpose
-------
Replace GRU + Kalman with a very-lightweight CSI/FTM fusion path:
  FTM + current CSI -> per-Anchor robust residual regression -> corrected range
  CSI/NLOS + learned expected range error -> WLS weight -> position

There is NO PyTorch at runtime and NO Kalman tracking.
The yellow point is the current robust WLS position, so response should be close to
the original fast version.

Files to copy into host_tools\multi_anchor_3d
------------------------------------------------
- fused_3d_mapper.py
- fast_dashboard.py
- anchors_3d.json
- fast_csi_ftm.py
- fast_csi_ftm_model.json
- train_fast_csi_ftm.py   (only needed when retraining)

Do NOT delete your GRU model files; just keep them as backups.

Run
---
python -m py_compile .\fused_3d_mapper.py
python -m py_compile .\fast_csi_ftm.py
python .\fast_dashboard.py --port COM3

Retrain later
-------------
If you add more labelled 2-D routes:
python .\train_fast_csi_ftm.py
Then restart the dashboard.

Offline check on the same 10 labelled straight-line runs used for GRU V2:
- Raw test range MAE: about 7.96 m
- Fast regression test range MAE: about 1.79 m
The GRU V2 test MAE was about 1.63 m, so this fast model gives up only a small
amount of range accuracy in exchange for much lower runtime latency.
Ground truth is approximate (P-point stops + time interpolation).
