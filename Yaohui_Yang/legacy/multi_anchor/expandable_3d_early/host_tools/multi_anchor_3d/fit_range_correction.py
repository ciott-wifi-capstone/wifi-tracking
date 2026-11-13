#!/usr/bin/env python3
"""Fit per-anchor monotonic range correction from labelled dashboard exports."""
from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

HERE = Path(__file__).resolve().parent
DEFAULT_RESULTS = HERE.parents[1] / "results" / "multi_anchor_3d_live"
DEFAULT_MODEL = HERE / "range_correction.json"


def isotonic_increasing(values: np.ndarray, weights: np.ndarray) -> np.ndarray:
    """Weighted pool-adjacent-violators regression."""
    blocks: list[list[float]] = []
    for index, (value, weight) in enumerate(zip(values, weights)):
        blocks.append([float(index), float(index), float(value), float(weight)])
        while len(blocks) >= 2 and blocks[-2][2] > blocks[-1][2]:
            right = blocks.pop()
            left = blocks.pop()
            total_weight = left[3] + right[3]
            mean = (left[2] * left[3] + right[2] * right[3]) / total_weight
            blocks.append([left[0], right[1], mean, total_weight])
    result = np.empty(values.size, dtype=float)
    for start, end, value, _weight in blocks:
        result[int(start):int(end) + 1] = value
    return result


def read_aggregates(root: Path) -> tuple[dict[str, list[dict[str, float]]], list[Path]]:
    files = [root] if root.is_file() else sorted(root.rglob("link_measurements.csv"))
    samples: dict[tuple[str, str, str, str, str], list[tuple[float, float]]] = defaultdict(list)
    for path in files:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            required = {"anchor", "baseline_distance_m", "true_distance_m"}
            if not required.issubset(reader.fieldnames or []):
                continue
            for row in reader:
                try:
                    measured = float(row["baseline_distance_m"])
                    truth = float(row["true_distance_m"])
                except (TypeError, ValueError):
                    continue
                if not (math.isfinite(measured) and math.isfinite(truth)
                        and measured > 0.0 and truth > 0.0):
                    continue
                key = (str(path.parent), row["anchor"], row.get("point_id", ""),
                       row.get("condition", ""), row.get("repeat", ""))
                samples[key].append((measured, truth))

    grouped: dict[str, list[dict[str, float]]] = defaultdict(list)
    for key, values in samples.items():
        _session, anchor, _point, _condition, _repeat = key
        array = np.asarray(values, dtype=float)
        grouped[anchor].append({
            "measured": float(np.median(array[:, 0])),
            "truth": float(np.median(array[:, 1])),
            "weight": float(max(1, array.shape[0])),
        })
    return grouped, files


def choose_knots(count: int, maximum: int) -> np.ndarray:
    if count <= maximum:
        return np.arange(count, dtype=int)
    return np.unique(np.rint(np.linspace(0, count - 1, maximum)).astype(int))


def fit_anchor(points: list[dict[str, float]], max_knots: int) -> dict[str, Any]:
    points = sorted(points, key=lambda item: item["measured"])
    x = np.asarray([item["measured"] for item in points], dtype=float)
    y = np.asarray([item["truth"] for item in points], dtype=float)
    weights = np.asarray([item["weight"] for item in points], dtype=float)
    fitted = isotonic_increasing(y, weights)
    indices = choose_knots(x.size, max_knots)
    knot_x = x[indices]
    knot_y = fitted[indices]

    # np.interp expects strictly useful x coordinates. Merge exact duplicate medians.
    unique_x: list[float] = []
    unique_y: list[float] = []
    for measured, corrected in zip(knot_x, knot_y):
        if unique_x and abs(measured - unique_x[-1]) < 1e-9:
            unique_y[-1] = (unique_y[-1] + float(corrected)) / 2.0
        else:
            unique_x.append(float(measured))
            unique_y.append(float(corrected))
    if len(unique_x) < 2:
        raise ValueError("fewer than two unique measured ranges")

    predicted = np.interp(x, unique_x, unique_y)
    raw_mae = float(np.mean(np.abs(x - y)))
    fitted_mae = float(np.mean(np.abs(predicted - y)))
    return {
        "raw_knots_m": [round(value, 6) for value in unique_x],
        "delta_knots_m": [round(corrected - measured, 6)
                            for measured, corrected in zip(unique_x, unique_y)],
        "aggregated_point_count": int(x.size),
        "sample_weight": int(np.sum(weights)),
        "training_range_m": [round(float(np.min(x)), 6), round(float(np.max(x)), 6)],
        "training_raw_mae_m": round(raw_mae, 6),
        "training_fitted_mae_m": round(fitted_mae, 6),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, nargs="?", default=DEFAULT_RESULTS,
                        help="结果根目录或单个 link_measurements.csv")
    parser.add_argument("--output", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--min-points", type=int, default=5,
                        help="每个 Anchor 至少需要的独立标定采集数")
    parser.add_argument("--max-knots", type=int, default=10)
    args = parser.parse_args()

    grouped, files = read_aggregates(args.input)
    anchors: dict[str, Any] = {}
    skipped: dict[str, str] = {}
    for anchor, points in sorted(grouped.items(), key=lambda item: int(item[0])):
        if len(points) < args.min_points:
            skipped[anchor] = f"only {len(points)} labelled points; need {args.min_points}"
            continue
        try:
            anchors[anchor] = fit_anchor(points, max(2, args.max_knots))
        except ValueError as exc:
            skipped[anchor] = str(exc)

    document = {
        "schema": 1,
        "method": "per_anchor_monotonic_piecewise_residual",
        "trained": bool(anchors),
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "source_root": str(args.input.resolve()),
        "source_file_count": len(files),
        "minimum_points_per_anchor": args.min_points,
        "anchors": anchors,
        "skipped": skipped,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {args.output}")
    for anchor, result in anchors.items():
        print(f"A{anchor}: points={result['aggregated_point_count']} "
              f"raw_MAE={result['training_raw_mae_m']:.3f}m "
              f"fitted_MAE={result['training_fitted_mae_m']:.3f}m")
    for anchor, reason in skipped.items():
        print(f"A{anchor}: skipped ({reason})")
    if not anchors:
        raise SystemExit("No anchor model was trained. Collect labelled sessions first.")


if __name__ == "__main__":
    main()
