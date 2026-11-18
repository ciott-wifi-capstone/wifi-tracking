#!/usr/bin/env python3
"""ESP32-S3 7-Anchor strict-epoch indoor localization dashboard.

Expected firmware records:
  MFUSED,1,round,esp_us,anchor,status,rtt_raw_ns,dist_est_cm,corrected_cm,
          filtered_cm,ftm_rssi,entries,csi_packets,csi_rssi_mean,csi_rssi_std,
          csi_power_mean,csi_power_std,csi_roughness,csi_first_invalid,csi_age_ms,
          ok_count,fail_count,bssid,scan_rssi
  MROUND_START,round,start_us
  MROUND_META,round,start_us,end_us,duration_us,measured

Key design rule: measurements from different rounds are NEVER mixed.
"""
from __future__ import annotations

import argparse
import csv
import itertools
import json
import math
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

HERE = Path(__file__).resolve().parent


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def sigmoid(x: float) -> float:
    x = clamp(x, -30.0, 30.0)
    return 1.0 / (1.0 + math.exp(-x))


def robust_mad(values: list[float] | deque[float], floor: float = 0.20) -> float:
    if len(values) < 4:
        return max(floor, 0.75)
    a = np.asarray(values, dtype=float)
    med = float(np.median(a))
    mad = 1.4826 * float(np.median(np.abs(a - med)))
    return max(floor, mad)


@dataclass
class Measurement:
    round_id: int
    esp_us: int
    anchor_id: str
    status: int
    rtt_raw_ns: int
    dist_est_m: float
    corrected_m: float
    filtered_m: float
    ftm_rssi: float
    entries: int
    csi_packets: int
    csi_rssi_mean: float
    csi_rssi_std: float
    csi_power_mean: float
    csi_power_std: float
    csi_roughness: float
    csi_first_invalid: int
    csi_age_ms: int
    ok_count: int
    fail_count: int
    bssid: str
    scan_rssi: int
    pc_time: float = field(default_factory=time.time)

    @property
    def distance_m(self) -> float:
        # Important: use THIS FTM exchange, not firmware cross-round median.
        # corrected_m currently equals the single-shot RTT-derived distance because
        # the anchor calibration is scale=1, bias=0. Host-side robust fusion handles noise.
        return self.corrected_m


@dataclass
class Epoch:
    round_id: int
    start_us: int | None = None
    end_us: int | None = None
    duration_us: int | None = None
    measured_slots: int = 0
    measurements: dict[str, Measurement] = field(default_factory=dict)


@dataclass
class AnchorHistory:
    distances: deque[float] = field(default_factory=lambda: deque(maxlen=30))
    rssi: deque[float] = field(default_factory=lambda: deque(maxlen=40))
    last_nlos: float = 0.5


def parse_mfused(line: str) -> Measurement | None:
    p = line.strip().split(",")
    if len(p) != 24 or p[0] != "MFUSED" or p[1] != "1":
        return None
    try:
        return Measurement(
            round_id=int(p[2]), esp_us=int(p[3]), anchor_id=str(int(p[4])),
            status=int(p[5]), rtt_raw_ns=int(p[6]), dist_est_m=int(p[7]) / 100.0,
            corrected_m=int(p[8]) / 100.0, filtered_m=int(p[9]) / 100.0,
            ftm_rssi=float(p[10]), entries=int(p[11]), csi_packets=int(p[12]),
            csi_rssi_mean=float(p[13]), csi_rssi_std=float(p[14]),
            csi_power_mean=float(p[15]), csi_power_std=float(p[16]),
            csi_roughness=float(p[17]), csi_first_invalid=int(p[18]),
            csi_age_ms=int(p[19]), ok_count=int(p[20]), fail_count=int(p[21]),
            bssid=p[22], scan_rssi=int(p[23]),
        )
    except (ValueError, IndexError):
        return None


def parse_round_start(line: str) -> tuple[int, int] | None:
    p = line.strip().split(",")
    if len(p) != 3 or p[0] != "MROUND_START":
        return None
    try:
        return int(p[1]), int(p[2])
    except ValueError:
        return None


def parse_round_meta(line: str) -> tuple[int, int, int, int, int] | None:
    p = line.strip().split(",")
    if len(p) != 6 or p[0] != "MROUND_META":
        return None
    try:
        return int(p[1]), int(p[2]), int(p[3]), int(p[4]), int(p[5])
    except ValueError:
        return None


