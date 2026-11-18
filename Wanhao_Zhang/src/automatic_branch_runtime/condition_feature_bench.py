"""Fixed-protocol diagnostic-feature ablations; no firmware or capture mutation.

The existing six-anchor measurements are development data. Both directions of a
physical pair are held out together; P1 is an already examined transfer check.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import pathlib
import time
from datetime import datetime, timezone

import numpy as np

import calibration_bench as cal

ROOT = pathlib.Path(__file__).resolve().parents[2]
OLD = ROOT / "outputs/results/optimization_20261017/calibration"
CONFIRMATION = ROOT / "outputs/results/pairwise_20261017_R1_condition_confirmed/summary.json"
DEFAULT_OUT = ROOT / "outputs/results/condition_features_20261019_v2"


def key(row):
    return (row["session_id"], *cal.event_key(row))


def unique_names(*groups):
    return list(dict.fromkeys(name for group in groups for name in group))


def fit_ridge(rows, names):
    """Same residual target, scaling, weights and alpha as the old Ridge arms."""
    for name in names:
        if any(token in name.lower() for token in ("truth", "label", "mac", "coordinate", "condition")):
            raise ValueError("Forbidden predictor " + name)
    weights = cal.pair_balanced_weights(rows)
    feature_rows = [r["features"] for r in rows]
    scaler = cal.fit_scaler(feature_rows, names, weights)
    X = cal.transform(scaler, feature_rows)
    X1 = np.column_stack([np.ones(len(X)), X])
    target = np.array([r["wipro_m"] - r["truth_m"] for r in rows])
    penalty = np.eye(X1.shape[1]) * 0.1
    penalty[0, 0] = 0
    coefficients = np.linalg.solve(X1.T @ (weights[:, None] * X1) + penalty,
                                   X1.T @ (weights * target))
    return {"method": "diagnostic_ridge", "scaler": scaler,
            "coefficients": coefficients.tolist(), "alpha": 0.1}


def predict_ridge(model, features):
    X = cal.transform(model["scaler"], features)
    residual = np.column_stack([np.ones(len(X)), X]) @ np.array(model["coefficients"])
    predicted = np.array([r["wipro_m"] for r in features]) - residual
    if not np.isfinite(predicted).all():
        raise ValueError("Nonfinite prediction")
    return predicted


def attach_payloads(rows, capture):
    """Read the historical CSV without changing its interpretation or events."""
    reports = {}
    conflicts = set()
    for item in cal.read_csv(capture / "ftm.csv"):
        event_key = cal.event_key(item)
        signature = tuple(item[n] for n in ("t1", "t2", "t3", "t4", "rssi", "payload_b64"))
        if event_key in reports and reports[event_key][0] != signature:
            conflicts.add(event_key)
        reports[event_key] = (signature, item)
    missing = 0
    for row in rows:
        event_key = cal.event_key(row)
        item = reports.get(event_key, (None, {}))[1] if event_key not in conflicts else {}
        if item and not math.isclose(cal.raw_range(item), row["raw_m"], abs_tol=1e-8, rel_tol=0):
            raise ValueError("FTM raw range disagrees with fixed event source")
        missing += int(not item)
        row["payload_b64"] = item.get("payload_b64", "")
        row["features"] = cal.extract_signal_features(row["wipro_m"], row["raw_m"],
                                                      item.get("rssi"), row["payload_b64"])
        delta = row["raw_m"] - row["wipro_m"]
        row["features"]["relative_correction"] = delta / (abs(row["raw_m"]) + 1.0)
    return {"capture": str(capture), "events_n": len(rows), "missing_ftm_n": missing}


def load_fixed_data():
    source = ROOT / "outputs/results/pairwise_20261017_R1_summary/static_evaluate/paired_events.csv"
    round_file = ROOT / "outputs/results/pairwise_20261017_R1_summary/round_summary.json"
    p1_cap = ROOT / "outputs/results/A0_static_20261017_160535_P1"
    p1_source = p1_cap / "analysis/paired_events.csv"
    d0 = cal.normalize_rows(cal.read_csv(source))
    p1 = cal.normalize_rows(cal.read_csv(p1_source))
    if len(d0) != 11827 or len(p1) != 1235:
        raise ValueError("Unexpected fixed event population")
    labels = json.loads(CONFIRMATION.read_text(encoding="utf-8-sig"))["confirmed_labels"]
    for row in d0:
        pair = "|".join(sorted([row["own_label"], row["target_label"]]))
        row["condition"] = labels[pair]
    for row in p1:
        row["condition"] = ("NLOS_through_wall" if row["target_label"] in ["A2", "A4", "A5", "A6"]
                            else "unconfirmed_non_through_wall")
    captures = []
    round_data = json.loads(round_file.read_text(encoding="utf-8"))
    for entry in round_data["by_initiator"].values():
        subset = [r for r in d0 if r["session_id"] == entry["session_id"]]
        captures.append((subset, pathlib.Path(entry["capture"])))
    captures.append((p1, p1_cap))
    inputs = [source, round_file, p1_source, CONFIRMATION, p1_cap / "analysis/summary.json"]
    coverage = []
    for rows, capture in captures:
        coverage.append(attach_payloads(rows, capture))
        inputs.extend([capture / "ftm.csv", capture / "serial.raw", capture / "capture_metadata.json"])
    return d0, p1, captures, coverage, inputs


def old_predictions(stage, rows):
    path = OLD / ("D0_lopo_predictions.csv" if stage == "D0_LOPO" else "P1_transfer_predictions.csv")
    saved = cal.read_csv(path)
    indexed = {key(r): r for r in saved}
    if len(indexed) != len(rows) or set(indexed) != {key(r) for r in rows}:
        raise ValueError("Old/new populations differ")
    methods = [name.removesuffix("_prediction_m") for name in saved[0] if name.endswith("_prediction_m")]
    for row in rows:
        reference = indexed[key(row)]
        for name in ["truth_m", "raw_m", "wipro_m"]:
            if float(reference[name]) != row[name]:
                raise ValueError("Old/new source numerical discrepancy")
        if stage == "D0_LOPO" and reference["fold"] != row["physical_pair"]:
            raise ValueError("Baseline fold discrepancy")
    return {method: np.array([float(indexed[key(r)][method + "_prediction_m"]) for r in rows])
            for method in methods}, path


def condition_metrics(rows, predictions, stage):
    records = []
    for condition in ["all", *sorted({r["condition"] for r in rows})]:
        indexes = np.array([i for i, row in enumerate(rows) if condition == "all" or row["condition"] == condition])
        subset = [rows[i] for i in indexes]
        native = cal.error_metrics(predictions["wipro"][indexes], subset)
        for method, values in predictions.items():
            metrics = cal.error_metrics(values[indexes], subset)
            records.append({"stage": stage, "condition": condition, "method": method, **metrics,
                            "event_mae_reduction_vs_native_pct": 100 * (1 - metrics["event_mae_m"] / native["event_mae_m"]),
                            "event_p90_reduction_vs_native_pct": 100 * (1 - metrics["event_p90_abs_error_m"] / native["event_p90_abs_error_m"])})
    return records


def export_features(path, rows, names):
    records = []
    for row in rows:
        item = {k: row[k] for k in ["session_id", "own_label", "target_label", "own_mac", "target_mac", "t2", "condition", "truth_m"]}
        item.update({k: row["features"].get(k, float("nan")) for k in names})
        records.append(item)
    cal.write_csv(path, records)


def run(args):
    import wipro_diagnostic_features as diagnostic
    import burst_diagnostic_features as burst
    started = time.monotonic()
    out = args.out.resolve()
    if out.exists():
        raise FileExistsError("Do not overwrite a previous benchmark " + str(out))
    out.mkdir(parents=True)
    # Lists are provided by independently checked extraction modules.
    normalization = ["relative_correction"]
    spectral = diagnostic.SPECTRAL_FEATURES + ["diagnostic_csi_missing"]
    # Span is exactly low_delta + high_delta. Keep the two independent
    # components so an exact duplicate does not alter Ridge shrinkage.
    cir = [name for name in diagnostic.CIR_FEATURES if name != "cir_threshold_span_m"] + ["diagnostic_cir_missing"]
    base = cal.CSI_FEATURES
    specifications = {
        "ridge_normalized": unique_names(base, normalization),
        "ridge_spectral": unique_names(base, normalization, spectral),
        "ridge_cir": unique_names(base, normalization, cir),
        "ridge_specialized": unique_names(base, normalization, spectral, cir),
        "ridge_specialized_prev_burst": unique_names(base, normalization, spectral, cir, burst.FEATURE_NAMES_PREVIOUS),
        "ridge_specialized_completed_burst": unique_names(base, normalization, spectral, cir, burst.FEATURE_NAMES_COMPLETED),
    }
    protocol = {
        "declared_utc": datetime.now(timezone.utc).isoformat(), "version": "condition_feature_v2_deduplicated",
        "v1_audit_change": "Removed exact redundant threshold_span=low_delta+high_delta; all events, models, alpha and other predictors unchanged; initial v1 retained",
        "development_data": "Oct17 six-anchor fixed deployment; same 11827 paired events as prior benchmark",
        "split": "Leave one physical pair out; both directions grouped; identical held-out pairs as old predictions",
        "model": "Ridge residual regression, alpha0.1, intercept unpenalized; exactly old Ridge objective",
        "fitting_weights": cal.PROTOCOL["fitting_weight"],
        "preprocessing": "training-fold-only scaling and weighted median imputation; missing flags retained",
        "feature_sets": specifications,
        "feature_selection": "fixed ablations declared before computing outcomes; no held-out tuning",
        "diagnostic_feature_spec": diagnostic.FEATURE_SPEC,
        "diagnostic_assets": diagnostic.ASSET_PROVENANCE,
        "burst_feature_spec": burst.PROTOCOL,
        "labels": "confirmed labels for scoring only; excluded from predictor and fit weights",
        "range_validity": "all finite native and predicted values scored, including negative ranges; no clipping",
        "evaluation_weighting": "event-weighted values match conversation; physical-pair/direction-balanced sensitivity also exported",
        "P1": "full D0 train; 1235 previously examined P1 events; A1/A3 only unconfirmed non-through-wall",
        "availability": "same-event CSI arrives at complete FTM export; completed-burst arm delayed until whole FTM packet complete; previous-burst arm uses earlier completed burst only",
        "temporal_scope": "static ranging only; no dynamic localization improvement claimed",
        "primary_comparisons": ["ridge_specialized vs old ridge_signal_csi", "LOS/NLOS each vs native"],
    }
    cal.dump_json(out / "protocol.json", protocol)
    d0, p1, captures, coverage, inputs = load_fixed_data()
    baseline_d0, old_d0_path = old_predictions("D0_LOPO", d0)
    baseline_p1, old_p1_path = old_predictions("P1_transfer", p1)
    inputs.extend([old_d0_path, old_p1_path, OLD / "models.json", cal.FROZEN_FILE,
                   pathlib.Path(__file__), pathlib.Path(cal.__file__), pathlib.Path(diagnostic.__file__), pathlib.Path(burst.__file__)])
    inputs.extend(diagnostic.ASSET_DIR / name for name in diagnostic.ASSET_HASHES)
    original_hashes = {str(path): cal.sha256(path) for path in inputs}
    extraction_audits, native_audits = [], []
    for rows, capture in captures:
        extracted, native_audit = diagnostic.extract_batch_diagnostic_features(
            [r["payload_b64"] for r in rows], [r["raw_m"] for r in rows], [r["wipro_m"] for r in rows])
        native_audits.append({"capture": str(capture), **native_audit})
        for row, features in zip(rows, extracted):
            row["features"].update(features)
        extraction_audits.append(burst.attach_burst_features(rows, capture))
        print(json.dumps({"feature_capture": str(capture), "n": len(rows)}), flush=True)
    all_names = unique_names(*specifications.values())
    for row in d0 + p1:
        absent = set(all_names) - set(row["features"])
        if absent:
            raise ValueError("Feature fields absent: " + str(absent))
    # CIR is trusted only following a separate exact-native check; keep failure fatal.
    cir_validation = {
        "events_n": sum(a["events_n"] for a in native_audits),
        "native_mismatch_n": sum(a["native_reconstruction_mismatch_n"] for a in native_audits),
        "valid_cir_n": sum(a["valid_cir_n"] for a in native_audits),
        "maximum_reconstruction_difference_m": max(a["native_reconstruction_abs_error_max_m"] for a in native_audits),
        "tolerance_m": diagnostic.FEATURE_SPEC["native_reconstruction_tolerance_m"],
        "passed": all(a["native_reconstruction_mismatch_n"] == 0 and a["valid_cir_n"] == a["events_n"] for a in native_audits),
    }
    cal.dump_json(out / "cir_validation.json", cir_validation)
    cal.dump_json(out / "cir_event_audits.json", native_audits)
    if not cir_validation["passed"]:
        raise ValueError("Native CIR reconstruction validation failed")
    predictions = dict(baseline_d0)
    predictions.update({method: np.full(len(d0), np.nan) for method in specifications})
    fold_ids = [None] * len(d0)
    folds, fold_models = [], []
    for fold_index, (pair, train, test) in enumerate(cal.grouped_folds(d0), 1):
        train_rows = [d0[i] for i in train]
        test_features = [d0[i]["features"] for i in test]
        trained = {}
        for method, names in specifications.items():
            model = fit_ridge(train_rows, names)
            trained[method] = model
            predictions[method][test] = predict_ridge(model, test_features)
        for index in test:
            fold_ids[index] = pair
        train_pairs = sorted({r["physical_pair"] for r in train_rows})
        if pair in train_pairs:
            raise ValueError("Held-out pair leaked")
        folds.append({"held_out_physical_pair": pair, "train_physical_pairs": train_pairs,
                      "train_n": len(train), "test_n": len(test)})
        fold_models.append({"held_out_physical_pair": pair, "models": trained})
        print(json.dumps({"fold": fold_index, "held_out": pair}), flush=True)
    if any(not np.isfinite(v).all() for v in predictions.values()):
        raise ValueError("Incomplete prediction population")
    full_models = {method: fit_ridge(d0, names) for method, names in specifications.items()}
    transfer = dict(baseline_p1)
    for method, model in full_models.items():
        transfer[method] = predict_ridge(model, [r["features"] for r in p1])
        replay = predict_ridge(json.loads(json.dumps(model)), [r["features"] for r in p1])
        if not np.array_equal(transfer[method], replay):
            raise ValueError("Exported inference disagrees")
    metrics = condition_metrics(d0, predictions, "D0_LOPO") + condition_metrics(p1, transfer, "P1_transfer")
    cal.write_csv(out / "condition_metrics.csv", metrics)
    cal.write_csv(out / "D0_lopo_predictions.csv", cal.export_prediction_rows(d0, predictions, fold_ids))
    cal.write_csv(out / "P1_transfer_predictions.csv", cal.export_prediction_rows(p1, transfer))
    cal.write_csv(out / "per_link_metrics.csv", cal.per_link_metrics(d0, predictions, "D0_LOPO") + cal.per_link_metrics(p1, transfer, "P1_transfer"))
    export_features(out / "D0_features.csv", d0, all_names)
    export_features(out / "P1_features.csv", p1, all_names)
    cal.dump_json(out / "fold_models.json", fold_models)
    cal.dump_json(out / "models.json", {"fit_dataset": "D0 only", "feature_sets": specifications, "models": full_models})
    cal.dump_json(out / "extraction_audit.json", {"baseline_feature_coverage": coverage, "burst": extraction_audits})
    feature_coverage = [{"stage": stage, "feature": name,
                         "finite_n": sum(math.isfinite(float(r["features"][name])) for r in rows),
                         "events_n": len(rows)} for stage, rows in [("D0", d0), ("P1", p1)] for name in all_names]
    cal.write_csv(out / "feature_coverage.csv", feature_coverage)
    unchanged = all(cal.sha256(path) == digest for path, digest in original_hashes.items())
    if not unchanged:
        raise ValueError("Original inputs changed during analysis")
    summary = {"protocol": protocol, "metrics": metrics, "folds": folds, "cir_validation": cir_validation,
               "source_hashes": original_hashes, "originals_unchanged": unchanged,
               "elapsed_seconds": time.monotonic() - started,
               "limitations": ["Repeatedly inspected development deployment, not independent new-environment validation",
                               "P1 is one previously examined point with six links",
                               "No online timing or dynamic localization performance tested",
                               "CSI/CIR diagnostics are algorithm-derived descriptors, not calibrated physical delay measurements"]}
    cal.dump_json(out / "summary.json", summary)
    (out / "benchmark_source.py").write_bytes(pathlib.Path(__file__).read_bytes())
    cal.dump_json(out / "checksums.json", {p.name: cal.sha256(p) for p in out.iterdir() if p.is_file()})
    print(json.dumps({"output": str(out), "elapsed_seconds": summary["elapsed_seconds"],
                      "primary": [r for r in metrics if r["condition"] in ["LOS", "NLOS_through_wall"]]}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=pathlib.Path, default=DEFAULT_OUT)
    run(parser.parse_args())
