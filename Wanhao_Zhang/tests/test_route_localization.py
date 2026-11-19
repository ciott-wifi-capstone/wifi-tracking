"""Scientific invariants for the independent live range-position solver."""

import copy
import importlib.util
import json
import math
from pathlib import Path

import numpy as np
import pytest
from scipy.optimize import least_squares

SOURCE = Path(__file__).resolve().parents[1] / "src" / "route_localization.py"
SPEC = importlib.util.spec_from_file_location("route_localization", SOURCE)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
solve_position = MODULE.solve_position

ANCHORS = [
    {"id": "A1", "x": 18.08, "y": 12.90, "z": 1.50},
    {"id": "A2", "x": 34.90, "y": 14.36, "z": 1.36},
    {"id": "A3", "x": 46.87, "y": 12.87, "z": 1.30},
    {"id": "A4", "x": 27.42, "y": 6.40, "z": 1.22},
    {"id": "A5", "x": 35.40, "y": 6.40, "z": 1.32},
    {"id": "A6", "x": 45.67, "y": 5.07, "z": 1.24},
]


def ranges_at(x, y, z, anchors=ANCHORS):
    return {a["id"]: math.dist((x, y, z), (a["x"], a["y"], a["z"]))
            for a in anchors}


@pytest.mark.parametrize("point", [(19.09, 13.56), (33.0, 9.0), (55.0, -3.0)])
def test_recovers_exact_slant_geometry_inside_and_outside_anchor_hull(point):
    result = solve_position(ANCHORS, ranges_at(*point, 1.22), 1.22)
    assert result["ok"]
    assert (result["x"], result["y"]) == pytest.approx(point, abs=1e-7)
    assert result["residual_rms_m"] < 1e-8
    assert result["anchors_n"] == 6
    assert result["condition_number"] >= 1


def test_three_noncollinear_anchors_suffice_and_height_is_not_flattened():
    anchors = [
        {"id": "one", "x": 0, "y": 0, "z": 0.0},
        {"id": "two", "x": 8, "y": 0, "z": 5.0},
        {"id": "three", "x": 0, "y": 9, "z": 2.5},
    ]
    result = solve_position(anchors, ranges_at(2, 3, 1.22, anchors), 1.22)
    assert result["ok"]
    assert (result["x"], result["y"]) == pytest.approx((2, 3), abs=1e-7)


def test_invalid_ranges_are_excluded_without_mutating_measurements():
    distances = ranges_at(30, 10, 1.22)
    distances.update(A1=-0.9, A2=float("nan"))
    del distances["A3"]
    old_keys = set(distances)
    result = solve_position(ANCHORS, distances, 1.22)
    assert result["ok"]
    assert result["anchors_used"] == ["A4", "A5", "A6"]
    assert {r["id"]: r["reason"] for r in result["anchors_excluded"]} == {
        "A1": "negative_range", "A2": "nonfinite_or_invalid_range", "A3": "missing_range"}
    assert set(distances) == old_keys
    assert distances["A1"] == -0.9 and math.isnan(distances["A2"])
    assert (result["x"], result["y"]) == pytest.approx((30, 10), abs=1e-6)


@pytest.mark.parametrize("distances", [{}, {"A1": 1, "A2": 2},
                                      {"A1": 1, "A2": -1, "A3": float("inf")}])
def test_no_estimate_with_fewer_than_three_valid_ranges(distances):
    result = solve_position(ANCHORS, distances, 1.22)
    assert not result["ok"]
    assert result["reason"] == "insufficient_valid_anchors"
    assert result["x"] is None and result["residual_rms_m"] is None
    json.dumps(result, allow_nan=False)


def test_collinear_anchor_geometry_rejected_even_with_many_ranges():
    anchors = [{"id": str(i), "x": i, "y": 2 * i + 3, "z": i / 10}
               for i in range(5)]
    result = solve_position(anchors, ranges_at(2, 4, 1.22, anchors), 1.22)
    assert not result["ok"]
    assert result["reason"] == "rank_deficient_anchor_geometry"