class PositionKalman:
    """Constant-velocity KF: [x, y, vx, vy]."""

    def __init__(self, accel_sigma: float = 2.5):
        self.accel_sigma = float(accel_sigma)
        self.x: np.ndarray | None = None
        self.P: np.ndarray | None = None
        self.last_t_s: float | None = None

    def reset(self) -> None:
        self.x = None
        self.P = None
        self.last_t_s = None

    def update(self, z: np.ndarray, t_s: float, R: np.ndarray) -> np.ndarray:
        z = np.asarray(z, dtype=float).reshape(2)
        if self.x is None:
            self.x = np.array([z[0], z[1], 0.0, 0.0], dtype=float)
            self.P = np.diag([2.0, 2.0, 4.0, 4.0])
            self.last_t_s = t_s
            return self.x[:2].copy()

        dt = clamp(t_s - float(self.last_t_s), 0.01, 2.0)
        F = np.array([[1, 0, dt, 0], [0, 1, 0, dt], [0, 0, 1, 0], [0, 0, 0, 1]], dtype=float)
        G = np.array([[0.5 * dt * dt, 0], [0, 0.5 * dt * dt], [dt, 0], [0, dt]], dtype=float)
        Q = (self.accel_sigma ** 2) * (G @ G.T)
        self.x = F @ self.x
        self.P = F @ self.P @ F.T + Q

        H = np.array([[1, 0, 0, 0], [0, 1, 0, 0]], dtype=float)
        y = z - H @ self.x
        S = H @ self.P @ H.T + R
        try:
            K = self.P @ H.T @ np.linalg.inv(S)
        except np.linalg.LinAlgError:
            K = self.P @ H.T @ np.linalg.pinv(S)
        self.x = self.x + K @ y
        self.P = (np.eye(4) - K @ H) @ self.P
        self.last_t_s = t_s
        return self.x[:2].copy()


