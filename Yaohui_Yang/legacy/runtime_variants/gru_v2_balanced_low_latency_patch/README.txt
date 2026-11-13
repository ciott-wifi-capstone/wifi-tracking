GRU V2 Balanced Low-Latency Patch

Base: first/stable GRU V2 live version.

Changes:
- trained 20-frame GRU is unchanged and NOT retrained
- all ready Anchors are inferred in one PyTorch batch per MROUND
- conservative Kalman display tracker replaced by bounded alpha-beta tracker
- next WLS solve uses previous RAW WLS position, not filtered yellow point
- dashboard refresh 100 ms -> 50 ms
- max innovation 3.5 m prevents the wild jumps seen in the ultra-fast patch

Replace:
  fused_3d_mapper.py
  fast_dashboard.py
  anchors_3d.json
  temporal_fusion_model.py

Keep:
  temporal_fusion_v2.pt
  temporal_fusion_v2_best.pt

Run:
  python -m py_compile .\fused_3d_mapper.py
  python -m py_compile .\fast_dashboard.py
  python .\fast_dashboard.py --port COM3

Tuning:
- still a little slow: alpha 0.82 -> 0.88
- too jittery: alpha 0.82 -> 0.75