@pytest.mark.parametrize("change,reason", [
    ("duplicate", "duplicate_anchor_id"),
    ("nan_coordinate", "invalid_anchor_coordinates"),
    ("missing_id", "invalid_anchor_id"),
])
def test_invalid_anchor_configuration_fails_explicitly(change, reason):
    anchors = copy.deepcopy(ANCHORS)
    if change == "duplicate":
        anchors[1]["id"] = anchors[0]["id"]
    elif change == "nan_coordinate":
        anchors[1]["x"] = float("nan")
    else:
        del anchors[1]["id"]
    result = solve_position(anchors, ranges_at(30, 10, 1.22), 1.22)
    assert not result["ok"] and result["reason"] == reason


@pytest.mark.parametrize("height", [None, float("nan"), float("inf"), True, "1.22"])
def test_invalid_height_fails_instead_of_estimating_z(height):
    result = solve_position(ANCHORS, ranges_at(30, 10, 1.22), height)
    assert not result["ok"] and result["reason"] == "invalid_tag_height"


def test_correction_is_only_the_supplied_range_change_and_previous_is_ignored():
    truth = ranges_at(30, 10, 1.22)
    offset = 1.6817616377125457
    biased = {key: value + offset for key, value in truth.items()}
    corrected = {key: value - offset for key, value in biased.items()}
    original = solve_position(ANCHORS, truth, 1.22)
    debiased = solve_position(ANCHORS, corrected, 1.22, previous=(-999999, 999999))
    assert debiased["x"] == pytest.approx(original["x"], abs=1e-9)
    assert debiased["y"] == pytest.approx(original["y"], abs=1e-9)
    assert solve_position(ANCHORS, biased, 1.22) == solve_position(
        ANCHORS, biased, 1.22, previous=(30, 10))
    assert solve_position(ANCHORS, biased, 1.22)["residual_rms_m"] > 0.1


def test_unweighted_residual_objective_and_order_invariance():
    distances = ranges_at(31, 9, 1.22)
    distances["A1"] += 4
    distances["A4"] -= 0.8
    result = solve_position(ANCHORS, distances, 1.22)
    reordered = solve_position(list(reversed(ANCHORS)), distances, 1.22)
    assert result == reordered
    xyz = np.asarray([[a["x"], a["y"], a["z"]] for a in ANCHORS])
    measured = np.asarray([distances[a["id"]] for a in ANCHORS])

    def independent_objective(point):
        p = np.asarray([*point, 1.22])
        return np.linalg.norm(p - xyz, axis=1) - measured

    check = least_squares(independent_objective, [31, 9],
                          ftol=1e-12, xtol=1e-12, gtol=1e-12)
    assert (result["x"], result["y"]) == pytest.approx(check.x, abs=2e-5)
    errors = independent_objective([result["x"], result["y"]])
    assert result["residual_rms_m"] == pytest.approx(float(np.sqrt(np.mean(errors ** 2))))


def test_zero_range_is_valid_and_does_not_force_a_hidden_lower_clip():
    anchors = [
        {"id": "A", "x": 0, "y": 0, "z": 1.22},
        {"id": "B", "x": 5, "y": 0, "z": 1.22},
        {"id": "C", "x": 0, "y": 5, "z": 1.22},
    ]
    result = solve_position(anchors, {"A": 0, "B": 5, "C": 5}, 1.22)
    assert result["ok"] and result["anchors_n"] == 3
    assert (result["x"], result["y"]) == pytest.approx((0, 0), abs=1e-7)


def test_solver_signature_cannot_accept_route_or_manual_truth():
    with pytest.raises(TypeError):
        solve_position(ANCHORS, ranges_at(30, 10, 1.22), 1.22,
                       route=[(30, 10)], manual_mark=(30, 10))


def test_numerically_unrepresentable_geometry_fails_without_nan_json():
    anchors = [{"id": str(i), "x": 1.7e308, "y": i, "z": 1.22}
               for i in range(4)]
    result = solve_position(anchors, {str(i): 1.0 for i in range(4)}, 1.22)
    assert not result["ok"]
    assert result["reason"] == "numerical_geometry_failure"
    json.dumps(result, allow_nan=False)
