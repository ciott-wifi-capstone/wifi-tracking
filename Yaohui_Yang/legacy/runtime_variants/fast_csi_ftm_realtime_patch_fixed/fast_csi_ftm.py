#!/usr/bin/env python3
"""Very-lightweight CSI+FTM residual regression for live ranging.

No PyTorch is used at runtime.  Each Anchor has two small robust-ridge models:
  1) residual correction: true_range - FTM_range
  2) expected absolute error (sigma proxy) used as an extra WLS reliability weight

The runtime cost is only a small feature vector plus two dot products per Anchor.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np

FEATURE_NAMES = [
    "d", "d2_div25", "d3_div625", "sqrt_d", "log1p_d",
    "ftm_rssi", "csi_rssi_mean", "csi_rssi_std", "log1p_csi_power",
    "log1p_csi_roughness", "csi_packets", "prev_nlos", "past_spread",
    "d_x_rough_div10", "d_x_csi_std_div10", "d_x_csi_rssi_div100",
    "ftm_rssi_x_rough_div10", "csi_rssi_x_rough_div10",
    "prev_nlos_x_d_div10", "spread_x_d_div10",
    "prev_nlos_x_rough", "spread_x_rough",
]


def build_feature_vector(baseline_m: float, features: dict[str, float]) -> np.ndarray:
    d = max(float(baseline_m), 0.0)
    ftm_rssi = float(features.get("ftm_rssi", 0.0))
    csi_rssi = float(features.get("csi_rssi_mean", 0.0))
    csi_std = max(float(features.get("csi_rssi_std", 0.0)), 0.0)
    csi_power = max(float(features.get("csi_power_mean", 0.0)), 0.0)
    csi_rough = max(float(features.get("csi_roughness", 0.0)), 0.0)
    csi_packets = max(float(features.get("csi_packets", 0.0)), 0.0)
    prev_nlos = min(max(float(features.get("prev_nlos", 0.5)), 0.0), 1.0)
    spread = max(float(features.get("past_spread", 0.5)), 0.08)
    log_power = math.log1p(csi_power)
    log_rough = math.log1p(csi_rough)
    return np.asarray([
        d, d*d/25.0, d*d*d/625.0, math.sqrt(d), math.log1p(d),
        ftm_rssi, csi_rssi, csi_std, log_power, log_rough, csi_packets,
        prev_nlos, spread,
        d*log_rough/10.0, d*csi_std/10.0, d*csi_rssi/100.0,
        ftm_rssi*log_rough/10.0, csi_rssi*log_rough/10.0,
        prev_nlos*d/10.0, spread*d/10.0, prev_nlos*log_rough, spread*log_rough,
    ], dtype=float)


def _linear_predict(block: dict[str, Any], x: np.ndarray) -> float:
    mu = np.asarray(block["mu"], dtype=float)
    sd = np.asarray(block["sd"], dtype=float)
    beta = np.asarray(block["beta"], dtype=float)
    sd = np.where(sd < 1e-6, 1.0, sd)
    z = (x - mu) / sd
    return float(beta[0] + np.dot(beta[1:], z))


class FastCSIFTMModel:
    """Per-Anchor fast residual corrector + reliability estimator."""

    def __init__(self, config: dict[str, Any], base_dir: Path):
        cfg = config.get("fast_csi_ftm", {})
        self.enabled = bool(cfg.get("enabled", True))
        model_path = Path(str(cfg.get("model_path", "fast_csi_ftm_model.json")))
        self.path = model_path if model_path.is_absolute() else base_dir / model_path
        self.max_abs_delta_m = float(cfg.get("max_abs_delta_m", 30.0))
        self.min_sigma_m = float(cfg.get("min_sigma_m", 0.25))
        self.max_sigma_m = float(cfg.get("max_sigma_m", 8.0))
        self.mode = "fast" if self.enabled else "off"
        self.document: dict[str, Any] = {"method": "none"}
        self.anchors: dict[str, Any] = {}
        self.error = ""
        if not self.enabled:
            return
        try:
            self.document = json.loads(self.path.read_text(encoding="utf-8"))
            if self.document.get("trained") is True:
                self.anchors = self.document.get("anchors", {})
            else:
                self.error = "model is not marked trained"
        except Exception as exc:
            self.error = str(exc)
            self.anchors = {}

    @property
    def trained(self) -> bool:
        return bool(self.anchors)

    def predict(self, anchor_id: str, baseline_m: float,
                features: dict[str, float]) -> tuple[float, float, float, bool]:
        if not self.enabled or not math.isfinite(baseline_m) or baseline_m <= 0.0:
            return baseline_m, 0.0, math.nan, False
        block = self.anchors.get(str(anchor_id))
        if not isinstance(block, dict):
            return baseline_m, 0.0, math.nan, False
        try:
            x = build_feature_vector(baseline_m, features)
            delta = _linear_predict(block["correction"], x)
            delta = float(np.clip(delta, -self.max_abs_delta_m, self.max_abs_delta_m))
            corrected = max(0.05, baseline_m + delta)
            log_error = _linear_predict(block["sigma"], x)
            sigma = math.exp(float(np.clip(log_error, -4.0, 4.0))) - 0.25
            sigma = float(np.clip(sigma, self.min_sigma_m, self.max_sigma_m))
            return corrected, corrected - baseline_m, sigma, True
        except Exception:
            return baseline_m, 0.0, math.nan, False

    def status(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "model_path": str(self.path),
            "trained": self.trained,
            "anchor_models": sorted(self.anchors, key=lambda v: int(v)),
            "method": self.document.get("method", "none"),
            "load_error": self.error,
        }
