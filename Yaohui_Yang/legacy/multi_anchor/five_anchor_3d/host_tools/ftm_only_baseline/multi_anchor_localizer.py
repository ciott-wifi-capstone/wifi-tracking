#!/usr/bin/env python3
"""Real-time robust 2-D localizer for multi_anchor_tag MRANGE output."""
from __future__ import annotations
import argparse, json, math, time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
import numpy as np
import serial

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = Path(__file__).resolve().with_name("anchors.json")
DEFAULT_LOG = PROJECT_ROOT / "data" / "live_logs" / "multi_anchor_positions.csv"

@dataclass
class Link:
    distance_m: float = math.nan
    rssi: int = -127
    entries: int = 0
    timestamp: float = 0.0
    history: deque = field(default_factory=lambda: deque(maxlen=7))

def robust_position(anchors, links, max_age, previous=None):
    now = time.monotonic()
    rows = []
    for aid, cfg in anchors.items():
        link = links.get(aid)
        if not link or now - link.timestamp > max_age or not link.history:
            continue
        values = np.asarray(link.history, dtype=float)
        distance = float(np.median(values))
        spread = max(0.15, 1.4826 * float(np.median(np.abs(values - distance))))
        quality = min(1.0, link.entries / 16.0) * np.clip((link.rssi + 95) / 45, 0.1, 1.0)
        rows.append((float(cfg["x_m"]), float(cfg["y_m"]), distance,
                     quality / (spread * spread + 0.05), aid))
    if len(rows) < 3:
        return None
    p = (np.mean([[r[0], r[1]] for r in rows], axis=0) if previous is None
         else np.asarray(previous, dtype=float).copy())
    for _ in range(12):
        jac, residuals, weights = [], [], []
        for ax, ay, distance, base_weight, _ in rows:
            delta = p - np.array([ax, ay])
            predicted = max(float(np.linalg.norm(delta)), 1e-6)
            residual = predicted - distance
            huber = 1.0 if abs(residual) <= 1.0 else 1.0 / abs(residual)
            jac.append(delta / predicted); residuals.append(residual)
            weights.append(base_weight * huber)
        j, r, w = np.asarray(jac), np.asarray(residuals), np.asarray(weights)
        h = j.T @ (w[:, None] * j) + np.eye(2) * 1e-5
        step = np.linalg.solve(h, j.T @ (w * r))
        p -= step
        if np.linalg.norm(step) < 1e-4:
            break
    final = [float(np.linalg.norm(p - [x, y]) - d) for x, y, d, _, _ in rows]
    return p, float(np.sqrt(np.mean(np.square(final)))), rows

def parse_mrange(line):
    parts = line.strip().split(",")
    if len(parts) != 13 or parts[0] != "MRANGE":
        return None
    return {"round": int(parts[1]), "anchor": str(int(parts[3])),
            "status": int(parts[4]), "filtered_m": int(parts[8]) / 100.0,
            "rssi": int(parts[9]), "entries": int(parts[10])}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--port")
    ap.add_argument("--log", default=str(DEFAULT_LOG))
    args = ap.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    anchors, links = config["anchors"], {}
    port = args.port or config["serial_port"]
    baud = int(config.get("baudrate", 115200))
    max_age = float(config.get("max_measurement_age_s", 3.0))
    previous = current_round = None
    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    new_log = not log_path.exists()
    log = log_path.open("a", encoding="utf-8", buffering=1)
    if new_log:
        log.write("pc_time_s,round,x_m,y_m,residual_rms_m,anchor_count\n")
    print(f"Listening on {port} at {baud} baud")
    with serial.Serial(port, baud, timeout=1) as ser:
        while True:
            m = parse_mrange(ser.readline().decode("utf-8", errors="ignore").strip())
            if not m or m["status"] != 0 or m["anchor"] not in anchors:
                continue
            aid, cfg = m["anchor"], anchors[m["anchor"]]
            distance = float(cfg.get("scale", 1.0)) * m["filtered_m"] + float(cfg.get("bias_m", 0.0))
            if not 0.05 < distance < 100:
                continue
            link = links.setdefault(aid, Link())
            link.distance_m, link.rssi = distance, m["rssi"]
            link.entries, link.timestamp = m["entries"], time.monotonic()
            link.history.append(distance)
            if current_round is None:
                current_round = m["round"]
            if m["round"] == current_round:
                continue
            solution = robust_position(anchors, links, max_age, previous)
            current_round = m["round"]
            if solution is None:
                continue
            p, rms, used = solution
            previous = p
            print(f"POS round={current_round} x={p[0]:.3f} y={p[1]:.3f} "
                  f"residual={rms:.3f}m anchors={len(used)}")
            log.write(f"{time.time():.6f},{current_round},{p[0]:.6f},{p[1]:.6f},"
                      f"{rms:.6f},{len(used)}\n")

if __name__ == "__main__":
    main()