class StrictEpochLocalizer:
    def __init__(self, config: dict[str, Any], output_dir: Path):
        self.config = config
        self.anchors = {
            aid: cfg for aid, cfg in config["anchors"].items() if bool(cfg.get("enabled", True))
        }
        self.min_anchors = int(config.get("min_anchors_for_solution", 3))
        self.preferred_anchors = int(config.get("preferred_anchors_for_solution", 4))
        self.max_range = float(config.get("max_range_m", 80.0))
        self.target_z = float(config.get("target_z_m", 0.0))
        self.height_comp = bool(config.get("use_height_compensation", False))
        solver = config.get("solver", {})
        self.huber_delta = float(solver.get("huber_delta_m", 1.0))
        self.sigma_floor = float(solver.get("range_sigma_floor_m", 0.35))
        self.enable_kalman = bool(solver.get("enable_kalman", True))
        self.nlos_reject = float(config.get("nlos_reject_probability", 0.88))
        self.histories = {aid: AnchorHistory() for aid in self.anchors}
        self.epochs: dict[int, Epoch] = {}
        self.last_raw: np.ndarray | None = None
        self.last_filtered: np.ndarray | None = None
        self.kf = PositionKalman(float(solver.get("process_accel_sigma_mps2", 2.5)))
        self.output_dir = output_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        self._open_logs()

    def _open_logs(self) -> None:
        self.pos_file = (self.output_dir / "positions.csv").open("w", newline="", encoding="utf-8")
        self.link_file = (self.output_dir / "links.csv").open("w", newline="", encoding="utf-8")
        self.pos_writer = csv.DictWriter(self.pos_file, fieldnames=[
            "pc_time_s", "round", "epoch_start_us", "epoch_end_us", "duration_ms",
            "x_raw_m", "y_raw_m", "x_filtered_m", "y_filtered_m", "rms_m",
            "anchor_count", "inlier_count", "geometry_score"
        ])
        self.link_writer = csv.DictWriter(self.link_file, fieldnames=[
            "pc_time_s", "round", "anchor", "esp_us", "distance_m", "filtered_fw_m",
            "ftm_rssi", "entries", "csi_packets", "csi_rssi_mean", "csi_rssi_std",
            "csi_roughness", "residual_m", "weight", "nlos_probability", "inlier",
            "range_sigma_m", "time_weight"
        ])
        self.pos_writer.writeheader()
        self.link_writer.writeheader()

    def close(self) -> None:
        self.pos_file.close()
        self.link_file.close()

    def start_epoch(self, round_id: int, start_us: int) -> None:
        self.epochs[round_id] = Epoch(round_id=round_id, start_us=start_us)
        # Avoid unlimited growth if serial is noisy or round endings were missed.
        for rid in sorted(list(self.epochs))[:-6]:
            self.epochs.pop(rid, None)

    def ingest(self, m: Measurement) -> None:
        if m.anchor_id not in self.anchors:
            return
        epoch = self.epochs.setdefault(m.round_id, Epoch(round_id=m.round_id))
        # One anchor contributes at most one measurement to one epoch.
        epoch.measurements[m.anchor_id] = m

    def _horizontal_distance(self, m: Measurement, aid: str) -> float:
        d = m.distance_m
        if not self.height_comp:
            return d
        dz = float(self.anchors[aid].get("z_m", 0.0)) - self.target_z
        sq = d * d - dz * dz
        return math.sqrt(max(0.01, sq))

    def _valid_rows(self, epoch: Epoch) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for aid, m in epoch.measurements.items():
            if aid not in self.anchors or m.status != 0:
                continue
            d = self._horizontal_distance(m, aid)
            if not (0.10 < d < self.max_range):
                continue
            a = self.anchors[aid]
            rows.append({
                "id": aid,
                "anchor": np.array([float(a["x_m"]), float(a["y_m"])], dtype=float),
                "m": m,
                "distance": d,
            })
        return rows

    @staticmethod
    def _geometry_ok(rows: list[dict[str, Any]]) -> bool:
        if len(rows) < 3:
            return False
        p = np.asarray([r["anchor"] for r in rows], dtype=float)
        return np.linalg.matrix_rank(p[1:] - p[0]) >= 2

    def _solve_gn(self, rows: list[dict[str, Any]], weights: np.ndarray | None = None,
                  initial: np.ndarray | None = None, iterations: int = 15) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        if initial is None:
            p = np.mean(np.asarray([r["anchor"] for r in rows]), axis=0)
        else:
            p = np.asarray(initial, dtype=float).copy()
        if weights is None:
            weights = np.ones(len(rows), dtype=float)
        weights = np.maximum(np.asarray(weights, dtype=float), 1e-8)
        for _ in range(iterations):
            J, residual = [], []
            for row in rows:
                delta = p - row["anchor"]
                pred = max(float(np.linalg.norm(delta)), 1e-6)
                # residual convention: predicted - measured
                residual.append(pred - row["distance"])
                J.append(delta / pred)
            J = np.asarray(J, dtype=float)
            r = np.asarray(residual, dtype=float)
            H = J.T @ (weights[:, None] * J) + np.eye(2) * 1e-6
            g = J.T @ (weights * r)
            try:
                step = np.linalg.solve(H, g)
            except np.linalg.LinAlgError:
                step = np.linalg.pinv(H) @ g
            p -= step
            if float(np.linalg.norm(step)) < 1e-4:
                break
        residual = np.asarray([float(np.linalg.norm(p - row["anchor"])) - row["distance"] for row in rows])
        return p, residual, J

    def _robust_initial(self, rows: list[dict[str, Any]]) -> np.ndarray:
        if len(rows) <= 3:
            p, _, _ = self._solve_gn(rows, initial=self.last_raw)
            return p
        subset_n = min(max(3, self.preferred_anchors), len(rows))
        best_score = math.inf
        best_p: np.ndarray | None = None
        # 7 anchors -> at most C(7,4)=35 candidates, cheap and robust.
        for subset in itertools.combinations(rows, subset_n):
            if not self._geometry_ok(list(subset)):
                continue
            try:
                p, _, _ = self._solve_gn(list(subset), initial=self.last_raw, iterations=12)
            except Exception:
                continue
            all_abs = np.asarray([abs(float(np.linalg.norm(p - r["anchor"])) - r["distance"]) for r in rows])
            med = float(np.median(all_abs))
            trimmed = np.sort(all_abs)[:max(3, len(all_abs) - 1)]
            score = med + 0.35 * float(np.sqrt(np.mean(trimmed ** 2)))
            if score < best_score:
                best_score, best_p = score, p
        if best_p is None:
            best_p, _, _ = self._solve_gn(rows, initial=self.last_raw)
        return best_p

    def _base_nlos(self, row: dict[str, Any], residual: float) -> tuple[float, float, float]:
        aid, m = row["id"], row["m"]
        hist = self.histories[aid]
        radio_rssi = m.csi_rssi_mean if m.csi_packets > 0 and m.csi_rssi_mean > -120 else m.ftm_rssi
        # Weak RSSI alone is only weak evidence; do not reject anchors solely because of it.
        weak = clamp((-62.0 - radio_rssi) / 25.0, 0.0, 1.0)
        unstable = clamp((m.csi_rssi_std - 2.5) / 8.0, 0.0, 1.0) if m.csi_packets > 0 else 0.15
        if len(hist.distances) >= 4:
            med = float(np.median(np.asarray(hist.distances)))
            sigma = robust_mad(hist.distances, self.sigma_floor)
            positive_jump = clamp((row["distance"] - med - 1.0) / max(2.5 * sigma, 1.0), 0.0, 1.0)
        else:
            positive_jump = 0.0
        # residual = predicted - measured. Strongly negative means measured range is too long,
        # the characteristic indoor multipath/NLOS direction seen by FTM.
        excess_range = clamp(((-residual) - 0.75) / 2.25, 0.0, 1.0)
        feature_score = -2.2 + 1.15 * weak + 0.65 * unstable + 1.15 * positive_jump
        p_feature = sigmoid(feature_score)
        p_nlos = 1.0 - (1.0 - p_feature) * (1.0 - 0.88 * excess_range)
        return clamp(p_nlos, 0.0, 0.995), radio_rssi, positive_jump

    def _quality_weight(self, row: dict[str, Any], residual: float, epoch_center_us: float,
                        epoch_duration_us: float) -> tuple[float, float, float, float]:
        aid, m = row["id"], row["m"]
        hist = self.histories[aid]
        sigma_range = robust_mad(hist.distances, self.sigma_floor)
        p_nlos, radio_rssi, _ = self._base_nlos(row, residual)
        q_entries = clamp(m.entries / 6.0, 0.25, 1.0)
        q_rssi = clamp((radio_rssi + 90.0) / 38.0, 0.25, 1.0)
        q_csi = 1.0 if m.csi_packets >= 1 else 0.78
        # Mild time weighting inside the same epoch. Strict round matching is the main protection.
        sigma_t_us = max(epoch_duration_us, 150_000.0)
        q_time = math.exp(-0.5 * ((m.esp_us - epoch_center_us) / sigma_t_us) ** 2)
        huber = 1.0 if abs(residual) <= self.huber_delta else self.huber_delta / max(abs(residual), 1e-6)
        w = ((1.0 - p_nlos) ** 3) * q_entries * q_rssi * q_csi * q_time * huber
        w /= (sigma_range * sigma_range + self.sigma_floor * self.sigma_floor)
        return max(w, 1e-6), p_nlos, sigma_range, q_time

    def finalize(self, round_id: int, start_us: int, end_us: int, duration_us: int,
                 measured_slots: int) -> dict[str, Any] | None:
        epoch = self.epochs.pop(round_id, Epoch(round_id=round_id))
        epoch.start_us, epoch.end_us, epoch.duration_us = start_us, end_us, duration_us
        epoch.measured_slots = measured_slots
        rows = self._valid_rows(epoch)
        if len(rows) < self.min_anchors or not self._geometry_ok(rows):
            # Still update link histories for valid measurements; no fake position is emitted.
            for r in rows:
                self.histories[r["id"]].distances.append(r["distance"])
            return None

        p = self._robust_initial(rows)
        center_us = (start_us + end_us) / 2.0
        final_weights = np.ones(len(rows), dtype=float)
        for _ in range(10):
            residuals = np.asarray([float(np.linalg.norm(p - r["anchor"])) - r["distance"] for r in rows])
            meta = [self._quality_weight(row, float(res), center_us, duration_us)
                    for row, res in zip(rows, residuals)]
            weights = np.asarray([x[0] for x in meta], dtype=float)
            nlos = np.asarray([x[1] for x in meta], dtype=float)
            # If enough alternatives exist, nearly eliminate very likely NLOS links.
            good_count = int(np.sum(nlos < self.nlos_reject))
            if good_count >= 3:
                weights = np.where(nlos >= self.nlos_reject, weights * 0.01, weights)
            new_p, _, _ = self._solve_gn(rows, weights=weights, initial=p, iterations=5)
            if float(np.linalg.norm(new_p - p)) < 1e-4:
                p = new_p
                final_weights = weights
                break
            p = new_p
            final_weights = weights

        residuals = np.asarray([float(np.linalg.norm(p - r["anchor"])) - r["distance"] for r in rows])
        meta = [self._quality_weight(row, float(res), center_us, duration_us)
                for row, res in zip(rows, residuals)]
        weights = np.asarray([x[0] for x in meta], dtype=float)
        nlos = np.asarray([x[1] for x in meta], dtype=float)
        sigmas = np.asarray([x[2] for x in meta], dtype=float)
        time_weights = np.asarray([x[3] for x in meta], dtype=float)
        if int(np.sum(nlos < self.nlos_reject)) >= 3:
            weights = np.where(nlos >= self.nlos_reject, weights * 0.01, weights)
        final_weights = weights

        J = []
        for row in rows:
            delta = p - row["anchor"]
            pred = max(float(np.linalg.norm(delta)), 1e-6)
            J.append(delta / pred)
        J = np.asarray(J)
        W = final_weights
        H = J.T @ (W[:, None] * J) + np.eye(2) * 1e-6
        rms = float(np.sqrt(np.average(residuals ** 2, weights=np.maximum(W, 1e-8))))
        try:
            cov = np.linalg.inv(H) * max(rms * rms, self.sigma_floor ** 2)
            geometry = float(math.sqrt(max(np.trace(np.linalg.inv(J.T @ J + np.eye(2) * 1e-6)), 0.0)))
        except np.linalg.LinAlgError:
            cov = np.eye(2) * max(rms * rms, 1.0)
            geometry = math.inf
        cov = np.clip(cov, -25.0, 25.0)
        cov[0, 0] = clamp(float(cov[0, 0]), 0.08, 25.0)
        cov[1, 1] = clamp(float(cov[1, 1]), 0.08, 25.0)

        raw = p.copy()
        t_s = center_us / 1e6
        filtered = self.kf.update(raw, t_s, cov) if self.enable_kalman else raw.copy()
        self.last_raw = raw.copy()
        self.last_filtered = filtered.copy()

        link_rows = []
        for i, row in enumerate(rows):
            aid = row["id"]
            self.histories[aid].last_nlos = float(nlos[i])
            self.histories[aid].distances.append(row["distance"])
            self.histories[aid].rssi.append(row["m"].ftm_rssi)
            inlier = bool(nlos[i] < self.nlos_reject and abs(residuals[i]) < max(2.5, 3.0 * sigmas[i]))
            link_rows.append({
                "id": aid, "distance": row["distance"], "residual": float(residuals[i]),
                "weight": float(final_weights[i]), "nlos": float(nlos[i]),
                "sigma": float(sigmas[i]), "time_weight": float(time_weights[i]),
                "inlier": inlier, "measurement": row["m"]
            })

        self._log_solution(round_id, start_us, end_us, duration_us, raw, filtered,
                           rms, geometry, link_rows)
        return {
            "round": round_id, "raw": raw, "filtered": filtered, "rms": rms,
            "geometry": geometry, "rows": link_rows, "duration_ms": duration_us / 1000.0,
            "anchor_count": len(rows), "inlier_count": sum(int(x["inlier"]) for x in link_rows),
            "epoch": epoch,
        }

    def _log_solution(self, round_id: int, start_us: int, end_us: int, duration_us: int,
                      raw: np.ndarray, filtered: np.ndarray, rms: float, geometry: float,
                      link_rows: list[dict[str, Any]]) -> None:
        pc = time.time()
        self.pos_writer.writerow({
            "pc_time_s": f"{pc:.6f}", "round": round_id, "epoch_start_us": start_us,
            "epoch_end_us": end_us, "duration_ms": f"{duration_us/1000.0:.3f}",
            "x_raw_m": f"{raw[0]:.6f}", "y_raw_m": f"{raw[1]:.6f}",
            "x_filtered_m": f"{filtered[0]:.6f}", "y_filtered_m": f"{filtered[1]:.6f}",
            "rms_m": f"{rms:.6f}", "anchor_count": len(link_rows),
            "inlier_count": sum(int(x["inlier"]) for x in link_rows),
            "geometry_score": f"{geometry:.6f}",
        })
        for row in link_rows:
            m = row["measurement"]
            self.link_writer.writerow({
                "pc_time_s": f"{pc:.6f}", "round": round_id, "anchor": row["id"],
                "esp_us": m.esp_us, "distance_m": f"{row['distance']:.6f}",
                "filtered_fw_m": f"{m.filtered_m:.6f}", "ftm_rssi": f"{m.ftm_rssi:.2f}",
                "entries": m.entries, "csi_packets": m.csi_packets,
                "csi_rssi_mean": f"{m.csi_rssi_mean:.3f}",
                "csi_rssi_std": f"{m.csi_rssi_std:.3f}",
                "csi_roughness": f"{m.csi_roughness:.6f}",
                "residual_m": f"{row['residual']:.6f}", "weight": f"{row['weight']:.8f}",
                "nlos_probability": f"{row['nlos']:.6f}", "inlier": int(row["inlier"]),
                "range_sigma_m": f"{row['sigma']:.6f}", "time_weight": f"{row['time_weight']:.6f}",
            })
        self.pos_file.flush()
        self.link_file.flush()


