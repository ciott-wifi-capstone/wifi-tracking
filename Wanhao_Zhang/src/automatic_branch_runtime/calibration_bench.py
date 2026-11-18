#!/usr/bin/env python3
"""Signal-only ranging calibration with physical-pair holdout and model export.

All fitted preprocessing lives inside the training fold. A physical pair groups
both radio directions; frames are never randomly split. P1 was inspected before
this experiment and is only a retrospective transfer check. Nothing here opens a
serial port, changes a capture, edits firmware or changes the frozen candidate.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import math
import pathlib
import sys
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np
from scipy.optimize import minimize

ROOT = pathlib.Path(__file__).resolve().parents[2]
PROJECT = pathlib.Path(r"C:\Users\ww820\Desktop\DP_workspce\05_wipro_baseline")
FROZEN_OFFSET = 1.6817616377125457
FROZEN_SHA256 = "6444d000dc59b448c21878e0a615c62b371b22abd6b04780629a155dd1e60912"
FROZEN_FILE = ROOT / "outputs/results/pairwise_20261017_R1_global_offset_candidate/candidate.json"
SIGNAL_FEATURES = ["wipro_m", "raw_minus_wipro_m", "rssi_dbm"]
CSI_FEATURES = SIGNAL_FEATURES + ["csi_amp_mean", "csi_amp_std", "csi_amp_cv",
                                  "csi_missing", "rssi_missing"]
# The same 114 observed subcarriers listed in pinned firmware/src/wipro.rs.
ACTIVE_40 = list(range(2, 59)) + list(range(70, 127))
FIT_METHODS = ["group_global_offset", "robust_affine", "ridge_signal",
               "ridge_signal_csi", "tiny_mlp_signal_csi"]
OOF_METHODS = ["raw", "wipro"] + FIT_METHODS
TRANSFER_METHODS = ["raw", "wipro", "frozen_global_offset"] + FIT_METHODS
PROTOCOL = {
    "version": "calibration_pair_holdout_v1",
    "seed": 4712,
    "split": "leave-one-physical-pair-out; both directions in the same fold",
    "fitting_weight": "equal physical-pair weight, equal direction weight within pair, equal event weight within direction",
    "model_selection": "fixed methods and hyperparameters; no result-driven tuning; no P1 fitting",
    "global_offset": "median directional signed residual, median directions per pair, median physical pairs",
    "robust_affine": {"loss": "soft_l1", "f_scale_m": 1.0, "l2": 0.0,
                      "optimizer": "L-BFGS-B", "maxiter": 400},
    "ridge": {"residual_target": "wipro minus truth", "alpha": 0.1,
              "objective": "pair-weighted mean squared residual + alpha * sum(scaled slope squared)",
              "intercept_penalty": False},
    "tiny_mlp": {"hidden_units": 8, "activation": "tanh", "epochs": 400,
                 "optimizer": "full-batch Adam", "learning_rate": 0.01,
                 "l2": 0.01, "residual_target": "training-scaled wipro minus truth",
                 "early_stopping": False, "target_derived_features": False},
    "scaling": "train-only pair-weighted mean and standard deviation",
    "missing_features": "train-only weighted median imputation; explicit CSI/RSSI missing flags in CSI model",
    "inference_inputs": "current unique event Wi-PRO, raw RTT, same-event RSSI and embedded CSI only",
    "excluded_inputs": ["MAC", "node label", "coordinates", "reference distance", "LOS labels", "time", "route"],
    "output_clipping": False,
    "P1_role": "retrospective transfer check at one previously examined static position; not untouched final validation",
    "interpretation": "D0 comprises 15 physical pairs from one fixed deployment/day; CV is internal development evidence, not new-environment generalization",
}


def sha256(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def read_csv(path):
    with pathlib.Path(path).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def dump_json(path, obj):
    pathlib.Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2,
                                          allow_nan=False), encoding="utf-8")


def write_csv(path, rows):
    if not rows:
        raise ValueError("Cannot export an empty CSV")
    fields = list(rows[0])
    with pathlib.Path(path).open("w", encoding="utf-8", newline="") as handle:
        out = csv.DictWriter(handle, fieldnames=fields)
        out.writeheader()
        out.writerows(rows)


def event_key(row):
    return (row["own_mac"].strip().upper(),
            row.get("target_mac", row.get("tgt_mac", "")).strip().upper(), int(row["t2"]))


def signed48diff(a, b):
    value = (int(a) - int(b)) & ((1 << 48) - 1)
    return value - (1 << 48) if value >= (1 << 47) else value


def raw_range(row):
    return (signed48diff(row["t4"], row["t1"]) - signed48diff(row["t3"], row["t2"])) * 299792458e-12 / 2


def extract_signal_features(wipro_m, raw_m, rssi=None, payload_b64=None):
    """Public event-local features; references and identities cannot enter here.

    The pinned upstream load_h_40 decodes bytes 128..383 as signed int8
    [imaginary, real]. Only magnitude summaries of its 114 observed carriers are
    used, before normalization. They are descriptive signal features, not a
    claimed physical channel impulse response or a calibrated path delay.
    """
    try:
        rssi_value = float(rssi)
        if not math.isfinite(rssi_value):
            raise ValueError("Nonfinite RSSI")
        rssi_missing = 0.0
    except (TypeError, ValueError):
        rssi_value, rssi_missing = float("nan"), 1.0
    mean = std = cv = float("nan")
    missing = 1.0
    try:
        data = base64.b64decode(payload_b64 or "", validate=True)
        if len(data) != 384:
            raise ValueError("CSI is not HT40 384-byte data")
        iq = np.frombuffer(data[128:], dtype=np.int8).astype(float)
        amplitudes = np.hypot(iq[0::2], iq[1::2])[ACTIVE_40]
        mean, std = float(amplitudes.mean()), float(amplitudes.std())
        if mean <= 0:
            raise ValueError("Zero CSI magnitude")
        cv = std / mean
        missing = 0.0
    except (TypeError, ValueError):
        mean = std = cv = float("nan")
    return {"wipro_m": float(wipro_m), "raw_minus_wipro_m": float(raw_m) - float(wipro_m),
            "rssi_dbm": rssi_value, "csi_amp_mean": mean, "csi_amp_std": std,
            "csi_amp_cv": cv, "csi_missing": missing, "rssi_missing": rssi_missing}


def attach_features(rows, capture):
    events, conflicts = {}, set()
    for ftm in read_csv(capture / "ftm.csv"):
        key = event_key(ftm)
        signature = tuple(ftm[name] for name in ("t1", "t2", "t3", "t4", "rssi", "payload_b64"))
        if key in events and events[key][0] != signature:
            conflicts.add(key)
        else:
            events[key] = (signature, ftm)
    missing, csi_missing, rssi_missing = 0, 0, 0
    for row in rows:
        key = event_key(row)
        if key not in events or key in conflicts:
            # Keep the exact same events for every method. Missing features are
            # handled using training-only imputation, never winner-specific drops.
            ftm = {}
            missing += 1
        else:
            ftm = events[key][1]
            if not math.isclose(raw_range(ftm), row["raw_m"], abs_tol=1e-8, rel_tol=0):
                raise ValueError(f"Exact-key raw RTT mismatch: {capture}/{key}")
        row["features"] = extract_signal_features(row["wipro_m"], row["raw_m"],
                                                  ftm.get("rssi"), ftm.get("payload_b64"))
        csi_missing += int(row["features"]["csi_missing"])
        rssi_missing += int(row["features"]["rssi_missing"])
    return {"capture": str(capture), "events_n": len(rows), "ftm_missing_or_conflicting_n": missing,
            "csi384_missing_n": csi_missing, "rssi_missing_n": rssi_missing,
            "ftm_conflicting_keys_n": len(conflicts)}


def normalize_rows(rows):
    seen = set()
    for row in rows:
        for field in ("truth_m", "wipro_m", "raw_m"):
            row[field] = float(row[field])
            if not math.isfinite(row[field]):
                raise ValueError(f"Nonfinite {field}")
        own, target, t2 = event_key(row)
        key = (row["session_id"], own, target, t2)
        if key in seen:
            raise ValueError("Duplicate paired event")
        seen.add(key)
        row["physical_pair"] = "|".join(sorted([own, target]))
        row["directed_link"] = own + "|" + target
        row["own_mac"], row["target_mac"], row["t2"] = own, target, t2
    return rows


def pair_balanced_weights(rows):
    """Each physical pair equal; each observed direction within it equal."""
    counts = defaultdict(lambda: defaultdict(int))
    for row in rows:
        counts[row["physical_pair"]][row["directed_link"]] += 1
    weights = np.asarray([1 / (len(counts) * len(counts[row["physical_pair"]]) *
                               counts[row["physical_pair"]][row["directed_link"]]) for row in rows])
    if not math.isclose(float(weights.sum()), 1.0, abs_tol=1e-12):
        raise ValueError("Weight normalization error")
    return weights


def grouped_folds(rows):
    groups = np.asarray([row["physical_pair"] for row in rows])
    for group in sorted(set(groups)):
        train, test = np.flatnonzero(groups != group), np.flatnonzero(groups == group)
        if not len(train) or not len(test):
            raise ValueError("A grouped holdout needs at least two groups")
        yield group, train, test


def weighted_quantile(values, weights, q):
    order = np.argsort(values, kind="stable")
    values, weights = np.asarray(values)[order], np.asarray(weights)[order]
    index = np.searchsorted(np.cumsum(weights) / weights.sum(), q, side="left")
    return float(values[min(int(index), len(values) - 1)])


def fit_scaler(feature_rows, names, weights):
    X = np.asarray([[row.get(name, float("nan")) for name in names] for row in feature_rows], dtype=float)
    fills = []
    for column in X.T:
        mask = np.isfinite(column)
        fills.append(weighted_quantile(column[mask], weights[mask], 0.5) if mask.any() else 0.0)
    X = np.where(np.isfinite(X), X, np.asarray(fills))
    mean = weights @ X
    scale = np.sqrt(weights @ ((X - mean) ** 2))
    scale[scale < 1e-9] = 1.0
    return {"feature_names": names, "fill_values": fills, "mean": mean.tolist(), "scale": scale.tolist()}


def transform(scaler, feature_rows):
    X = np.asarray([[row.get(name, float("nan")) for name in scaler["feature_names"]]
                    for row in feature_rows], dtype=float)
    X = np.where(np.isfinite(X), X, scaler["fill_values"])
    return (X - scaler["mean"]) / scaler["scale"]


def group_offset(rows):
    directions = defaultdict(list)
    for row in rows:
        directions[(row["physical_pair"], row["directed_link"])].append(row["wipro_m"] - row["truth_m"])
    pairs = defaultdict(list)
    for (pair, _), residuals in directions.items():
        pairs[pair].append(float(np.median(residuals)))
    return float(np.median([np.median(values) for values in pairs.values()]))


def fit_model(method, rows):
    weights = pair_balanced_weights(rows)
    features = [row["features"] for row in rows]
    wipro = np.asarray([row["wipro_m"] for row in rows])
    truth = np.asarray([row["truth_m"] for row in rows])
    if method == "group_global_offset":
        return {"method": method, "offset_to_subtract_m": group_offset(rows)}
    if method == "robust_affine":
        # Optimize directly in training-standardized Wi-PRO coordinates.
        scaler = fit_scaler(features, ["wipro_m"], weights)
        x = transform(scaler, features)[:, 0]
        start = np.asarray([scaler["scale"][0], scaler["mean"][0] - group_offset(rows)])

        def objective(parameters):
            residual = parameters[0] * x + parameters[1] - truth
            root = np.sqrt(1 + residual ** 2)
            gradient = 2 * residual / root
            return float(weights @ (2 * (root - 1))), np.asarray([weights @ (gradient * x), weights @ gradient])

        result = minimize(objective, start, jac=True, method="L-BFGS-B",
                          options={"maxiter": 400, "ftol": 1e-12, "gtol": 1e-8})
        if not result.success:
            raise ValueError(f"Robust affine optimizer failed: {result.message}")
        return {"method": method, "scaler": scaler, "slope_scaled": float(result.x[0]),
                "intercept_m": float(result.x[1]), "optimizer_iterations": int(result.nit)}
    names = SIGNAL_FEATURES if method == "ridge_signal" else CSI_FEATURES
    scaler = fit_scaler(features, names, weights)
    X = transform(scaler, features)
    residual_target = wipro - truth
    if method in ("ridge_signal", "ridge_signal_csi"):
        X1 = np.column_stack([np.ones(len(X)), X])
        penalty = np.eye(X1.shape[1]) * PROTOCOL["ridge"]["alpha"]
        penalty[0, 0] = 0
        coefficients = np.linalg.solve(X1.T @ (weights[:, None] * X1) + penalty,
                                       X1.T @ (weights * residual_target))
        return {"method": method, "scaler": scaler, "coefficients": coefficients.tolist(),
                "alpha": PROTOCOL["ridge"]["alpha"]}
    if method != "tiny_mlp_signal_csi":
        raise ValueError(f"Unknown model: {method}")
    target_mean = float(weights @ residual_target)
    target_scale = max(float(np.sqrt(weights @ (residual_target - target_mean) ** 2)), 1e-9)
    y = (residual_target - target_mean) / target_scale
    rng = np.random.default_rng(PROTOCOL["seed"])
    hidden = PROTOCOL["tiny_mlp"]["hidden_units"]
    parameters = [rng.normal(0, 1 / math.sqrt(X.shape[1]), (X.shape[1], hidden)),
                  np.zeros(hidden), rng.normal(0, 1 / math.sqrt(hidden), hidden), np.zeros(1)]
    moment1, moment2 = [np.zeros_like(p) for p in parameters], [np.zeros_like(p) for p in parameters]
    losses = []
    l2 = PROTOCOL["tiny_mlp"]["l2"]
    for epoch in range(1, PROTOCOL["tiny_mlp"]["epochs"] + 1):
        W, b, v, c = parameters
        activation = np.tanh(X @ W + b)
        error = activation @ v + c[0] - y
        weighted_error = 2 * weights * error
        delta = weighted_error[:, None] * v * (1 - activation ** 2)
        gradients = [X.T @ delta + 2 * l2 * W, delta.sum(axis=0),
                     activation.T @ weighted_error + 2 * l2 * v, np.asarray([weighted_error.sum()])]
        loss = float(weights @ error ** 2 + l2 * ((W ** 2).sum() + (v ** 2).sum()))
        if epoch in (1, PROTOCOL["tiny_mlp"]["epochs"]):
            losses.append({"epoch": epoch, "pre_update_objective": loss})
        for i, gradient in enumerate(gradients):
            moment1[i] = 0.9 * moment1[i] + 0.1 * gradient
            moment2[i] = 0.999 * moment2[i] + 0.001 * gradient ** 2
            parameters[i] -= PROTOCOL["tiny_mlp"]["learning_rate"] * (moment1[i] / (1 - 0.9 ** epoch)) / (
                np.sqrt(moment2[i] / (1 - 0.999 ** epoch)) + 1e-8)
    W, b, v, c = parameters
    final_activation = np.tanh(X @ W + b)
    final_error = final_activation @ v + c[0] - y
    final_loss = float(weights @ final_error ** 2 + l2 * ((W ** 2).sum() + (v ** 2).sum()))
    final_weighted_error = 2 * weights * final_error
    final_delta = final_weighted_error[:, None] * v * (1 - final_activation ** 2)
    final_gradients = [X.T @ final_delta + 2 * l2 * W, final_delta.sum(axis=0),
                       final_activation.T @ final_weighted_error + 2 * l2 * v,
                       np.asarray([final_weighted_error.sum()])]
    return {"method": method, "scaler": scaler, "W": parameters[0].tolist(), "b": parameters[1].tolist(),
            "v": parameters[2].tolist(), "c": float(parameters[3][0]), "target_mean_m": target_mean,
            "target_scale_m": target_scale, "seed": PROTOCOL["seed"], "training_objective": losses,
            "final_objective": final_loss,
            "final_gradient_norm": float(np.sqrt(sum((g ** 2).sum() for g in final_gradients))),
            "optimizer_status": "fixed_400_epoch_budget_completed; convergence_not_asserted",
            "converged": None}


def predict_model(model, feature_rows):
    """Public causal inference API returning untrimmed ranges in metres.

    feature_rows comes from extract_signal_features; it contains no reference
    positions or labels. This function performs no fitting or route projection.
    """
    method = model["method"]
    wipro = np.asarray([row["wipro_m"] for row in feature_rows], dtype=float)
    if method in ("group_global_offset", "frozen_global_offset"):
        prediction = wipro - model["offset_to_subtract_m"]
    elif method == "robust_affine":
        prediction = model["slope_scaled"] * transform(model["scaler"], feature_rows)[:, 0] + model["intercept_m"]
    elif method in ("ridge_signal", "ridge_signal_csi"):
        X = transform(model["scaler"], feature_rows)
        prediction = wipro - np.column_stack([np.ones(len(X)), X]) @ np.asarray(model["coefficients"])
    elif method == "tiny_mlp_signal_csi":
        X = transform(model["scaler"], feature_rows)
        residual = (np.tanh(X @ np.asarray(model["W"]) + np.asarray(model["b"])) @ np.asarray(model["v"]) +
                    model["c"]) * model["target_scale_m"] + model["target_mean_m"]
        prediction = wipro - residual
    else:
        raise ValueError(f"Unknown exported model {method}")
    if not np.isfinite(prediction).all():
        raise ValueError("Nonfinite prediction; all finite ranges must be retained without clipping")
    return prediction


def error_metrics(predictions, rows):
    truth = np.asarray([row["truth_m"] for row in rows])
    errors = np.asarray(predictions) - truth
    absolute = np.abs(errors)
    weights = pair_balanced_weights(rows)
    return {"events_n": len(rows), "physical_pairs_n": len({r["physical_pair"] for r in rows}),
            "directed_links_n": len({r["directed_link"] for r in rows}),
            "event_mae_m": float(absolute.mean()), "event_rmse_m": float(np.sqrt(np.mean(errors ** 2))),
            "event_p90_abs_error_m": float(np.percentile(absolute, 90)), "event_bias_m": float(errors.mean()),
            "pair_balanced_mae_m": float(weights @ absolute),
            "pair_balanced_rmse_m": float(np.sqrt(weights @ errors ** 2)),
            "pair_balanced_p90_abs_error_m": weighted_quantile(absolute, weights, 0.9),
            "pair_balanced_bias_m": float(weights @ errors),
            "negative_prediction_n": int((np.asarray(predictions) < 0).sum()),
            "max_abs_error_m": float(absolute.max())}


def export_prediction_rows(rows, predictions, folds=None):
    exported = []
    for i, row in enumerate(rows):
        result = {name: row[name] for name in ("session_id", "own_label", "target_label", "own_mac", "target_mac",
                                               "t2", "physical_pair", "truth_m", "raw_m", "wipro_m")}
        result["fold"] = folds[i] if folds is not None else "D0_full_fit_to_P1"
        for method, values in predictions.items():
            result[method + "_prediction_m"] = float(values[i])
        exported.append(result)
    return exported


def per_link_metrics(rows, predictions, stage):
    links = defaultdict(list)
    for i, row in enumerate(rows):
        links[row["directed_link"]].append(i)
    out = []
    for link, indexes in sorted(links.items()):
        subset = [rows[i] for i in indexes]
        for method, values in predictions.items():
            out.append({"stage": stage, "method": method, "directed_link": link,
                        "own_label": subset[0]["own_label"], "target_label": subset[0]["target_label"],
                        **error_metrics(np.asarray(values)[indexes], subset)})
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=pathlib.Path, default=ROOT / "outputs/results/optimization_20261017/calibration")
    args = parser.parse_args()
    out = args.out.resolve()
    if out.exists():
        raise FileExistsError(f"Refusing to overwrite benchmark: {out}")
    out.mkdir(parents=True)
    protocol_file = out / "protocol.json"
    dump_json(protocol_file, {"declared_utc": datetime.now(timezone.utc).isoformat(), **PROTOCOL})
    d0_csv = ROOT / "outputs/results/pairwise_20261017_R1_summary/static_evaluate/paired_events.csv"
    round_file = ROOT / "outputs/results/pairwise_20261017_R1_summary/round_summary.json"
    p1_cap = ROOT / "outputs/results/A0_static_20261017_160535_P1"
    p1_csv = p1_cap / "analysis/paired_events.csv"
    inputs = [d0_csv, round_file, p1_csv, p1_cap / "analysis/summary.json", FROZEN_FILE,
              PROJECT / "firmware/src/wipro.rs"]
    if sha256(FROZEN_FILE) != FROZEN_SHA256:
        raise ValueError("Frozen global candidate has changed")
    d0 = normalize_rows(read_csv(d0_csv))
    p1 = normalize_rows(read_csv(p1_csv))
    if len(d0) != 11827 or len({r["physical_pair"] for r in d0}) != 15:
        raise ValueError("Unexpected current D0 coverage")
    if len(p1) != 1235 or len({r["directed_link"] for r in p1}) != 6:
        raise ValueError("Unexpected P1 coverage")
    round_summary = json.loads(round_file.read_text(encoding="utf-8"))
    coverage = []
    for entry in round_summary["by_initiator"].values():
        capture = pathlib.Path(entry["capture"])
        subset = [row for row in d0 if row["session_id"] == entry["session_id"]]
        coverage.append(attach_features(subset, capture))
        inputs.extend([capture / "ftm.csv", capture / "capture_metadata.json"])
    coverage.append(attach_features(p1, p1_cap))
    inputs.extend([p1_cap / "ftm.csv", p1_cap / "capture_metadata.json"])
    if any("features" not in row for row in d0 + p1):
        raise ValueError("A paired event did not map to its capture")
    input_hashes = {str(path): sha256(path) for path in inputs}
    predictions = {"raw": np.asarray([r["raw_m"] for r in d0]),
                   "wipro": np.asarray([r["wipro_m"] for r in d0])}
    predictions.update({name: np.full(len(d0), np.nan) for name in FIT_METHODS})
    folds = [None] * len(d0)
    fold_records, fold_models = [], []
    for fold_index, (group, train_indexes, test_indexes) in enumerate(grouped_folds(d0), 1):
        train_rows = [d0[i] for i in train_indexes]
        test_rows = [d0[i] for i in test_indexes]
        test_features = [r["features"] for r in test_rows]
        models = {method: fit_model(method, train_rows) for method in FIT_METHODS}
        for method, model in models.items():
            predictions[method][test_indexes] = predict_model(model, test_features)
        for i in test_indexes:
            folds[i] = group
        train_groups = sorted({row["physical_pair"] for row in train_rows})
        if group in train_groups:
            raise ValueError("Group leakage")
        fold_records.append({"fold": fold_index, "held_out_physical_pair": group,
                             "train_physical_pairs": train_groups, "train_events_n": len(train_rows),
                             "test_events_n": len(test_rows),
                             "test_directions": sorted({r["directed_link"] for r in test_rows})})
        fold_models.append({"held_out_physical_pair": group, "models": models})
        print(json.dumps({"fold": fold_index, "held_out": group, "test_n": len(test_rows)}), flush=True)
    if any(not np.isfinite(values).all() for values in predictions.values()) or any(x is None for x in folds):
        raise ValueError("Incomplete OOF coverage")
    full_models = {method: fit_model(method, d0) for method in FIT_METHODS}
    if not math.isclose(full_models["group_global_offset"]["offset_to_subtract_m"], FROZEN_OFFSET, abs_tol=1e-12):
        raise ValueError("Group-balanced D0 constant does not reproduce frozen candidate")
    frozen_model = {"method": "frozen_global_offset", "offset_to_subtract_m": FROZEN_OFFSET,
                    "source_sha256": FROZEN_SHA256}
    all_export_models = {"frozen_global_offset": frozen_model, **full_models}
    transfer_features = [row["features"] for row in p1]
    transfer = {"raw": np.asarray([r["raw_m"] for r in p1]), "wipro": np.asarray([r["wipro_m"] for r in p1])}
    transfer.update({method: predict_model(model, transfer_features) for method, model in all_export_models.items()})
    summary = {
        "schema_version": 1, "completed_utc": datetime.now(timezone.utc).isoformat(),
        "protocol_sha256": sha256(protocol_file), "protocol": PROTOCOL,
        "D0_lopo": {name: error_metrics(values, d0) for name, values in predictions.items()},
        "P1_retrospective_transfer": {name: error_metrics(values, p1) for name, values in transfer.items()},
        "D0_frozen_global_descriptive_only": error_metrics(predict_model(frozen_model, [r["features"] for r in d0]), d0),
        "frozen_global_D0_note": "This frozen constant uses all D0 truth and is not an unbiased D0 held-pair result. The group_global_offset LOPO arm refits from the other 14 pairs.",
        "feature_coverage": coverage, "folds": fold_records,
        "training_frames_are_correlated": True, "independent_D0_physical_pairs_n": 15,
        "P1_static_positions_n": 1, "P1_previously_examined": True,
        "P1_unseen_hardware_claim": False,
        "P1_hardware_note": "Current A0 47:A0 was an older historical A3; P1 is new to D0 training but hardware is not globally unseen.",
        "source_sha256": sha256(__file__), "input_sha256": input_hashes,
        "no_input_or_frozen_candidate_changed": all(sha256(path) == digest for path, digest in input_hashes.items()),
        "no_serial_or_firmware_mutation": True,
        "prediction_api": "extract_signal_features(wipro_m, raw_m, rssi, payload_b64); predict_model(exported_model, feature_rows)",
        "limitations": ["One fixed anchor deployment/day with 15 physical pairs, not 11827 independent calibration examples.",
                        "Internal pair holdout still shares hardware and environment between folds.",
                        "P1 is one previously examined position with six links; no final generalization or grade claim.",
                        "Reported reference coordinate/height uncertainty is unquantified.",
                        "CSI amplitude summaries can depend on receiver gain, firmware and antenna orientation; they are not physical path-delay estimates.",
                        "No LOS classifier is fitted; reliable controlled labels and independent conditions are not available."]}
    write_csv(out / "D0_lopo_predictions.csv", export_prediction_rows(d0, predictions, folds))
    write_csv(out / "P1_transfer_predictions.csv", export_prediction_rows(p1, transfer))
    write_csv(out / "per_link_metrics.csv", per_link_metrics(d0, predictions, "D0_LOPO") +
              per_link_metrics(p1, transfer, "P1_retrospective_transfer"))
    dump_json(out / "models.json", {"schema_version": 1, "fit_dataset": "D0_only",
                                  "fit_physical_pairs_n": 15, "protocol_sha256": sha256(protocol_file),
                                  "models": all_export_models})
    dump_json(out / "fold_models.json", fold_models)
    dump_json(out / "summary.json", summary)
    write_csv(out / "feature_coverage.csv", coverage)
    checksums = {path.name: sha256(path) for path in sorted(out.iterdir()) if path.is_file()}
    dump_json(out / "checksums.json", checksums)
    print(json.dumps({"out": str(out), "D0_lopo": summary["D0_lopo"],
                      "P1_transfer": summary["P1_retrospective_transfer"]}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
