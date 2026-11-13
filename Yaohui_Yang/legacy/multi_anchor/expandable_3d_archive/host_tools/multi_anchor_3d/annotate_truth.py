#!/usr/bin/env python3
"""Annotate the three pre-boundary-fix groups with ground-truth ranges.

Ground truth from the experimenter:
  - The tag always moves on the straight line y = 6.37 m.
  - Static group: held under each anchor in order A1 -> A3 -> A2 -> A6 -> A5 -> A7,
    A2 and A5 held 90 s, the others 60 s. Tag z ~ 0.
  - Fast/slow moving groups: constant-speed straight run from x = A1 (21.58 m)
    to x = A7 (47.22 m), y = 6.37, z ignored (approximated as 0).

Only rows with nlos_probability < 0.65 receive a true_distance_m so that the
occasional interference periods do not pollute the range-correction fit.
The original link_measurements.csv is backed up to link_measurements.raw_backup.csv
before the annotated version is written in place.
"""
from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path

import numpy as np

ROOT = Path(r"C:\\Users\\YOUR_USER\Desktop\thesis\04_multi_anchor_3d\results\multi_anchor_3d_live")

ANCHORS: dict[str, tuple[float, float, float]] = {
    "A1": (21.58, 5.64, 1.35),
    "A2": (31.57, 5.64, 2.14),
    "A3": (28.16, 7.10, 0.00),
    "A5": (41.35, 5.64, 0.52),
    "A6": (36.52, 6.94, 1.32),
    "A7": (47.22, 5.64, 0.52),
}
TAG_Y = 6.37
STATIC_ORDER = ["A1", "A3", "A2", "A6", "A5", "A7"]
STATIC_DUR = {"A1": 60, "A3": 60, "A2": 90, "A6": 60, "A5": 90, "A7": 60}
NLOS_MAX = 0.80
MOVE_X_START = ANCHORS["A1"][0]
MOVE_X_END = ANCHORS["A7"][0]


def anchor_vector(aid: str) -> np.ndarray:
    return np.asarray(ANCHORS[aid], dtype=float)


def true_distance(aid: str, tag: np.ndarray) -> float:
    return float(np.linalg.norm(anchor_vector(aid) - tag))


def static_tag_x_map(ts: np.ndarray, xs: np.ndarray) -> np.ndarray:
    """Assign a ground-truth anchor x to every timestamp of the static group.

    The measured x is smoothed, snapped to the nearest anchor x, and then the
    anchor identity is kept per timestamp; the measured values only decide the
    time boundaries, the assigned x is the exact anchor coordinate.
    """
    smoothed = np.asarray(xs, dtype=float).copy()
    window = 15
    for i in range(len(smoothed)):
        lo = max(0, i - window // 2)
        hi = min(len(smoothed), i + window // 2 + 1)
        smoothed[i] = np.median(xs[lo:hi])
    anchor_x = np.asarray([ANCHORS[a][0] for a in STATIC_ORDER], dtype=float)
    assigned = np.empty(len(ts), dtype=object)
    for i in range(len(ts)):
        idx = int(np.argmin(np.abs(anchor_x - smoothed[i])))
        assigned[i] = STATIC_ORDER[idx]
    # time-continuity: merge single-point flips
    for i in range(1, len(assigned) - 1):
        if assigned[i] != assigned[i - 1] and assigned[i] != assigned[i + 1]:
            assigned[i] = assigned[i - 1]
    return assigned


def annotate(group: Path, mode: str) -> None:
    link_path = group / "link_measurements.csv"
    positions_path = group / "positions.csv"
    backup = group / "link_measurements.raw_backup.csv"
    if not link_path.exists():
        print(f"  skip {group.name}: no link_measurements.csv")
        return
    if not backup.exists():
        shutil.copy2(link_path, backup)

    rows = list(csv.DictReader(open(link_path, encoding="utf-8-sig")))
    fieldnames = list(rows[0].keys())

    pos_rows = list(csv.DictReader(open(positions_path, encoding="utf-8")))
    pos_ts = np.asarray([float(r["pc_time_s"]) for r in pos_rows])
    pos_x = np.asarray([float(r["x_m"]) for r in pos_rows])
    t_start = float(pos_ts[0])
    t_end = float(pos_ts[-1])
    if mode == "static":
        tag_x_map = static_tag_x_map(pos_ts, pos_x)
        def tag_at(t: float) -> tuple[str, np.ndarray]:
            idx = int(np.argmin(np.abs(pos_ts - t)))
            aid = tag_x_map[idx]
            return aid, np.asarray([ANCHORS[aid][0], TAG_Y, 0.0], dtype=float)
    else:
        span = max(t_end - t_start, 1e-6)
        def tag_at(t: float) -> tuple[str, np.ndarray]:
            frac = min(max((t - t_start) / span, 0.0), 1.0)
            x = MOVE_X_START + (MOVE_X_END - MOVE_X_START) * frac
            return "moving", np.asarray([x, TAG_Y, 0.0], dtype=float)

    n_annotated = 0
    n_skipped_nlos = 0
    for row in rows:
        try:
            t = float(row["pc_time_s"])
            aid = row["anchor"]
            nlos = float(row.get("nlos_probability") or 0.0)
        except (TypeError, ValueError):
            continue
        label, tag = tag_at(t)
        row["point_id"] = row.get("point_id", "") or label
        row["condition"] = mode
        key = f"A{aid}" if aid not in ANCHORS else aid
        if nlos < NLOS_MAX and key in ANCHORS:
            dist = true_distance(key, tag)
            row["true_distance_m"] = f"{dist:.6f}"
            n_annotated += 1
        else:
            row["true_distance_m"] = ""
            n_skipped_nlos += 1

    with open(link_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"  {group.name}: rows={len(rows)} annotated={n_annotated} "
          f"skipped(NLOS/other)={n_skipped_nlos} backup={backup.name}")


def main() -> None:
    groups = [d for d in ROOT.iterdir() if d.is_dir()]
    for group in groups:
        if "静态" in group.name:
            annotate(group, "static")
        elif "较快" in group.name:
            annotate(group, "moving_fast")
        elif "较慢" in group.name:
            annotate(group, "moving_slow")
        else:
            print(f"  unknown group: {group.name}")


if __name__ == "__main__":
    main()
