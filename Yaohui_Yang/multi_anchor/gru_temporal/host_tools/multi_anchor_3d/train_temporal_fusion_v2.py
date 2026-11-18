
from __future__ import annotations

import csv
import math
import random
from collections import defaultdict
from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F
from torch.utils.data import Dataset, DataLoader

from temporal_fusion_model import TemporalFusionGRU, FEATURE_NAMES, ANCHOR_IDS

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1] / "results" / "multi_anchor_3d_live"
CHECKPOINT_PATH = HERE / "temporal_fusion_v2.pt"

SEED = 20260909
SEQ_LEN = 20
TRAIN_STRIDE = 3
EVAL_STRIDE = 3
BATCH_SIZE = 256
EPOCHS = 80
LEARNING_RATE = 1e-3
PATIENCE = 12
MAX_TIME_GAP_S = 1.2

# 用完整 Session 分割，避免相邻帧泄漏。
TRAIN_SESSIONS = [
    "session-20260909-170554",
    "session-20260909-170831",
    "session-20260909-171035",
    "session-20260909-172450",
    "session-20260909-172653",
    "session-20260909-172853",
]

VALID_SESSIONS = [
    "session-20260909-173739",
    "session-20260909-174125",
]

TEST_SESSIONS = [
    "session-20260909-173934",
    "session-20260909-174317",
]

ANCHOR_TO_INDEX = {aid: i for i, aid in enumerate(ANCHOR_IDS)}


def safe_float(row, name, default=0.0):
    try:
        v = float(row.get(name, default) or default)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def read_sessions(session_names):
    rows = []

    for session_name in session_names:
        path = ROOT / session_name / "link_measurements_labeled.csv"

        if not path.exists():
            raise FileNotFoundError(
                f"找不到 {path}\n"
                f"请先运行: python .\\label_route_sessions_v2.py"
            )

        print("读取:", path)

        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)

            for row in reader:
                try:
                    truth = float(row["true_distance_m"])
                    baseline = float(row["baseline_distance_m"])
                    anchor = int(row["anchor"])
                    timestamp = float(row["pc_time_s"])
                except (KeyError, TypeError, ValueError):
                    continue

                if not (math.isfinite(truth) and math.isfinite(baseline)):
                    continue
                if baseline <= 0 or anchor not in ANCHOR_TO_INDEX:
                    continue

                features = np.asarray(
                    [safe_float(row, name) for name in FEATURE_NAMES],
                    dtype=np.float32,
                )

                rows.append({
                    "session": session_name,
                    "anchor": anchor,
                    "timestamp": timestamp,
                    "features": features,
                    "baseline": baseline,
                    "truth": truth,
                    "delta": truth - baseline,
                })

    return rows


def make_sequences(rows, stride):
    groups = defaultdict(list)

    for row in rows:
        groups[(row["session"], row["anchor"])].append(row)

    samples = []

    for (session, anchor), group in groups.items():
        group.sort(key=lambda r: r["timestamp"])

        segments = []
        current = []
        last_t = None

        for row in group:
            if last_t is not None and row["timestamp"] - last_t > MAX_TIME_GAP_S:
                if current:
                    segments.append(current)
                current = []
            current.append(row)
            last_t = row["timestamp"]

        if current:
            segments.append(current)

        for segment in segments:
            if len(segment) < SEQ_LEN:
                continue

            for end in range(SEQ_LEN - 1, len(segment), stride):
                window = segment[end - SEQ_LEN + 1:end + 1]
                last = window[-1]

                samples.append({
                    "x": np.stack([r["features"] for r in window]).astype(np.float32),
                    "anchor_idx": ANCHOR_TO_INDEX[anchor],
                    "baseline": last["baseline"],
                    "truth": last["truth"],
                    "target_delta": last["delta"],
                })

    return samples


def compute_norm(samples):
    x = np.concatenate([s["x"] for s in samples], axis=0)
    mu = x.mean(axis=0).astype(np.float32)
    std = x.std(axis=0).astype(np.float32)
    std[std < 1e-6] = 1.0
    return mu, std


class TemporalDataset(Dataset):
    def __init__(self, samples, mu, std):
        self.samples = samples
        self.mu = mu
        self.std = std

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        s = self.samples[idx]
        x = (s["x"] - self.mu) / self.std

        return (
            torch.tensor(x, dtype=torch.float32),
            torch.tensor(s["anchor_idx"], dtype=torch.long),
            torch.tensor(s["baseline"], dtype=torch.float32),
            torch.tensor(s["truth"], dtype=torch.float32),
            torch.tensor(s["target_delta"], dtype=torch.float32),
        )


@torch.no_grad()
def evaluate(model, loader, device):
    model.eval()
    corr_err = []
    raw_err = []
    sigma_all = []

    for x, anchor_idx, baseline, truth, target_delta in loader:
        x = x.to(device)
        anchor_idx = anchor_idx.to(device)
        baseline = baseline.to(device)
        truth = truth.to(device)

        out = model(x, anchor_idx)
        corrected = baseline + out["delta"]

        corr_err.append(torch.abs(corrected - truth).cpu())
        raw_err.append(torch.abs(baseline - truth).cpu())
        sigma_all.append(out["sigma"].cpu())

    corr = torch.cat(corr_err).numpy()
    raw = torch.cat(raw_err).numpy()
    sigma = torch.cat(sigma_all).numpy()

    return {
        "mae": float(np.mean(corr)),
        "median": float(np.median(corr)),
        "p90": float(np.percentile(corr, 90)),
        "raw_mae": float(np.mean(raw)),
        "sigma_mean": float(np.mean(sigma)),
    }


