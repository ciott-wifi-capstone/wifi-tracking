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


def logit(p: float) -> float:
    p = clamp(float(p), 1e-4, 1.0 - 1e-4)
    return math.log(p / (1.0 - p))


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
        weighting = config.get("weighting", {})
        self.los_weight_exponent = float(weighting.get("los_weight_exponent", 2.0))
        self.loo_excess_start_m = float(weighting.get("loo_excess_start_m", 0.8))
        self.loo_excess_full_m = float(weighting.get("loo_excess_full_m", 4.0))
        self.default_indoor_prior = float(weighting.get("indoor_prior_nlos", 0.08))
        self.default_outdoor_prior = float(weighting.get("outdoor_prior_nlos", 0.80))
        self.min_relative_weight = float(weighting.get("min_relative_weight", 0.01))
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
            "csi_roughness", "residual_m", "loo_residual_m", "weight",
            "nlos_probability", "prior_nlos_probability", "inlier",
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

    def _anchor_prior_nlos(self, aid: str) -> tuple[float, str, float]:
        """Return (prior_nlos, zone, loo_strength).

        The user's current deployment has three indoor anchors (A2/A6/A8) and
        four outdoor anchors (A1/A3/A4/A5) separated by walls.  This prior is
        deliberately explicit in anchors_7.json instead of being inferred from
        RSSI.  It keeps outdoor links useful for geometry, but naturally much
        lower weighted than indoor links.
        """
        cfg = self.anchors[aid]
        zone = str(cfg.get("zone", "unknown")).lower()
        if "prior_nlos" in cfg:
            prior = float(cfg["prior_nlos"])
        elif zone == "indoor":
            prior = self.default_indoor_prior
        elif zone == "outdoor":
            prior = self.default_outdoor_prior
        else:
            prior = 0.35
        if "loo_strength" in cfg:
            loo_strength = float(cfg["loo_strength"])
        else:
            # Protect known-indoor anchors from false rejection caused by a
            # temporarily bad global position. Outdoor links are allowed to be
            # rejected more aggressively because a wall is known to exist.
            loo_strength = 0.55 if zone == "indoor" else 0.92
        return clamp(prior, 0.01, 0.98), zone, clamp(loo_strength, 0.0, 1.0)

    def _feature_nlos(self, row: dict[str, Any]) -> tuple[float, float, float, float]:
        """NLOS probability before geometric residual evidence.

        This part never uses the current position estimate, so it cannot create
        the old feedback loop: bad position -> bad residual -> good indoor link
        rejected -> even worse position.
        """
        aid, m = row["id"], row["m"]
        hist = self.histories[aid]
        prior, zone, _ = self._anchor_prior_nlos(aid)
        radio_rssi = m.csi_rssi_mean if m.csi_packets > 0 and m.csi_rssi_mean > -120 else m.ftm_rssi
        weak = clamp((-65.0 - radio_rssi) / 24.0, 0.0, 1.0)
        unstable = clamp((m.csi_rssi_std - 3.0) / 8.0, 0.0, 1.0) if m.csi_packets > 0 else 0.15
        if len(hist.distances) >= 5:
            med = float(np.median(np.asarray(hist.distances)))
            sigma = robust_mad(hist.distances, self.sigma_floor)
            positive_jump = clamp((row["distance"] - med - 0.8) / max(2.8 * sigma, 1.2), 0.0, 1.0)
        else:
            positive_jump = 0.0

        # Bayesian-style prior in log-odds space.  Radio features can move the
        # probability, but they cannot by themselves overturn the known
        # indoor/outdoor placement too aggressively.
        score = logit(prior) + 0.70 * weak + 0.45 * unstable + 1.00 * positive_jump
        p = sigmoid(score)
        return clamp(p, 0.005, 0.995), radio_rssi, positive_jump, prior

    def _base_quality(self, row: dict[str, Any], epoch_center_us: float,
                      epoch_duration_us: float) -> tuple[float, float, float, float, float]:
        aid, m = row["id"], row["m"]
        hist = self.histories[aid]
        sigma_range = robust_mad(hist.distances, self.sigma_floor)
        p_feature, radio_rssi, _, prior = self._feature_nlos(row)
        q_entries = clamp(m.entries / 6.0, 0.30, 1.0)
        q_rssi = clamp((radio_rssi + 90.0) / 38.0, 0.25, 1.0)
        q_csi = 1.0 if m.csi_packets >= 1 else 0.78
        sigma_t_us = max(epoch_duration_us, 150_000.0)
        q_time = math.exp(-0.5 * ((m.esp_us - epoch_center_us) / sigma_t_us) ** 2)
        quality = q_entries * q_rssi * q_csi * q_time
        quality /= (sigma_range * sigma_range + self.sigma_floor * self.sigma_floor)
        # This is the static/prior weight.  It is used for leave-one-out
        # prediction before any residual-based NLOS decision is made.
        prior_weight = quality * max((1.0 - p_feature) ** self.los_weight_exponent,
                                     self.min_relative_weight)
        return max(prior_weight, 1e-8), p_feature, sigma_range, q_time, prior

    def _loo_residuals(self, rows: list[dict[str, Any]], seed: np.ndarray,
                       base_weights: np.ndarray) -> np.ndarray:
        """Leave-one-out residual for every anchor.

        residual convention is predicted - measured. A strongly negative LOO
        residual means this anchor reports a range that is much too long, the
        common FTM multipath/NLOS direction. Crucially, anchor i is excluded
        when its own residual is calculated.
        """
        out = np.zeros(len(rows), dtype=float)
        global_p, _, _ = self._solve_gn(rows, weights=base_weights, initial=seed, iterations=8)
        for i, row in enumerate(rows):
            others = [r for j, r in enumerate(rows) if j != i]
            if len(others) >= 3 and self._geometry_ok(others):
                w = np.asarray([base_weights[j] for j in range(len(rows)) if j != i], dtype=float)
                try:
                    p_loo, _, _ = self._solve_gn(others, weights=w, initial=global_p, iterations=8)
                except Exception:
                    p_loo = global_p
            else:
                p_loo = global_p
            pred = float(np.linalg.norm(p_loo - row["anchor"]))
            out[i] = pred - float(row["distance"])
        return out

    def _final_link_weight(self, row: dict[str, Any], loo_residual: float,
                           epoch_center_us: float, epoch_duration_us: float
                           ) -> tuple[float, float, float, float, float]:
        base_weight, p_feature, sigma_range, q_time, prior = self._base_quality(
            row, epoch_center_us, epoch_duration_us)
        _, _, loo_strength = self._anchor_prior_nlos(row["id"])

        # Only the characteristic "measured range too long" direction is used
        # as strong NLOS evidence.  A short range is left to the symmetric Huber
        # term rather than automatically being called NLOS.
        excess_m = max(0.0, -loo_residual - self.loo_excess_start_m)
        span = max(self.loo_excess_full_m - self.loo_excess_start_m, 0.2)
        excess_score = clamp(excess_m / span, 0.0, 1.0)
        p_nlos = 1.0 - (1.0 - p_feature) * (1.0 - loo_strength * excess_score)
        p_nlos = clamp(p_nlos, 0.005, 0.995)

        huber = 1.0 if abs(loo_residual) <= self.huber_delta else \
            self.huber_delta / max(abs(loo_residual), 1e-6)

        # Reconstruct the radio/time quality from base_weight by removing its
        # feature-NLOS factor, then apply the final NLOS probability.  This
        # keeps the final weighting formula explicit and stable.
        feature_factor = max((1.0 - p_feature) ** self.los_weight_exponent,
                             self.min_relative_weight)
        quality_only = base_weight / feature_factor
        los_factor = max((1.0 - p_nlos) ** self.los_weight_exponent,
                         self.min_relative_weight)
        weight = quality_only * los_factor * huber
        return max(weight, 1e-8), p_nlos, sigma_range, q_time, prior

    def finalize(self, round_id: int, start_us: int, end_us: int, duration_us: int,
                 measured_slots: int) -> dict[str, Any] | None:
        epoch = self.epochs.pop(round_id, Epoch(round_id=round_id))
        epoch.start_us, epoch.end_us, epoch.duration_us = start_us, end_us, duration_us
        epoch.measured_slots = measured_slots
        rows = self._valid_rows(epoch)
        if len(rows) < self.min_anchors or not self._geometry_ok(rows):
            for r in rows:
                self.histories[r["id"]].distances.append(r["distance"])
            return None

        center_us = (start_us + end_us) / 2.0
        seed = self._robust_initial(rows)

        # Pass 1: prior/radio/history weighting only. No current-solution
        # residual is allowed to decide which anchor is good or bad.
        base_meta = [self._base_quality(r, center_us, duration_us) for r in rows]
        base_weights = np.asarray([x[0] for x in base_meta], dtype=float)
        p_prior_solution, _, _ = self._solve_gn(rows, weights=base_weights, initial=seed, iterations=10)

        # Pass 2: leave-one-out residuals. Each anchor is judged by a position
        # estimated without using that anchor itself.
        loo1 = self._loo_residuals(rows, p_prior_solution, base_weights)
        meta1 = [self._final_link_weight(r, float(lr), center_us, duration_us)
                 for r, lr in zip(rows, loo1)]
        weights1 = np.asarray([x[0] for x in meta1], dtype=float)
        p1, _, _ = self._solve_gn(rows, weights=weights1, initial=p_prior_solution, iterations=10)

        # One refinement pass uses the first robust weights for the LOO solves.
        loo2 = self._loo_residuals(rows, p1, weights1)
        meta2 = [self._final_link_weight(r, float(lr), center_us, duration_us)
                 for r, lr in zip(rows, loo2)]
        weights = np.asarray([x[0] for x in meta2], dtype=float)
        nlos = np.asarray([x[1] for x in meta2], dtype=float)
        sigmas = np.asarray([x[2] for x in meta2], dtype=float)
        time_weights = np.asarray([x[3] for x in meta2], dtype=float)
        priors = np.asarray([x[4] for x in meta2], dtype=float)

        # Do not hard-delete outdoor/NLOS links unless there are at least three
        # good alternatives.  Their low prior weight can still help geometry.
        good_count = int(np.sum(nlos < self.nlos_reject))
        if good_count >= 3:
            weights = np.where(nlos >= self.nlos_reject, weights * 0.08, weights)

        p, _, _ = self._solve_gn(rows, weights=weights, initial=p1, iterations=12)
        residuals = np.asarray([
            float(np.linalg.norm(p - r["anchor"])) - r["distance"] for r in rows
        ], dtype=float)

        # Absolute scale of W does not affect WLS, so normalize for a clearer
        # UI/log value in [0, 1] while keeping the same solve.
        max_w = float(np.max(weights)) if len(weights) else 1.0
        display_weights = weights / max(max_w, 1e-12)

        J = []
        for row in rows:
            delta = p - row["anchor"]
            pred = max(float(np.linalg.norm(delta)), 1e-6)
            J.append(delta / pred)
        J = np.asarray(J)
        W = weights
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
            inlier = bool(nlos[i] < self.nlos_reject and abs(loo2[i]) < max(3.0, 3.5 * sigmas[i]))
            link_rows.append({
                "id": aid, "distance": row["distance"], "residual": float(residuals[i]),
                "loo_residual": float(loo2[i]), "weight": float(display_weights[i]),
                "solver_weight": float(weights[i]), "nlos": float(nlos[i]),
                "prior_nlos": float(priors[i]), "sigma": float(sigmas[i]),
                "time_weight": float(time_weights[i]), "inlier": inlier,
                "measurement": row["m"]
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
                "residual_m": f"{row['residual']:.6f}",
                "loo_residual_m": f"{row['loo_residual']:.6f}",
                "weight": f"{row['weight']:.8f}",
                "nlos_probability": f"{row['nlos']:.6f}",
                "prior_nlos_probability": f"{row['prior_nlos']:.6f}",
                "inlier": int(row["inlier"]),
                "range_sigma_m": f"{row['sigma']:.6f}", "time_weight": f"{row['time_weight']:.6f}",
            })
        self.pos_file.flush()
        self.link_file.flush()


