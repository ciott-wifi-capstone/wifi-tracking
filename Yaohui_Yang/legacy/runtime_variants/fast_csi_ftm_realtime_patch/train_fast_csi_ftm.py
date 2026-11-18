#!/usr/bin/env python3
"""Train the fast CSI+FTM residual-regression model from the 10 labelled runs.

Requires only NumPy.  No sklearn and no PyTorch are used.
"""
from __future__ import annotations

import csv
import json
import math
from collections import defaultdict, deque
from pathlib import Path

import numpy as np

from fast_csi_ftm import FEATURE_NAMES, build_feature_vector

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1] / "results" / "multi_anchor_3d_live"
OUTPUT = HERE / "fast_csi_ftm_model.json"

TRAIN_SESSIONS = [
    "session-20260909-170554", "session-20260909-170831", "session-20260909-171035",
    "session-20260909-172450", "session-20260909-172653", "session-20260909-172853",
]
VALID_SESSIONS = ["session-20260909-173739", "session-20260909-174125"]
TEST_SESSIONS = ["session-20260909-173934", "session-20260909-174317"]
ANCHORS = [1, 2, 3, 5, 6, 7]


def safe_float(row, key, default=0.0):
    try:
        value = float(row.get(key, default) or default)
        return value if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def past_spread(history: deque[float]) -> float:
    if len(history) < 3:
        return 0.5
    arr = np.asarray(history, dtype=float)
    return max(0.08, 1.4826 * float(np.median(np.abs(arr - np.median(arr)))))


def load_sessions(names):
    records = []
    for session in names:
        path = ROOT / session / "link_measurements_labeled.csv"
        if not path.exists():
            raise FileNotFoundError(f"Missing {path}; run the route-label script first")
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        groups = defaultdict(list)
        for row in rows:
            try:
                aid = int(row["anchor"])
                t = float(row["pc_time_s"])
                baseline = float(row["baseline_distance_m"])
                truth = float(row["true_distance_m"])
            except (KeyError, TypeError, ValueError):
                continue
            if aid not in ANCHORS or baseline <= 0 or not math.isfinite(truth):
                continue
            groups[aid].append((t, row, baseline, truth))

        for aid, items in groups.items():
            items.sort(key=lambda x: x[0])
            history = deque(maxlen=15)
            previous_nlos = 0.5
            for t, row, baseline, truth in items:
                features = {
                    "ftm_rssi": safe_float(row, "ftm_rssi"),
                    "csi_packets": safe_float(row, "csi_packets"),
                    "csi_rssi_mean": safe_float(row, "csi_rssi_mean"),
                    "csi_rssi_std": safe_float(row, "csi_rssi_std"),
                    "csi_power_mean": safe_float(row, "csi_power_mean"),
                    "csi_roughness": safe_float(row, "csi_roughness"),
                    "prev_nlos": previous_nlos,
                    "past_spread": past_spread(history),
                }
                x = build_feature_vector(baseline, features)
                records.append({
                    "session": session, "anchor": aid, "x": x,
                    "baseline": baseline, "truth": truth,
                    "delta": truth - baseline,
                })
                history.append(baseline)
                previous_nlos = safe_float(row, "nlos_probability", previous_nlos)
    return records


def robust_ridge(x, y, lam=0.5, huber_delta=1.5, iterations=15):
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    mu = x.mean(axis=0)
    sd = x.std(axis=0)
    sd[sd < 1e-6] = 1.0
    z = (x - mu) / sd
    a = np.column_stack([np.ones(len(z)), z])
    w = np.ones(len(y), dtype=float)
    reg = np.eye(a.shape[1], dtype=float) * lam
    reg[0, 0] = 0.0
    beta = np.zeros(a.shape[1], dtype=float)
    for _ in range(iterations):
        sw = np.sqrt(w)
        aw = a * sw[:, None]
        yw = y * sw
        beta = np.linalg.solve(aw.T @ aw + reg, aw.T @ yw)
        residual = y - a @ beta
        absolute = np.abs(residual)
        w = np.where(absolute <= huber_delta, 1.0,
                     huber_delta / np.maximum(absolute, 1e-6))
    return {"mu": mu.tolist(), "sd": sd.tolist(), "beta": beta.tolist()}