def make_loader(samples, mu, std, shuffle):
    ds = TemporalDataset(samples, mu, std)
    return DataLoader(ds, batch_size=BATCH_SIZE, shuffle=shuffle)


def main():
    random.seed(SEED)
    np.random.seed(SEED)
    torch.manual_seed(SEED)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    print("=" * 72)
    print("GRU V2: CSI + FTM Temporal Fusion")
    print("Device:", device)
    print("=" * 72)

    train_rows = read_sessions(TRAIN_SESSIONS)
    valid_rows = read_sessions(VALID_SESSIONS)
    test_rows = read_sessions(TEST_SESSIONS)

    train_samples = make_sequences(train_rows, TRAIN_STRIDE)
    valid_samples = make_sequences(valid_rows, EVAL_STRIDE)
    test_samples = make_sequences(test_rows, EVAL_STRIDE)

    print()
    print("Train rows/sequences:", len(train_rows), len(train_samples))
    print("Valid rows/sequences:", len(valid_rows), len(valid_samples))
    print("Test  rows/sequences:", len(test_rows), len(test_samples))

    if not train_samples or not valid_samples or not test_samples:
        raise RuntimeError("某个数据集无法形成时序样本，请检查标注文件。")

    mu, std = compute_norm(train_samples)

    train_loader = make_loader(train_samples, mu, std, True)
    valid_loader = make_loader(valid_samples, mu, std, False)
    test_loader = make_loader(test_samples, mu, std, False)

    model = TemporalFusionGRU().to(device)

    optimizer = torch.optim.AdamW(
        model.parameters(),
        lr=LEARNING_RATE,
        weight_decay=1e-4,
    )

    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
        optimizer,
        mode="min",
        factor=0.5,
        patience=4,
    )

    best_mae = float("inf")
    best_state = None
    no_improve = 0

    for epoch in range(1, EPOCHS + 1):
        model.train()
        total_loss = 0.0
        batches = 0

        for x, anchor_idx, baseline, truth, target_delta in train_loader:
            x = x.to(device)
            anchor_idx = anchor_idx.to(device)
            baseline = baseline.to(device)
            truth = truth.to(device)

            optimizer.zero_grad(set_to_none=True)

            out = model(x, anchor_idx)
            corrected = baseline + out["delta"]

            # V2：距离准确度是主目标
            loss_distance = F.smooth_l1_loss(corrected, truth, beta=1.0)

            # sigma 只作为辅助任务，学习“当前误差大概有多大”
            error_target = torch.abs(corrected.detach() - truth) + 0.05
            loss_sigma = F.smooth_l1_loss(
                torch.log(torch.clamp(out["sigma"], min=0.05)),
                torch.log(torch.clamp(error_target, min=0.05)),
                beta=0.5,
            )

            loss = loss_distance + 0.05 * loss_sigma

            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=5.0)
            optimizer.step()

            total_loss += float(loss.item())
            batches += 1

        valid = evaluate(model, valid_loader, device)
        scheduler.step(valid["mae"])
        lr = optimizer.param_groups[0]["lr"]

        print(
            f"Epoch {epoch:03d}"
            f" | Loss={total_loss/max(batches,1):.4f}"
            f" | Valid MAE={valid['mae']:.3f} m"
            f" | Median={valid['median']:.3f} m"
            f" | P90={valid['p90']:.3f} m"
            f" | Raw={valid['raw_mae']:.3f} m"
            f" | Sigma={valid['sigma_mean']:.3f} m"
            f" | LR={lr:.2e}"
        )

        if valid["mae"] < best_mae - 1e-4:
            best_mae = valid["mae"]
            best_state = {
                k: v.detach().cpu().clone()
                for k, v in model.state_dict().items()
            }
            no_improve = 0
        else:
            no_improve += 1

        if no_improve >= PATIENCE:
            print("Early stopping:", epoch)
            break

    if best_state is None:
        raise RuntimeError("训练失败，没有得到最佳模型。")

    model.load_state_dict(best_state)

    valid = evaluate(model, valid_loader, device)
    test = evaluate(model, test_loader, device)

    checkpoint = {
        "schema": 2,
        "model_type": "TemporalFusionGRU",
        "state_dict": model.state_dict(),
        "feature_names": FEATURE_NAMES,
        "anchor_ids": ANCHOR_IDS,
        "sequence_length": SEQ_LEN,
        "feature_mu": mu,
        "feature_std": std,
        "max_delta_m": model.max_delta_m,
        "train_sessions": TRAIN_SESSIONS,
        "valid_sessions": VALID_SESSIONS,
        "test_sessions": TEST_SESSIONS,
        "validation_metrics": valid,
        "test_metrics": test,
    }

    torch.save(checkpoint, CHECKPOINT_PATH)

    print()
    print("=" * 72)
    print("Training Finished")
    print("=" * 72)
    print(
        f"VALID: Raw={valid['raw_mae']:.3f} m -> "
        f"GRU={valid['mae']:.3f} m | "
        f"Median={valid['median']:.3f} | P90={valid['p90']:.3f}"
    )
    print(
        f"TEST : Raw={test['raw_mae']:.3f} m -> "
        f"GRU={test['mae']:.3f} m | "
        f"Median={test['median']:.3f} | P90={test['p90']:.3f}"
    )
    print("模型:", CHECKPOINT_PATH)


if __name__ == "__main__":
    main()
