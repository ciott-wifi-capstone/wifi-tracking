from __future__ import annotations
import json
from pathlib import Path
import numpy as np
from localizer_core import Measurement, StrictEpochLocalizer

HERE = Path(__file__).resolve().parent
config = json.loads((HERE / "anchors_7.json").read_text(encoding="utf-8"))
out = HERE / "_selftest_output"
loc = StrictEpochLocalizer(config, out)
truth = np.array([31.5, 10.2])
rid = 1
start = 1_000_000
end = 1_260_000
loc.start_epoch(rid, start)
rng = np.random.default_rng(7)
for idx, aid in enumerate(sorted(loc.anchors, key=int)):
    a = loc.anchors[aid]
    d = float(np.linalg.norm(truth - np.array([a["x_m"], a["y_m"]])))
    # emulate wall multipath on outside anchors, especially A1
    if aid == "1": bias = 6.0
    elif aid in {"3", "4", "5"}: bias = 1.8 + rng.normal(0, 0.2)
    else: bias = rng.normal(0.0, 0.15)
    cm = int(round((d + bias) * 100))
    indoor = aid in {"2", "6", "8"}
    m = Measurement(
        rid, start + 20_000 + idx * 30_000, aid, 0, max(1, cm // 15),
        cm / 100.0, cm / 100.0, cm / 100.0,
        -52 if indoor else -70, 6, 1,
        -53 if indoor else -72, 1.3 if indoor else 4.5,
        100.0, 10.0, 0.2, 0, 10, 1, 0,
        f"00:00:00:00:00:{int(aid):02x}", -50,
    )
    loc.ingest(m)
sol = loc.finalize(rid, start, end, end-start, 7)
loc.close()
if sol is None:
    raise SystemExit("SELFTEST FAILED: no solution")
err = float(np.linalg.norm(sol["filtered"] - truth))
print(f"SELFTEST truth={truth.tolist()} estimate={sol['filtered'].round(3).tolist()} error={err:.3f} m")
print("weights:", {r["id"]: round(r["weight"], 3) for r in sol["rows"]})
print("prior:", {r["id"]: round(r["prior_nlos"], 3) for r in sol["rows"]})
print("NLOS:", {r["id"]: round(r["nlos"], 3) for r in sol["rows"]})
print("LOO residual:", {r["id"]: round(r["loo_residual"], 3) for r in sol["rows"]})
if err >= 3.0:
    raise SystemExit(3)
