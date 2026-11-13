#!/usr/bin/env python3
"""Runtime support for data-driven, per-anchor range correction."""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np


class RangeCorrectionModel:
    """Load a monotonic piecewise model and predict a corrected range.

    ``shadow`` computes the candidate value but keeps the baseline value in the
    position solver. ``apply`` feeds the candidate value to the solver.
    """

    VALID_MODES = {"off", "shadow", "apply"}

    def __init__(self, config: dict[str, Any], base_dir: Path):
        settings = config.get("range_correction", {})
        mode = str(settings.get("mode", "off")).lower()
        self.mode = mode if mode in self.VALID_MODES else "off"
        model_path = Path(str(settings.get("model_path", "range_correction.json")))
        self.path = model_path if model_path.is_absolute() else base_dir / model_path
        self.max_abs_delta_m = max(0.0, float(settings.get("max_abs_delta_m", 3.0)))
        self.document: dict[str, Any] = {}
        self.anchors: dict[str, Any] = {}
        self.error = ""
        try:
            self.document = json.loads(self.path.read_text(encoding="utf-8"))
            if self.document.get("trained") is True:
                self.anchors = self.document.get("anchors", {})
        except (OSError, ValueError, TypeError) as exc:
            self.error = str(exc)

    @property
    def trained(self) -> bool:
        return bool(self.anchors)

    def predict(self, anchor_id: str, baseline_m: float) -> tuple[float, float, bool]:
        """Return ``(candidate_m, delta_m, model_available)``."""
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
