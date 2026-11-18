#!/usr/bin/env python3
"""Fair comparison of range-correction methods with held-out locations.

Methods (all trained on the SAME training locations, evaluated on the SAME
held-out locations):
  none    - raw baseline distance
  pav     - per-anchor monotonic piecewise residual (refit on train only)
  mlp     - multi-feature MLP (distance + CSI/RSSI + anchor one-hot)
  mcdrop  - MC-Dropout MLP with per-sample uncertainty

Validation is by calibration point: the A5 static point and the slow moving
run are never seen during training.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1] / "results" / "multi_anchor_3d_live"

import sys
sys.path.insert(0, str(HERE))
from fit_range_correction import fit_anchor  # noqa: E402
from range_correction import NeuralCorrector  # noqa: E402

FEATURES = ["baseline_distance_m"] + NeuralCorrector.FEATURES
ANCHOR_IDS = NeuralCorrector.ANCHOR_IDS
HOLD_OUT_POINTS = {"A5|static", "moving|moving_slow"}
SEED = 20260908


def load_rows() -> list[dict]:
    rows: list[dict] = []
    for group in sorted([d for d in ROOT.iterdir() if d.is_dir()]):
        path = group / "link_measurements.csv"
        if not path.exists():
            continue
        for row in csv.DictReader(open(path, encoding="utf-8-sig")):
            try:
                base = float(row["baseline_distance_m"])
                truth = float(row["true_distance_m"])
            except (TypeError, ValueError):
                continue
            if base <= 0.0 or not np.isfinite(truth):
                continue
            anchor = int(row["anchor"])
            point = f"{row.get('point_id') or 'unknown'}|{row.get('condition') or ''}"
            feats = []
            for name in FEATURES:
                try:
                    value = float(row.get(name) or 0.0)
                except (TypeError, ValueError):
                    value = 0.0
                feats.append(value if np.isfinite(value) else 0.0)
            feats += [1.0 if anchor == aid else 0.0 for aid in ANCHOR_IDS]
            rows.append({
                "anchor": anchor,
                "point": point,
                "features": np.asarray(feats, dtype=float),
                "baseline": base,
                "truth": truth,
            })
    return rows


def split(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    train = [r for r in rows if r["point"] not in HOLD_OUT_POINTS]
    valid = [r for r in rows if r["point"] in HOLD_OUT_POINTS]
    return train, valid


def report_mae(tag: str, rows: list[dict], corrected: np.ndarray) -> None:
    truth = np.asarray([r["truth"] for r in rows])
    err = np.abs(corrected - truth)
    print(f"  {tag:<12} MAE = {float(err.mean()):.3f} m   median = {float(np.median(err)):.3f} m")


def train_pav(train_rows: list[dict]) -> dict[int, dict]:
    """Per-anchor PAV refit using only the training locations (fair baseline)."""
    agg: dict[tuple[int, str], list] = defaultdict(list)
    for r in train_rows:
        agg[(r["anchor"], r["point"])].append((r["baseline"], r["truth"]))
    per_anchor: dict[int, list] = defaultdict(list)
    for (anchor, _point), pairs in agg.items():
        arr = np.asarray(pairs, dtype=float)
        per_anchor[anchor].append({
            "measured": float(np.median(arr[:, 0])),
            "truth": float(np.median(arr[:, 1])),
            "weight": float(arr.shape[0]),
        })
    models: dict[int, dict] = {}
    for anchor, points in per_anchor.items():
        try:
            models[anchor] = fit_anchor(points, max_knots=10)
        except ValueError:
            pass
    return models


def predict_pav(models: dict[int, dict], rows: list[dict]) -> np.ndarray:
    out = []
    for r in rows:
        model = models.get(r["anchor"])
        if not model:
            out.append(r["baseline"])
            continue
        raw = np.asarray(model["raw_knots_m"], dtype=float)
        delta = np.asarray(model["delta_knots_m"], dtype=float)
        out.append(float(np.interp(r["baseline"], raw, raw + delta)))
    return np.asarray(out)


def train_mlp(train_rows: list[dict]):
    from sklearn.preprocessing import StandardScaler
    from sklearn.neural_network import MLPRegressor

    x_train = np.vstack([r["features"] for r in train_rows])
    y_train = np.asarray([r["truth"] for r in train_rows], dtype=float)
    scaler = StandardScaler().fit(x_train)
    model = MLPRegressor(
        hidden_layer_sizes=(64, 32), activation="relu", solver="adam",
        batch_size=256, learning_rate_init=1e-3, max_iter=1000,
        early_stopping=True, n_iter_no_change=40, validation_fraction=0.1,
        random_state=SEED,
    )
    model.fit(scaler.transform(x_train), y_train)
    return model, scaler


def train_mcdrop(train_rows: list[dict]):
    import torch
    import torch.nn as nn

    torch.manual_seed(SEED)
    x_train = np.vstack([r["features"] for r in train_rows])
    y_train = np.asarray([r["truth"] for r in train_rows], dtype=float)
    mu = x_train.mean(axis=0)
    sd = x_train.std(axis=0) + 1e-6

    def normalize(x: np.ndarray) -> np.ndarray:
        return (x - mu) / sd

    class DropoutMLP(nn.Module):
        def __init__(self, in_dim: int):
            super().__init__()
            self.net = nn.Sequential(
                nn.Linear(in_dim, 64), nn.ReLU(), nn.Dropout(0.10),
                nn.Linear(64, 32), nn.ReLU(), nn.Dropout(0.10),
                nn.Linear(32, 1),
            )

        def forward(self, x):
            return self.net(x)

    model = DropoutMLP(x_train.shape[1])
    xt = torch.tensor(normalize(x_train), dtype=torch.float32)
    yt = torch.tensor(y_train, dtype=torch.float32).view(-1, 1)
    optimizer = torch.optim.Adam(model.parameters(), lr=1e-3)
    loss_fn = nn.MSELoss()
    model.train()
    for _epoch in range(500):
        optimizer.zero_grad()
        loss = loss_fn(model(xt), yt)
        loss.backward()
        optimizer.step()
    model.eval()
    return model, normalize, mu, sd


def predict_mcdrop(model, normalize, rows: list[dict], samples: int = 40):
    import torch

    x = np.vstack([r["features"] for r in rows])
    xt = torch.tensor(normalize(x), dtype=torch.float32)
    model.train()  # keep dropout active so MC samples differ
    with torch.no_grad():
        preds = [model(xt).numpy()[:, 0] for _ in range(samples)]
    model.eval()
    stack = np.vstack(preds)
    return stack.mean(axis=0), stack.std(axis=0)


def main() -> None:
    rows = load_rows()
    print(f"loaded {len(rows)} rows")
    train, valid = split(rows)
    print(f"train rows={len(train)} points={sorted({r['point'] for r in train})}")
    print(f"valid rows={len(valid)} points={sorted({r['point'] for r in valid})}")

    report_mae("no-corr", valid, np.asarray([r["baseline"] for r in valid]))
    pav_models = train_pav(train)
    pav_valid = predict_pav(pav_models, valid)
    report_mae("PAV(fair)", valid, pav_valid)

    mlp_model, mlp_scaler = train_mlp(train)
    x_valid = mlp_scaler.transform(np.vstack([r["features"] for r in valid]))
    mlp_valid = mlp_model.predict(x_valid)
    report_mae("MLP", valid, mlp_valid)

    mc_model, mc_norm, mc_mu, mc_sd = train_mcdrop(train)
    mc_mean, mc_std = predict_mcdrop(mc_model, mc_norm, valid)
    report_mae("MCDrop", valid, mc_mean)
    print(f"  MCDrop mean sigma = {float(mc_std.mean()):.3f} m (per-link uncertainty)")

    # persist the MC-Dropout model for the runtime neural mode
    import torch
    checkpoint_path = HERE / "neural_correction.pt"
    torch.save({"state_dict": mc_model.net.state_dict(),
                "mu": mc_mu, "sd": mc_sd}, str(checkpoint_path))
    print(f"saved {checkpoint_path}")

    print("\nper-anchor MAE (no-corr / PAV / MLP / MCDrop):")
    for anchor in sorted({r["anchor"] for r in valid}):
        idx = [i for i, r in enumerate(valid) if r["anchor"] == anchor]
        truth = np.asarray([valid[i]["truth"] for i in idx])
        base = np.asarray([valid[i]["baseline"] for i in idx])
        print(f"  A{anchor}: {np.abs(base-truth).mean():.2f} / "
              f"{np.abs(pav_valid[idx]-truth).mean():.2f} / "
              f"{np.abs(mlp_valid[idx]-truth).mean():.2f} / "
              f"{np.abs(mc_mean[idx]-truth).mean():.2f} m")


if __name__ == "__main__":
    main()
