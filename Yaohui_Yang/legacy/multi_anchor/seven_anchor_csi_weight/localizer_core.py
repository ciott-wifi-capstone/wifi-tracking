#!/usr/bin/env python3
"""Strict-epoch 7-anchor localizer with CSI-driven dynamic range weighting.

Design goal:
  FTM provides range; CSI estimates how trustworthy THIS range measurement is.
  No anchor is permanently classified as indoor/outdoor/LOS/NLOS.
  Geometry residuals are used only as a final robust safety net (Huber), not as
  the primary reliability classifier.
"""
from __future__ import annotations

import csv
import itertools
import math
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def smooth_good_low(x: float, good: float, bad: float) -> float:
    """1 near/below good, 0 near/above bad, smooth linear in between."""
    if bad <= good:
        return 1.0
    return 1.0 - clamp((x - good) / (bad - good), 0.0, 1.0)


def sigmoid(x: float) -> float:
    x = clamp(x, -30.0, 30.0)
    return 1.0 / (1.0 + math.exp(-x))


def robust_mad(values, floor: float = 0.20) -> float:
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
    roughness: deque[float] = field(default_factory=lambda: deque(maxlen=40))
    csi_rssi_std: deque[float] = field(default_factory=lambda: deque(maxlen=40))
    power_cv: deque[float] = field(default_factory=lambda: deque(maxlen=40))


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
    def __init__(self, accel_sigma: float = 2.5):
        self.accel_sigma = float(accel_sigma)
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
        F = np.array([[1,0,dt,0],[0,1,0,dt],[0,0,1,0],[0,0,0,1]], dtype=float)
        G = np.array([[0.5*dt*dt,0],[0,0.5*dt*dt],[dt,0],[0,dt]], dtype=float)
        Q = (self.accel_sigma**2) * (G @ G.T)
        self.x = F @ self.x
        self.P = F @ self.P @ F.T + Q
        H = np.array([[1,0,0,0],[0,1,0,0]], dtype=float)
        y = z - H @ self.x
        S = H @ self.P @ H.T + R
        K = self.P @ H.T @ np.linalg.pinv(S)
        self.x = self.x + K @ y
        self.P = (np.eye(4) - K @ H) @ self.P
        self.last_t_s = t_s
        return self.x[:2].copy()


