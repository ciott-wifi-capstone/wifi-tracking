#!/usr/bin/env python3
"""Online 3-D FTM/CSI localizer and coarse radio-anomaly voxel mapper.

The map is a radio blockage likelihood map, not a reconstruction of wall geometry.
Input protocol: MFUSED schema 1 and MROUND lines from ftm_csi_tag firmware.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[1]
DEFAULT_CONFIG = HERE / "anchors_3d.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "results" / "ftm_csi_3d_live"


@dataclass
class Measurement:
    round_id: int
    timestamp: float
    distance_m: float
    ftm_rssi: float
    entries: int
    csi_packets: int
    csi_rssi_mean: float
    csi_rssi_std: float
    csi_power_mean: float
    csi_power_std: float
    csi_roughness: float
    csi_age_ms: int
    bssid: str


@dataclass
class LinkState:
    latest: Measurement | None = None
    distances: deque[float] = field(default_factory=lambda: deque(maxlen=15))
    roughness: deque[float] = field(default_factory=lambda: deque(maxlen=60))
    rssi_std: deque[float] = field(default_factory=lambda: deque(maxlen=60))
    last_nlos_probability: float = 0.5

    def update(self, measurement: Measurement) -> None:
        self.latest = measurement
        self.distances.append(measurement.distance_m)
        if measurement.csi_packets:
            self.roughness.append(measurement.csi_roughness)
            self.rssi_std.append(measurement.csi_rssi_std)


@dataclass
class Voxel:
    log_odds: float = 0.0
    observations: int = 0


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def sigmoid(value: float) -> float:
    value = clamp(value, -30.0, 30.0)
    return 1.0 / (1.0 + math.exp(-value))


def robust_z(value: float, history: deque[float]) -> float:
    if len(history) < 8:
        return 0.0
    array = np.asarray(history, dtype=float)
    median = float(np.median(array))
    mad = 1.4826 * float(np.median(np.abs(array - median)))
    return (value - median) / max(mad, 0.15)


def parse_mfused(line: str, now: float | None = None) -> dict[str, Any] | None:
    parts = line.strip().split(",")
    if len(parts) != 24 or parts[0] != "MFUSED" or parts[1] != "1":
        return None
    try:
        return {
            "round": int(parts[2]), "esp_us": int(parts[3]), "anchor": parts[4],
            "status": int(parts[5]), "rtt_raw_ns": int(parts[6]),
            "dist_est_cm": int(parts[7]), "corrected_cm": int(parts[8]),
            "filtered_cm": int(parts[9]), "ftm_rssi": float(parts[10]),
            "entries": int(parts[11]), "csi_packets": int(parts[12]),
            "csi_rssi_mean": float(parts[13]), "csi_rssi_std": float(parts[14]),
            "csi_power_mean": float(parts[15]), "csi_power_std": float(parts[16]),
            "csi_roughness": float(parts[17]), "csi_first_invalid": int(parts[18]),
            "csi_age_ms": int(parts[19]), "ok_count": int(parts[20]),
            "fail_count": int(parts[21]), "bssid": parts[22],
            "scan_rssi": int(parts[23]), "pc_monotonic": now or time.monotonic(),
        }
    except ValueError:
        return None


def distance_spread(state: LinkState) -> float:
    if len(state.distances) < 3:
        return 0.5
    values = np.asarray(state.distances, dtype=float)
    return max(0.08, 1.4826 * float(np.median(np.abs(values - np.median(values)))))


def feature_nlos_probability(state: LinkState) -> float:
    m = state.latest
    if m is None:
        return 1.0
    radio_rssi = m.csi_rssi_mean if m.csi_packets else m.ftm_rssi
    weak_signal = clamp((-68.0 - radio_rssi) / 27.0, 0.0, 1.5)
    unstable_rssi = clamp((m.csi_rssi_std - 2.0) / 7.0, 0.0, 1.5)
    rough_absolute = clamp(math.log1p(max(m.csi_roughness, 0.0)) / math.log(5.0), 0.0, 1.5)
    rough_change = clamp(robust_z(m.csi_roughness, state.roughness) / 4.0, 0.0, 1.0)
    sparse_csi = clamp((3.0 - m.csi_packets) / 3.0, 0.0, 1.0)
    volatility = clamp(distance_spread(state) / 1.2, 0.0, 1.5)
    # Deliberately conservative heuristic. Replace with a labelled classifier later.
    score = (-2.0 + 1.20 * weak_signal + 1.20 * unstable_rssi
             + 1.10 * rough_absolute + 0.80 * rough_change
             + 0.85 * sparse_csi + 0.75 * volatility)
    return sigmoid(score)


def anchor_vector(config: dict[str, Any]) -> np.ndarray:
    return np.asarray([config["x_m"], config["y_m"], config["z_m"]], dtype=float)


def solve_position(
    anchors: dict[str, dict[str, Any]],
    links: dict[str, LinkState],
    max_age: float,
    bounds: tuple[np.ndarray, np.ndarray],
    previous: np.ndarray | None,
    now: float | None = None,
) -> dict[str, Any] | None:
    now = now or time.monotonic()
    rows: list[dict[str, Any]] = []
    for aid, cfg in anchors.items():
        state = links.get(aid)
        if not state or not state.latest or now - state.latest.timestamp > max_age:
            continue
        m = state.latest
        if not (0.05 < m.distance_m < 100.0):
            continue
        rows.append({"id": aid, "anchor": anchor_vector(cfg), "distance": m.distance_m,
                     "state": state, "base_nlos": feature_nlos_probability(state)})
    if len(rows) < 4:
        return None

    positions = np.asarray([row["anchor"] for row in rows])
    if np.linalg.matrix_rank(positions[1:] - positions[0]) < 3:
        return None
    p = np.mean(positions, axis=0) if previous is None else np.asarray(previous, dtype=float).copy()
    last_hessian = np.eye(3)
    for _ in range(18):
        jacobian, residuals, weights = [], [], []
        for row in rows:
            delta = p - row["anchor"]
            predicted = max(float(np.linalg.norm(delta)), 1e-6)
            residual = predicted - row["distance"]
            # Positive range bias is a frequent NLOS symptom (residual becomes negative).
            residual_nlos = sigmoid(((-residual) - 0.55) / 0.35)
            nlos = 1.0 - (1.0 - row["base_nlos"]) * (1.0 - 0.65 * residual_nlos)
            spread = distance_spread(row["state"])
            m = row["state"].latest
            entry_quality = clamp(m.entries / 16.0, 0.15, 1.0)
            csi_quality = clamp(m.csi_packets / 4.0, 0.25, 1.0)
            huber = 1.0 if abs(residual) <= 0.7 else 0.7 / abs(residual)
            weight = ((1.0 - nlos) ** 3 * entry_quality * csi_quality
                      * huber / (spread * spread + 0.08))
            if nlos > 0.72:
                weight *= 0.02
            weight = max(1e-5, weight)
            jacobian.append(delta / predicted)
            residuals.append(residual)
            weights.append(weight)
            row["nlos"] = nlos
            row["residual"] = residual
            row["weight"] = weight
        j = np.asarray(jacobian)
        r = np.asarray(residuals)
        w = np.asarray(weights)
        hessian = j.T @ (w[:, None] * j) + np.eye(3) * 1e-5
        gradient = j.T @ (w * r)
        if previous is not None:
            # A five-anchor S3 system can lose vertical observability when two links
            # are blocked. This short-term motion prior prevents a false floor/ceiling
            # jump while still allowing ordinary movement between rounds.
            reliable = sum(row["nlos"] < 0.65 for row in rows)
            prior = np.asarray([0.35, 0.35, 4.0 if reliable < 4 else 0.35])
            hessian += np.diag(prior)
            gradient += prior * (p - previous)
        try:
            step = np.linalg.solve(hessian, gradient)
        except np.linalg.LinAlgError:
            return None
        p -= step
        p = np.clip(p, bounds[0], bounds[1])
        last_hessian = hessian
        if float(np.linalg.norm(step)) < 1e-4:
            break

    residual_rms = math.sqrt(float(np.mean([row["residual"] ** 2 for row in rows])))
    try:
        geometry = math.sqrt(float(np.trace(np.linalg.inv(last_hessian))))
    except np.linalg.LinAlgError:
        geometry = math.inf
    for row in rows:
        row["state"].last_nlos_probability = float(row["nlos"])
    return {"position": p, "rms": residual_rms, "geometry": geometry, "rows": rows}


class RadioVoxelMap:
    def __init__(self, size_m: float, minimum: np.ndarray, maximum: np.ndarray):
        self.size = size_m
        self.minimum = minimum
        self.maximum = maximum
        self.voxels: dict[tuple[int, int, int], Voxel] = {}

    def key(self, point: np.ndarray) -> tuple[int, int, int]:
        return tuple(np.floor((point - self.minimum) / self.size).astype(int))

    def update_ray(self, anchor: np.ndarray, tag: np.ndarray, nlos_probability: float) -> None:
        distance = float(np.linalg.norm(tag - anchor))
        if distance < self.size:
            return
        count = max(2, int(distance / (self.size * 0.45)))
        keys: set[tuple[int, int, int]] = set()
        for fraction in np.linspace(0.15, 0.85, count):
            point = anchor + fraction * (tag - anchor)
            if np.all(point >= self.minimum) and np.all(point <= self.maximum):
                keys.add(self.key(point))
        delta = 0.20 * (nlos_probability - 0.5)
        for key in keys:
            voxel = self.voxels.setdefault(key, Voxel())
            voxel.log_odds = clamp(voxel.log_odds + delta, -4.0, 4.0)
            voxel.observations += 1

    def records(self) -> list[dict[str, Any]]:
        records = []
        for key, voxel in sorted(self.voxels.items()):
            center = self.minimum + (np.asarray(key) + 0.5) * self.size
            records.append({
                "ix": key[0], "iy": key[1], "iz": key[2],
                "x_m": center[0], "y_m": center[1], "z_m": center[2],
                "blockage_probability": sigmoid(voxel.log_odds),
                "observations": voxel.observations,
            })
        return records


class OnlineSystem:
    def __init__(self, config: dict[str, Any], output_dir: Path):
        self.config = config
        self.anchors = config["anchors"]
        self.links: dict[str, LinkState] = {}
        room = config["room_bounds_m"]
        self.bounds = (np.asarray(room["min"], dtype=float), np.asarray(room["max"], dtype=float))
        self.map = RadioVoxelMap(float(config.get("voxel_size_m", 0.5)), *self.bounds)
        self.max_age = float(config.get("max_measurement_age_s", 3.0))
        self.previous: np.ndarray | None = None
        self.output_dir = output_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        self.position_file = (output_dir / "positions.csv").open("w", newline="", encoding="utf-8")
        self.link_file = (output_dir / "link_measurements.csv").open("w", newline="", encoding="utf-8")
        self.position_writer = csv.DictWriter(self.position_file, fieldnames=[
            "pc_time_s", "round", "x_m", "y_m", "z_m", "residual_rms_m",
            "geometry_score", "anchor_count"])
        self.link_writer = csv.DictWriter(self.link_file, fieldnames=[
            "pc_time_s", "round", "anchor", "distance_m", "ftm_rssi",
            "csi_packets", "csi_rssi_mean", "csi_rssi_std", "csi_power_mean",
            "csi_roughness", "nlos_probability", "residual_m", "weight"])
        self.position_writer.writeheader()
        self.link_writer.writeheader()

    def close(self) -> None:
        self.position_file.close()
        self.link_file.close()

    def ingest(self, parsed: dict[str, Any]) -> None:
        aid = parsed["anchor"]
        if parsed["status"] != 0 or aid not in self.anchors or parsed["filtered_cm"] <= 0:
            return
        cfg = self.anchors[aid]
        distance = (float(cfg.get("scale", 1.0)) * parsed["filtered_cm"] / 100.0
                    + float(cfg.get("bias_m", 0.0)))
        measurement = Measurement(
            round_id=parsed["round"], timestamp=parsed["pc_monotonic"], distance_m=distance,
            ftm_rssi=parsed["ftm_rssi"], entries=parsed["entries"],
            csi_packets=parsed["csi_packets"], csi_rssi_mean=parsed["csi_rssi_mean"],
            csi_rssi_std=parsed["csi_rssi_std"], csi_power_mean=parsed["csi_power_mean"],
            csi_power_std=parsed["csi_power_std"], csi_roughness=parsed["csi_roughness"],
            csi_age_ms=parsed["csi_age_ms"], bssid=parsed["bssid"])
        self.links.setdefault(aid, LinkState()).update(measurement)

    def finish_round(self, round_id: int, now: float | None = None) -> dict[str, Any] | None:
        solution = solve_position(self.anchors, self.links, self.max_age, self.bounds,
                                  self.previous, now=now)
        if solution is None:
            return None
        p = solution["position"]
        self.previous = p.copy()
        pc_time = time.time()
        self.position_writer.writerow({
            "pc_time_s": f"{pc_time:.6f}", "round": round_id,
            "x_m": f"{p[0]:.6f}", "y_m": f"{p[1]:.6f}", "z_m": f"{p[2]:.6f}",
            "residual_rms_m": f"{solution['rms']:.6f}",
            "geometry_score": f"{solution['geometry']:.6f}",
            "anchor_count": len(solution["rows"]),
        })
        link_records = []
        for row in solution["rows"]:
            m = row["state"].latest
            self.map.update_ray(row["anchor"], p, row["nlos"])
            record = {
                "pc_time_s": f"{pc_time:.6f}", "round": round_id, "anchor": row["id"],
                "distance_m": f"{row['distance']:.6f}", "ftm_rssi": f"{m.ftm_rssi:.3f}",
                "csi_packets": m.csi_packets, "csi_rssi_mean": f"{m.csi_rssi_mean:.3f}",
                "csi_rssi_std": f"{m.csi_rssi_std:.3f}",
                "csi_power_mean": f"{m.csi_power_mean:.3f}",
                "csi_roughness": f"{m.csi_roughness:.6f}",
                "nlos_probability": f"{row['nlos']:.6f}",
                "residual_m": f"{row['residual']:.6f}", "weight": f"{row['weight']:.6f}",
            }
            self.link_writer.writerow(record)
            link_records.append(record)
        self.position_file.flush()
        self.link_file.flush()
        self._save_state(round_id, solution, link_records)
        return solution

    def _save_state(self, round_id: int, solution: dict[str, Any], links: list[dict[str, Any]]) -> None:
        p = solution["position"]
        state = {"schema": 1, "round": round_id, "updated_unix_s": time.time(),
                 "position_m": [float(x) for x in p],
                 "residual_rms_m": solution["rms"], "geometry_score": solution["geometry"],
                 "links": links,
                 "map_semantics": "coarse radio blockage likelihood; not wall geometry"}
        (self.output_dir / "latest_state.json").write_text(
            json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        records = self.map.records()
        with (self.output_dir / "radio_voxels.csv").open("w", newline="", encoding="utf-8") as file:
            fieldnames = ["ix", "iy", "iz", "x_m", "y_m", "z_m",
                          "blockage_probability", "observations"]
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(records)


def simulate(system: OnlineSystem, rounds: int = 100) -> None:
    rng = np.random.default_rng(20260820)
    anchors = {aid: anchor_vector(cfg) for aid, cfg in system.anchors.items()}
    for round_id in range(1, rounds + 1):
        phase = (round_id - 1) / max(rounds - 1, 1)
        truth = np.asarray([0.8 + 4.4 * phase, 2.5 + 1.4 * math.sin(phase * 2 * math.pi),
                            1.1 + 0.25 * math.sin(phase * 4 * math.pi)])
        now = time.monotonic()
        for aid, anchor in anchors.items():
            true_distance = float(np.linalg.norm(truth - anchor))
            blocked = aid in {"2", "5"} and 0.30 < phase < 0.72
            bias = rng.normal(0.0, 0.12) + (rng.uniform(0.8, 1.8) if blocked else 0.0)
            parsed = {
                "round": round_id, "anchor": aid, "status": 0,
                "filtered_cm": max(1, round((true_distance + bias) * 100)),
                "ftm_rssi": -72 if blocked else -53, "entries": 16,
                "csi_packets": 8, "csi_rssi_mean": -76 if blocked else -50,
                "csi_rssi_std": 7.0 if blocked else 1.5,
                "csi_power_mean": 900.0, "csi_power_std": 100.0,
                "csi_roughness": 3.2 if blocked else 0.35, "csi_age_ms": 20,
                "bssid": f"02:00:00:00:00:{int(aid):02x}", "pc_monotonic": now,
            }
            system.ingest(parsed)
        solution = system.finish_round(round_id, now=now)
        if solution and (round_id == 1 or round_id % 10 == 0):
            error = float(np.linalg.norm(solution["position"] - truth))
            p = solution["position"]
            print(f"SIM round={round_id:3d} pos=({p[0]:.2f},{p[1]:.2f},{p[2]:.2f}) "
                  f"truth_error={error:.2f}m rms={solution['rms']:.2f}m")


def run_serial(system: OnlineSystem, port: str, baudrate: int) -> None:
    try:
        import serial
    except ImportError as exc:
        raise SystemExit("Missing pyserial; run: pip install -r requirements.txt") from exc
    print(f"Listening on {port} at {baudrate} baud; Ctrl+C stops safely")
    with serial.Serial(port, baudrate, timeout=1) as device:
        while True:
            line = device.readline().decode("utf-8", errors="ignore").strip()
            parsed = parse_mfused(line)
            if parsed:
                system.ingest(parsed)
            elif line.startswith("MROUND,"):
                try:
                    round_id = int(line.split(",")[1])
                except (IndexError, ValueError):
                    continue
                solution = system.finish_round(round_id)
                if solution:
                    p = solution["position"]
                    print(f"POS round={round_id} x={p[0]:.3f} y={p[1]:.3f} z={p[2]:.3f} "
                          f"rms={solution['rms']:.3f}m anchors={len(solution['rows'])}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--port", help="Override serial port in config")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--simulate", action="store_true", help="Run deterministic hardware-free test")
    parser.add_argument("--rounds", type=int, default=100)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    system = OnlineSystem(config, args.output)
    try:
        if args.simulate:
            simulate(system, args.rounds)
        else:
            run_serial(system, args.port or config["serial_port"], int(config.get("baudrate", 115200)))
    finally:
        system.close()


if __name__ == "__main__":
    main()
