"""Deterministic range-only 2D localization at a supplied antenna height.

This module has no serial, route, mark, calibration, or persistent-state access.
All ranges are *slant* distances.  The objective is the unweighted sum of
``(sqrt((x-a.x)^2 + (y-a.y)^2 + (height-a.z)^2) - measured_range)^2``.
The reported range residual is an internal fit diagnostic, not position error.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
from scipy.optimize import least_squares


def _number(value: Any) -> float | None:
    """Accept finite numeric values, excluding booleans and numeric strings."""
    if isinstance(value, (bool, str, bytes)):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _result(reason: str, used: list[str], excluded: list[dict[str, str]],
            condition: float | None = None, **kwargs: Any) -> dict[str, Any]:
    result = {
        "ok": False,
        "x": None,
        "y": None,
        "residual_rms_m": None,
        "anchors_n": len(used),
        "reason": reason,
        "condition_number": condition,
        "anchors_used": used,
        "anchors_excluded": excluded,
    }
    result.update(kwargs)
    return result


def solve_position(
    anchors: list[dict[str, Any]],
    distances: dict[str, float],
    tag_height_m: float,
    previous: tuple[float, float] | None = None,
) -> dict[str, Any]:
    """Fit x/y using >=3 noncollinear anchors and valid current ranges.

    ``anchors`` must contain unique nonempty string ``id`` values and finite
    ``x``, ``y``, ``z`` coordinates in metres.  Invalid anchor configuration or
    tag height fails explicitly. Missing, negative, or nonfinite ranges are
    excluded from this solve and identified in the return value; callers must
    preserve those original measurements in their raw recordings.

    ``previous`` is accepted for caller compatibility but is intentionally
    ignored: no route, truth, prior-position preference, bounds, snapping,
    clipping, smoothing, or fitted offset enters the objective. Correction,
    if desired, must be applied by the caller to the supplied range mapping.
    This function estimates x/y only; tag height is never estimated.
    """
    del previous
    used: list[str] = []
    excluded: list[dict[str, str]] = []
    height = _number(tag_height_m)
    if height is None:
        return _result("invalid_tag_height", used, excluded)
    if (not isinstance(anchors, Sequence) or isinstance(anchors, (str, bytes))
            or not isinstance(distances, Mapping)):
        return _result("invalid_input_container", used, excluded)

    configured: dict[str, tuple[float, float, float]] = {}
    for anchor in anchors:
        if not isinstance(anchor, Mapping):
            return _result("invalid_anchor_configuration", used, excluded)
        identity = anchor.get("id")
        if not isinstance(identity, str) or not identity.strip():
            return _result("invalid_anchor_id", used, excluded)
        if identity in configured:
            return _result("duplicate_anchor_id", used, excluded)
        xyz = tuple(_number(anchor.get(key)) for key in ("x", "y", "z"))
        if any(value is None for value in xyz):
            return _result("invalid_anchor_coordinates", used, excluded)
        configured[identity] = xyz

    rows: list[tuple[float, float, float]] = []
    ranges: list[float] = []
    # Sorting fixes all start/order/tie behaviour independently of JSON order.
    for identity in sorted(configured):
        if identity not in distances:
            excluded.append({"id": identity, "reason": "missing_range"})
            continue
        distance = _number(distances[identity])
        if distance is None:
            excluded.append({"id": identity, "reason": "nonfinite_or_invalid_range"})
            continue
        if distance < 0:
            excluded.append({"id": identity, "reason": "negative_range"})
            continue
        used.append(identity)
        rows.append(configured[identity])
        ranges.append(distance)

    if len(used) < 3:
        return _result("insufficient_valid_anchors", used, excluded)
    xyz = np.asarray(rows, dtype=float)
    measured = np.asarray(ranges, dtype=float)
    try:
        with np.errstate(over="raise", invalid="raise"):
            centre = np.mean(xyz[:, :2], axis=0)
            xy = xyz[:, :2] - centre
            singular = np.linalg.svd(xy, compute_uv=False)
            dz = height - xyz[:, 2]
    except (FloatingPointError, ValueError, np.linalg.LinAlgError):
        return _result("numerical_geometry_failure", used, excluded)
    if singular[0] == 0 or singular[1] <= singular[0] * 1e-10:
        return _result("rank_deficient_anchor_geometry", used, excluded)
    condition = float(singular[0] / singular[1])

    def residual(point: np.ndarray) -> np.ndarray:
        difference = point - xy
        return np.sqrt(np.sum(difference * difference, axis=1) + dz * dz) - measured

    def jacobian(point: np.ndarray) -> np.ndarray:
        difference = point - xy
        length = np.sqrt(np.sum(difference * difference, axis=1) + dz * dz)
        # Norm has no unique derivative exactly at a coplanar anchor. Zero is
        # a deterministic valid subgradient; other independent starts remain.
        return np.divide(difference, length[:, None],
                         out=np.zeros_like(difference), where=length[:, None] > 0)

    # Subtracting the mean squared-range equation removes the unknown x²+y²
    # term and yields a route-independent linear least-squares start.
    try:
        with np.errstate(over="raise", invalid="raise"):
            squared = np.sum(xy * xy, axis=1) + dz * dz - measured * measured
            linear, *_ = np.linalg.lstsq(2 * xy, squared - np.mean(squared), rcond=None)
    except (FloatingPointError, ValueError, np.linalg.LinAlgError):
        return _result("linear_initialization_failed", used, excluded, condition)
    seeds = [np.zeros(2), linear, *xy]
    best: tuple[float, np.ndarray] | None = None
    for seed in seeds:
        if not np.all(np.isfinite(seed)):
            continue
        try:
            fit = least_squares(residual, seed, jac=jacobian, method="trf",
                                ftol=1e-10, xtol=1e-10, gtol=1e-10,
                                max_nfev=200)
        except (ValueError, FloatingPointError, np.linalg.LinAlgError):
            continue
        if not fit.success or not np.all(np.isfinite(fit.x)):
            continue
        cost = float(np.dot(fit.fun, fit.fun))
        if math.isfinite(cost) and (best is None or cost < best[0]):
            best = (cost, fit.x)
    if best is None:
        return _result("optimization_failed", used, excluded, condition)
    point = best[1] + centre
    if not np.all(np.isfinite(point)):
        return _result("nonfinite_solution", used, excluded, condition)
    return _result("ok", used, excluded, condition, ok=True,
                   x=float(point[0]), y=float(point[1]),
                   residual_rms_m=math.sqrt(best[0] / len(used)))
