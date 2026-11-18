#!/usr/bin/env python3
"""Runtime support for data-driven, per-anchor range correction.

Two model families are supported:
  - ``pav``: monotonic piecewise residual curve (range_correction.json)
  - ``neural``: MC-Dropout MLP that fuses distance + CSI/RSSI features and
    also returns a per-sample uncertainty estimate (neural_correction.pt)

Modes: ``off`` (no correction), ``shadow`` (compute but keep baseline),
``apply`` (feed the PAV candidate to the solver), ``neural`` (feed the
neural candidate to the solver).
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Optional

import numpy as np


class NeuralCorrector:
    """MC-Dropout MLP range corrector persisted as a torch checkpoint."""

    FEATURES = [
        "ftm_rssi", "csi_packets", "csi_rssi_mean", "csi_rssi_std",
        "csi_power_mean", "csi_roughness", "nlos_probability",
    ]
    ANCHOR_IDS = [1, 2, 3, 5, 6, 7]

    def __init__(self, path: Path):
        import torch
        import torch.nn as nn
        self._torch = torch
        self._nn = nn
        checkpoint = torch.load(str(path), map_location="cpu", weights_only=False)
        self.mu = np.asarray(checkpoint["mu"], dtype=float)
        self.sd = np.asarray(checkpoint["sd"], dtype=float)
        self.net = self._build_net(int(self.mu.shape[0]))
        self.net.load_state_dict(checkpoint["state_dict"])
        self.net.eval()

    def _build_net(self, in_dim: int):
        return self._nn.Sequential(
            self._nn.Linear(in_dim, 64), self._nn.ReLU(), self._nn.Dropout(0.10),
            self._nn.Linear(64, 32), self._nn.ReLU(), self._nn.Dropout(0.10),
            self._nn.Linear(32, 1),
        )

    @classmethod
    def feature_vector(cls, anchor_id: int, baseline: float,
                       features: dict[str, float]) -> np.ndarray:
        vector = [baseline] + [float(features.get(name, 0.0)) for name in cls.FEATURES]
        vector += [1.0 if anchor_id == aid else 0.0 for aid in cls.ANCHOR_IDS]
        return np.asarray(vector, dtype=float)

    def predict(self, anchor_id: int, baseline: float,
                features: dict[str, float], samples: int = 20
                ) -> tuple[float, float, bool, float]:
        """Return (corrected_m, delta_m, available, uncertainty_m)."""
        x = self.feature_vector(anchor_id, baseline, features)
        xt = self._torch.tensor((x - self.mu) / self.sd,
                                dtype=self._torch.float32).view(1, -1)
        self.net.train()  # keep dropout active so the MC samples differ
        with self._torch.no_grad():
            preds = [float(self.net(xt).numpy()[0, 0]) for _ in range(samples)]
        self.net.eval()
        mean = float(np.mean(preds))
        std = float(np.std(preds))
        corrected = max(0.01, mean)
        return corrected, corrected - baseline, True, std


class RangeCorrectionModel:
    """Load a correction model and predict corrected ranges."""

    VALID_MODES = {"off", "shadow", "apply", "neural"}

    def __init__(self, config: dict[str, Any], base_dir: Path):
        settings = config.get("range_correction", {})
        mode = str(settings.get("mode", "off")).lower()
        self.mode = mode if mode in self.VALID_MODES else "off"
        self.max_abs_delta_m = max(0.0, float(settings.get("max_abs_delta_m", 3.0)))
        self.document: dict[str, Any] = {}
        self.anchors: dict[str, Any] = {}
        self.neural: Optional[NeuralCorrector] = None
        self.error = ""
        if self.mode == "neural":
            model_path = Path(str(settings.get("neural_path", "neural_correction.pt")))
            self.path = model_path if model_path.is_absolute() else base_dir / model_path
            try:
                self.neural = NeuralCorrector(self.path)
            except Exception as exc:  # noqa: BLE001 - report load failure to caller
                self.error = str(exc)
            return
        model_path = Path(str(settings.get("model_path", "range_correction.json")))
        self.path = model_path if model_path.is_absolute() else base_dir / model_path
        try:
            self.document = json.loads(self.path.read_text(encoding="utf-8"))
            if self.document.get("trained") is True:
                self.anchors = self.document.get("anchors", {})
        except (OSError, ValueError, TypeError) as exc:
            self.error = str(exc)

    @property
    def trained(self) -> bool:
        if self.mode == "neural":
            return self.neural is not None
        return bool(self.anchors)

    def predict(self, anchor_id: str, baseline_m: float,
                features: Optional[dict[str, float]] = None
                ) -> tuple[float, float, bool]:
        """Return ``(candidate_m, delta_m, model_available)``."""
        if self.mode == "neural":
            if self.neural is None or features is None:
                return baseline_m, 0.0, False
            corrected, delta, available, _sigma = self.neural.predict(
                int(anchor_id), baseline_m, features)
            return corrected, delta, available
        if self.mode == "off" or not math.isfinite(baseline_m) or baseline_m <= 0.0:
            return baseline_m, 0.0, False
        curve = self.anchors.get(str(anchor_id))
        if not isinstance(curve, dict):
            return baseline_m, 0.0, False
        raw = np.asarray(curve.get("raw_knots_m", []), dtype=float)
        delta = np.asarray(curve.get("delta_knots_m", []), dtype=float)
        if raw.size < 2 or raw.size != delta.size or not np.all(np.isfinite(raw + delta)):
            return baseline_m, 0.0, False
        order = np.argsort(raw)
        raw, delta = raw[order], delta[order]
        predicted_delta = float(np.interp(baseline_m, raw, delta))
        predicted_delta = float(np.clip(predicted_delta, -self.max_abs_delta_m,
                                        self.max_abs_delta_m))
        candidate = max(0.01, baseline_m + predicted_delta)
        return candidate, candidate - baseline_m, True

    def status(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "model_path": str(self.path),
            "trained": self.trained,
            "anchor_models": sorted(self.anchors, key=lambda value: int(value)),
            "load_error": self.error,
        }