class StrictEpochLocalizer:
    def __init__(self, config: dict[str, Any], output_dir: Path):
        self.config = config
        self.anchors = {aid: cfg for aid, cfg in config["anchors"].items() if cfg.get("enabled", True)}
        self.min_anchors = int(config.get("min_anchors_for_solution", 3))
        self.preferred_anchors = int(config.get("preferred_anchors_for_solution", 4))
        self.max_range = float(config.get("max_range_m", 80.0))
        self.target_z = float(config.get("target_z_m", 0.0))
        self.height_comp = bool(config.get("use_height_compensation", False))
        solver = config.get("solver", {})
        self.huber_delta = float(solver.get("huber_delta_m", 2.0))
        self.sigma_floor = float(solver.get("range_sigma_floor_m", 0.35))
        self.enable_kalman = bool(solver.get("enable_kalman", True))
        self.kf = PositionKalman(float(solver.get("process_accel_sigma_mps2", 2.5)))
        self.cw = config.get("csi_weighting", {})
        self.histories = {aid: AnchorHistory() for aid in self.anchors}
        self.epochs: dict[int, Epoch] = {}
        self.last_raw = None
        self.output_dir = output_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        self._open_logs()

    def _open_logs(self):
        self.pos_file = (self.output_dir / "positions.csv").open("w", newline="", encoding="utf-8")
        self.link_file = (self.output_dir / "links.csv").open("w", newline="", encoding="utf-8")
        self.pos_writer = csv.DictWriter(self.pos_file, fieldnames=[
            "pc_time_s","round","epoch_start_us","epoch_end_us","duration_ms",
            "x_raw_m","y_raw_m","x_filtered_m","y_filtered_m","rms_m","anchor_count"])
        self.link_writer = csv.DictWriter(self.link_file, fieldnames=[
            "pc_time_s","round","anchor","esp_us","distance_m","ftm_rssi","entries",
            "csi_packets","csi_rssi_mean","csi_rssi_std","csi_power_mean","csi_power_std",
            "csi_roughness","csi_reliability","csi_sigma_m","range_sigma_m","residual_m","weight",
            "q_packets","q_rssi_strength","q_rssi_stability","q_power_stability","q_roughness"])
        self.pos_writer.writeheader(); self.link_writer.writeheader()

    def close(self):
        self.pos_file.close(); self.link_file.close()

    def start_epoch(self, round_id: int, start_us: int):
        self.epochs[round_id] = Epoch(round_id=round_id, start_us=start_us)
        for rid in sorted(self.epochs)[:-6]:
            self.epochs.pop(rid, None)

    def ingest(self, m: Measurement):
        if m.anchor_id in self.anchors:
            self.epochs.setdefault(m.round_id, Epoch(round_id=m.round_id)).measurements[m.anchor_id] = m

    def _horizontal_distance(self, m: Measurement, aid: str) -> float:
        d = m.distance_m
        if not self.height_comp:
            return d
        dz = float(self.anchors[aid].get("z_m", 0.0)) - self.target_z
        return math.sqrt(max(0.01, d*d - dz*dz))

    def _valid_rows(self, epoch: Epoch):
        rows = []
        for aid, m in epoch.measurements.items():
            if aid not in self.anchors or m.status != 0:
                continue
            d = self._horizontal_distance(m, aid)
            if not (0.10 < d < self.max_range):
                continue
            a = self.anchors[aid]
            rows.append({"id": aid, "anchor": np.array([float(a["x_m"]), float(a["y_m"])], dtype=float),
                         "m": m, "distance": d})
        return rows

    @staticmethod
    def _geometry_ok(rows) -> bool:
        if len(rows) < 3:
            return False
        p = np.asarray([r["anchor"] for r in rows], dtype=float)
        return np.linalg.matrix_rank(p[1:] - p[0]) >= 2

    def _csi_reliability(self, row: dict[str, Any]) -> dict[str, float]:
        """Estimate reliability from CSI only (plus packet completeness).

        This is deliberately heuristic V1. It does NOT use anchor ID, indoor/outdoor
        labels, current position, or geometric residuals.
        """
        m: Measurement = row["m"]
        aid = row["id"]
        hist = self.histories[aid]

        packets_full = max(1.0, float(self.cw.get("packets_full", 6)))
        q_packets = clamp(m.csi_packets / packets_full, 0.0, 1.0)
        if m.csi_packets <= 0:
            return {"reliability": float(self.cw.get("csi_reliability_floor", 0.03)),
                    "sigma_csi": float(self.cw.get("csi_sigma_max_m", 6.0)),
                    "q_packets": 0.0, "q_rssi_strength": 0.25,
                    "q_rssi_stability": 0.15, "q_power_stability": 0.15,
                    "q_roughness": 0.15, "power_cv": 9.9}

        # Signal strength is deliberately weak evidence: strong reflection can also have high RSSI.
        center = float(self.cw.get("rssi_center_dbm", -78.0))
        scale = max(1.0, float(self.cw.get("rssi_scale_db", 8.0)))
        q_rssi_strength = 0.25 + 0.75 * sigmoid((m.csi_rssi_mean - center) / scale)

        q_rssi_stability = smooth_good_low(
            max(0.0, m.csi_rssi_std),
            float(self.cw.get("rssi_std_good_db", 1.5)),
            float(self.cw.get("rssi_std_bad_db", 8.0)))

        power_cv = abs(m.csi_power_std) / (abs(m.csi_power_mean) + 1e-6)
        q_power_stability = smooth_good_low(
            power_cv,
            float(self.cw.get("power_cv_good", 0.10)),
            float(self.cw.get("power_cv_bad", 0.80)))

        q_rough_abs = smooth_good_low(
            max(0.0, m.csi_roughness),
            float(self.cw.get("roughness_good", 0.25)),
            float(self.cw.get("roughness_bad", 3.50)))

        # A relative roughness term lets the same anchor adapt when the environment changes.
        q_rough_rel = 1.0
        if len(hist.roughness) >= 8:
            arr = np.asarray(hist.roughness, dtype=float)
            med = float(np.median(arr))
            mad = max(0.05, 1.4826 * float(np.median(np.abs(arr - med))))
            z = max(0.0, (m.csi_roughness - med) / mad)
            q_rough_rel = math.exp(-0.35 * min(z, 8.0))
        q_roughness = math.sqrt(max(q_rough_abs, 0.02) * max(q_rough_rel, 0.02))

        # Weighted geometric mean. Stability/roughness dominate; RSSI strength is weak evidence.
        terms = [
            (max(q_packets, 0.02), 1.0),
            (max(q_rssi_strength, 0.02), 0.35),
            (max(q_rssi_stability, 0.02), 1.2),
            (max(q_power_stability, 0.02), 1.2),
            (max(q_roughness, 0.02), 1.4),
        ]
        den = sum(w for _, w in terms)
        reliability = math.exp(sum(w * math.log(v) for v, w in terms) / den)
        reliability = clamp(reliability, float(self.cw.get("csi_reliability_floor", 0.03)), 1.0)

        sigma_min = float(self.cw.get("csi_sigma_min_m", 0.35))
        sigma_max = float(self.cw.get("csi_sigma_max_m", 6.0))
        sigma_csi = sigma_min + (1.0 - reliability) * (sigma_max - sigma_min)
        return {"reliability": reliability, "sigma_csi": sigma_csi,
                "q_packets": q_packets, "q_rssi_strength": q_rssi_strength,
                "q_rssi_stability": q_rssi_stability, "q_power_stability": q_power_stability,
                "q_roughness": q_roughness, "power_cv": power_cv}

    def _base_weight(self, row: dict[str, Any], center_us: float, duration_us: float):
        m = row["m"]; aid = row["id"]
        c = self._csi_reliability(row)
        sigma_range = robust_mad(self.histories[aid].distances, self.sigma_floor)
        q_entries = clamp(m.entries / max(1.0, float(self.cw.get("ftm_entries_full", 6))), 0.25, 1.0)
        sigma_t_us = max(duration_us, 150_000.0)
        q_time = math.exp(-0.5 * ((m.esp_us - center_us) / sigma_t_us) ** 2)
        # CSI reliability maps to expected ranging uncertainty. This is the key V1 idea.
        variance = self.sigma_floor**2 + sigma_range**2 + c["sigma_csi"]**2
        rel_exp = float(self.cw.get("reliability_exponent", 2.0))
        w = q_entries * q_time * (c["reliability"] ** rel_exp) / max(variance, 1e-6)
        return max(w, 1e-9), c, sigma_range, q_time

    def _solve_gn(self, rows, weights=None, initial=None, iterations=15):
        p = np.mean(np.asarray([r["anchor"] for r in rows]), axis=0) if initial is None else np.asarray(initial, dtype=float).copy()
        weights = np.ones(len(rows)) if weights is None else np.maximum(np.asarray(weights, dtype=float), 1e-9)
        J = np.zeros((len(rows),2))
        for _ in range(iterations):
            J=[]; residual=[]
            for row in rows:
                delta = p - row["anchor"]
                pred = max(float(np.linalg.norm(delta)), 1e-6)
                residual.append(pred - row["distance"]); J.append(delta/pred)
            J=np.asarray(J); r=np.asarray(residual)
            H=J.T @ (weights[:,None]*J) + np.eye(2)*1e-6
            g=J.T @ (weights*r)
            step=np.linalg.pinv(H) @ g
            p -= step
            if float(np.linalg.norm(step)) < 1e-4: break
        residual=np.asarray([float(np.linalg.norm(p-r["anchor"]))-r["distance"] for r in rows])
        return p,residual,J

    def _robust_initial(self, rows, base_weights):
        if len(rows) <= 3:
            return self._solve_gn(rows, weights=base_weights, initial=self.last_raw)[0]
        subset_n = min(max(3, self.preferred_anchors), len(rows))
        best=(math.inf,None)
        for idxs in itertools.combinations(range(len(rows)), subset_n):
            sub=[rows[i] for i in idxs]
            if not self._geometry_ok(sub): continue
            sw=np.asarray([base_weights[i] for i in idxs])
            p,_,_=self._solve_gn(sub, weights=sw, initial=self.last_raw, iterations=10)
            rr=np.asarray([abs(float(np.linalg.norm(p-r["anchor"]))-r["distance"]) for r in rows])
            # Score remains geometric only for selecting a numerically sane initial seed.
            score=float(np.median(rr)) + 0.25*float(np.sqrt(np.mean(np.sort(rr)[:max(3,len(rr)-1)]**2)))
            if score<best[0]: best=(score,p)
        return best[1] if best[1] is not None else self._solve_gn(rows, weights=base_weights, initial=self.last_raw)[0]

    def finalize(self, round_id: int, start_us: int, end_us: int, duration_us: int, measured_slots: int):
        epoch=self.epochs.pop(round_id, Epoch(round_id=round_id))
        rows=self._valid_rows(epoch)
        if len(rows)<self.min_anchors or not self._geometry_ok(rows):
            for r in rows: self._update_history(r)
            return None
        center=(start_us+end_us)/2.0
        base=[self._base_weight(r,center,duration_us) for r in rows]
        base_w=np.asarray([x[0] for x in base])
        seed=self._robust_initial(rows, base_w)

        # CSI-driven solve first.
        p0,res0,_=self._solve_gn(rows, weights=base_w, initial=seed, iterations=12)
        # Geometry is only a safety net: symmetric Huber downweights gross outliers.
        huber=np.ones(len(rows))
        for i,r in enumerate(res0):
            if abs(r)>self.huber_delta:
                huber[i]=self.huber_delta/max(abs(r),1e-6)
        final_w=base_w*huber
        p,res,J=self._solve_gn(rows, weights=final_w, initial=p0, iterations=12)

        max_w=float(np.max(final_w)) if len(final_w) else 1.0
        display_w=final_w/max(max_w,1e-12)
        rms=float(np.sqrt(np.average(res**2, weights=np.maximum(final_w,1e-9))))
        H=J.T @ (final_w[:,None]*J) + np.eye(2)*1e-6
        cov=np.linalg.pinv(H)*max(rms*rms,self.sigma_floor**2)
        cov=np.clip(cov,-25.0,25.0)
        cov[0,0]=clamp(float(cov[0,0]),0.08,25.0); cov[1,1]=clamp(float(cov[1,1]),0.08,25.0)
        raw=p.copy(); filt=self.kf.update(raw,center/1e6,cov) if self.enable_kalman else raw.copy(); self.last_raw=raw.copy()

        link_rows=[]
        for i,row in enumerate(rows):
            _,c,sigma_range,q_time=base[i]
            link_rows.append({"id":row["id"],"distance":row["distance"],"residual":float(res[i]),
                              "weight":float(display_w[i]),"solver_weight":float(final_w[i]),
                              "csi_reliability":float(c["reliability"]),"csi_sigma":float(c["sigma_csi"]),
                              "sigma":float(sigma_range),"time_weight":float(q_time),
                              "q_packets":float(c["q_packets"]),"q_rssi_strength":float(c["q_rssi_strength"]),
                              "q_rssi_stability":float(c["q_rssi_stability"]),"q_power_stability":float(c["q_power_stability"]),
                              "q_roughness":float(c["q_roughness"]),"measurement":row["m"]})
            self._update_history(row)
        self._log(round_id,start_us,end_us,duration_us,raw,filt,rms,link_rows)
        return {"round":round_id,"raw":raw,"filtered":filt,"rms":rms,"rows":link_rows,
                "duration_ms":duration_us/1000.0,"anchor_count":len(rows),
                "inlier_count":sum(abs(x["residual"])<=self.huber_delta for x in link_rows),"epoch":epoch}

    def _update_history(self,row):
        h=self.histories[row["id"]]; m=row["m"]
        h.distances.append(float(row["distance"])); h.roughness.append(max(0.0,float(m.csi_roughness)))
        h.csi_rssi_std.append(max(0.0,float(m.csi_rssi_std)))
        cv=abs(float(m.csi_power_std))/(abs(float(m.csi_power_mean))+1e-6) if m.csi_packets>0 else 9.9
        h.power_cv.append(cv)

    def _log(self,round_id,start_us,end_us,duration_us,raw,filt,rms,rows):
        pc=time.time()
        self.pos_writer.writerow({"pc_time_s":f"{pc:.6f}","round":round_id,"epoch_start_us":start_us,"epoch_end_us":end_us,
                                  "duration_ms":f"{duration_us/1000:.3f}","x_raw_m":f"{raw[0]:.6f}","y_raw_m":f"{raw[1]:.6f}",
                                  "x_filtered_m":f"{filt[0]:.6f}","y_filtered_m":f"{filt[1]:.6f}","rms_m":f"{rms:.6f}","anchor_count":len(rows)})
        for r in rows:
            m=r["measurement"]
            self.link_writer.writerow({"pc_time_s":f"{pc:.6f}","round":round_id,"anchor":r["id"],"esp_us":m.esp_us,
                "distance_m":f"{r['distance']:.6f}","ftm_rssi":f"{m.ftm_rssi:.2f}","entries":m.entries,"csi_packets":m.csi_packets,
                "csi_rssi_mean":f"{m.csi_rssi_mean:.3f}","csi_rssi_std":f"{m.csi_rssi_std:.3f}",
                "csi_power_mean":f"{m.csi_power_mean:.3f}","csi_power_std":f"{m.csi_power_std:.3f}","csi_roughness":f"{m.csi_roughness:.6f}",
                "csi_reliability":f"{r['csi_reliability']:.6f}","csi_sigma_m":f"{r['csi_sigma']:.6f}","range_sigma_m":f"{r['sigma']:.6f}",
                "residual_m":f"{r['residual']:.6f}","weight":f"{r['weight']:.8f}","q_packets":f"{r['q_packets']:.6f}",
                "q_rssi_strength":f"{r['q_rssi_strength']:.6f}","q_rssi_stability":f"{r['q_rssi_stability']:.6f}",
                "q_power_stability":f"{r['q_power_stability']:.6f}","q_roughness":f"{r['q_roughness']:.6f}"})
        self.pos_file.flush(); self.link_file.flush()