def predict_block(block, x):
    mu = np.asarray(block["mu"], dtype=float)
    sd = np.asarray(block["sd"], dtype=float)
    beta = np.asarray(block["beta"], dtype=float)
    z = (np.asarray(x, dtype=float) - mu) / np.where(sd < 1e-6, 1.0, sd)
    return float(beta[0] + beta[1:] @ z)


def evaluate(records, anchor_models):
    raw_errors, corrected_errors, predicted_sigmas = [], [], []
    for r in records:
        block = anchor_models[str(r["anchor"])]
        delta = float(np.clip(predict_block(block["correction"], r["x"]), -30.0, 30.0))
        corrected = max(0.05, r["baseline"] + delta)
        sigma = math.exp(float(np.clip(predict_block(block["sigma"], r["x"]), -4.0, 4.0))) - 0.25
        sigma = float(np.clip(sigma, 0.25, 8.0))
        raw_errors.append(abs(r["baseline"] - r["truth"]))
        corrected_errors.append(abs(corrected - r["truth"]))
        predicted_sigmas.append(sigma)
    raw = np.asarray(raw_errors)
    err = np.asarray(corrected_errors)
    sig = np.asarray(predicted_sigmas)
    return {
        "samples": int(len(err)),
        "raw_mae_m": float(raw.mean()),
        "corrected_mae_m": float(err.mean()),
        "median_m": float(np.median(err)),
        "p90_m": float(np.percentile(err, 90)),
        "mean_predicted_sigma_m": float(sig.mean()),
    }


def main():
    train = load_sessions(TRAIN_SESSIONS)
    valid = load_sessions(VALID_SESSIONS)
    test = load_sessions(TEST_SESSIONS)
    anchors = {}

    # Correction model per Anchor.
    for aid in ANCHORS:
        rows = [r for r in train if r["anchor"] == aid]
        x = np.stack([r["x"] for r in rows])
        y = np.asarray([r["delta"] for r in rows], dtype=float)
        anchors[str(aid)] = {"correction": robust_ridge(x, y, lam=0.5, huber_delta=1.5)}

    # Fit expected absolute post-correction error.  This becomes a cheap WLS weight.
    for aid in ANCHORS:
        rows = [r for r in train if r["anchor"] == aid]
        corr_block = anchors[str(aid)]["correction"]
        x = np.stack([r["x"] for r in rows])
        errors = []
        for r in rows:
            delta = float(np.clip(predict_block(corr_block, r["x"]), -30.0, 30.0))
            errors.append(abs(max(0.05, r["baseline"] + delta) - r["truth"]))
        target = np.log(np.asarray(errors, dtype=float) + 0.25)
        anchors[str(aid)]["sigma"] = robust_ridge(x, target, lam=3.0,
                                                    huber_delta=0.5, iterations=12)

    document = {
        "schema": 1,
        "trained": True,
        "method": "per_anchor_huber_ridge_residual_plus_sigma",
        "feature_names": FEATURE_NAMES,
        "max_abs_delta_m": 30.0,
        "sigma_floor_m": 0.25,
        "train_sessions": TRAIN_SESSIONS,
        "valid_sessions": VALID_SESSIONS,
        "test_sessions": TEST_SESSIONS,
        "anchors": anchors,
    }
    document["validation_metrics"] = evaluate(valid, anchors)
    document["test_metrics"] = evaluate(test, anchors)
    OUTPUT.write_text(json.dumps(document, indent=2), encoding="utf-8")

    v = document["validation_metrics"]
    t = document["test_metrics"]
    print(f"VALID raw={v['raw_mae_m']:.3f} m -> fast={v['corrected_mae_m']:.3f} m "
          f"median={v['median_m']:.3f} p90={v['p90_m']:.3f}")
    print(f"TEST  raw={t['raw_mae_m']:.3f} m -> fast={t['corrected_mae_m']:.3f} m "
          f"median={t['median_m']:.3f} p90={t['p90_m']:.3f}")
    print("Saved:", OUTPUT)


if __name__ == "__main__":
    main()
