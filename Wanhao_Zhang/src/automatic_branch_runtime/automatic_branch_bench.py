"""Automatic offset/specialized correction with physical-pair holdout.

Known labels select LOS training examples only. Automatic inference consumes
radio features alone. Reuses exact saved specialized-expert OOF predictions.
"""
from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict
import time

import numpy as np

import calibration_bench as cal
import condition_feature_bench as feature_bench
import automatic_branch_core as gate

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "outputs/results/condition_features_20261019_v2"
OUT = ROOT / "outputs/results/automatic_branch_20261019_v1"
BIAS_THRESHOLD_M = 2.0
GATE_SETS = {"full": gate.FULL_GATE_FEATURES, "compact": gate.PHYSICAL_GATE_FEATURES}


def key(row):
    return (row["session_id"], *cal.event_key(row))


def high_bias_targets(rows):
    """Training-only pair regime labels; positive bias, NOT absolute error."""
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["physical_pair"]].append(row["wipro_m"] - row["truth_m"])
    medians = {pair: float(np.median(errors)) for pair, errors in grouped.items()}
    targets = np.array([float(medians[r["physical_pair"]] > BIAS_THRESHOLD_M) for r in rows])
    return targets, medians


def load_stage(stage):
    prediction_file = SOURCE / ("D0_lopo_predictions.csv" if stage == "D0_LOPO" else "P1_transfer_predictions.csv")
    feature_file = SOURCE / ("D0_features.csv" if stage == "D0_LOPO" else "P1_features.csv")
    rows = cal.normalize_rows(cal.read_csv(prediction_file))
    features = cal.read_csv(feature_file)
    index = {key(r): r for r in features}
    if len(index) != len(rows) or set(index) != {key(r) for r in rows}:
        raise ValueError("Feature and prediction event populations differ")
    methods = [name.removesuffix("_prediction_m") for name in rows[0] if name.endswith("_prediction_m")]
    predictions = {method: np.array([float(r[method + "_prediction_m"]) for r in rows]) for method in methods}
    for row in rows:
        feature_row = index[key(row)]
        if row["truth_m"] != float(feature_row["truth_m"]) or row["wipro_m"] != float(feature_row["wipro_m"]):
            raise ValueError("Feature/reference values disagree")
        row["condition"] = feature_row["condition"]
        row["features"] = {name: float(feature_row[name]) for name in gate.FULL_GATE_FEATURES}
        if stage == "D0_LOPO" and row["fold"] != row["physical_pair"]:
            raise ValueError("Saved expert fold does not match held-out pair")
    return rows, predictions, [prediction_file, feature_file]


def predict_automatic_bundle(bundle, feature_rows):
    """Saved-model API. No labels, truth, IDs, coordinates or fitting inputs."""
    low = np.array([r["wipro_m"] for r in feature_rows]) - bundle["los_offset_m"]
    high = feature_bench.predict_ridge(bundle["specialized_expert"], feature_rows)
    output = {"los_offset_every_link": low, "specialized": high}
    for name, model in bundle["gates"].items():
        probability = gate.predict_probability(model, feature_rows)
        routed = gate.branch_predictions(low, high, probability)
        output["automatic_" + name + "_hard"] = routed["hard_prediction_m"]
        output["automatic_" + name + "_soft"] = routed["soft_prediction_m"]
        output[name + "_probability"] = probability
    return output


def gate_metrics(rows, probabilities, stage):
    target, _ = high_bias_targets(rows)  # Evaluation only, AFTER predictions.
    records = []
    for condition in ["all", *sorted({r["condition"] for r in rows})]:
        indexes = np.array([i for i,r in enumerate(rows) if condition == "all" or r["condition"] == condition])
        subset = [rows[i] for i in indexes]
        balanced = cal.pair_balanced_weights(subset)
        truth = target[indexes].astype(bool)
        for name, values in probabilities.items():
            p = values[indexes]
            selected = p >= .5
            records.append({"stage": stage, "condition": condition, "gate": name, "events_n": len(indexes),
                            "physical_pairs_n": len({r["physical_pair"] for r in subset}),
                            "specialized_selected_pct": float(selected.mean() * 100),
                            "mean_gate_score": float(p.mean()),
                            "true_high_bias_pct": float(truth.mean() * 100),
                            "event_accuracy": float((selected == truth).mean()),
                            "pair_direction_balanced_accuracy": float(balanced @ (selected == truth)),
                            "true_positive_n": int(np.sum(selected & truth)),
                            "false_positive_n": int(np.sum(selected & ~truth)),
                            "true_negative_n": int(np.sum(~selected & ~truth)),
                            "false_negative_n": int(np.sum(~selected & truth))})
    return records


def export_gate_outputs(rows, probability, offset, high, folds):
    records = []
    for i,row in enumerate(rows):
        item = {k: row[k] for k in ["session_id", "own_label", "target_label", "own_mac", "target_mac", "t2", "condition", "truth_m", "wipro_m"]}
        item.update(fold=folds[i], offset_prediction_m=float(offset[i]), specialized_prediction_m=float(high[i]))
        for name, values in probability.items():
            item[name + "_probability_large_positive_bias"] = float(values[i])
            item[name + "_selected_specialized"] = int(values[i] >= .5)
        records.append(item)
    return records


def pair_metrics(rows, predictions, stage):
    grouped = defaultdict(list)
    for i,row in enumerate(rows):grouped[row["physical_pair"]].append(i)
    records = []
    for pair,indexes in grouped.items():
        subset = [rows[i] for i in indexes]
        name = "|".join(sorted([subset[0]["own_label"], subset[0]["target_label"]]))
        for method, values in predictions.items():
            records.append({"stage": stage, "pair": name, "condition": subset[0]["condition"], "method": method,
                            **cal.error_metrics(values[indexes], subset)})
    return records


def main():
    started = time.monotonic()
    if OUT.exists():raise FileExistsError(OUT)
    OUT.mkdir(parents=True)
    protocol = {
        "declared_utc": datetime.now(timezone.utc).isoformat(), "version": "automatic_branch_v1",
        "data": "Exact v2 static-ranging populations; D0 11827, P1 1235; no dropped/trimmed/clipped outputs",
        "split": "LOPO, both directions of each physical pair held out together",
        "low_branch": "Median offset from confirmed LOS examples in outer training fold only; 4 or 5 physical pairs",
        "high_branch": "Identical v2 dedicated Ridge: reuse its saved outer-fold predictions; full D0 model for P1",
        "gate_target": "Training-only physical-pair median signed native error > 2.0m, positive high bias; not LOS/NLOS classification",
        "gate_features": GATE_SETS,
        "gate_fit": "Weighted logistic regression; alpha0.1, train-fold-only scaler/imputation; no class rebalance",
        "training_weight": cal.PROTOCOL["fitting_weight"],
        "routing": {"hard": "p>=0.5 selects specialized, else offset", "soft": "(1-p)*offset+p*specialized"},
        "probability": "Uncalibrated gate score for the training-defined high-positive-bias regime",
        "selection": "Two fixed feature sets, hard and soft routing; no threshold/feature/P1 tuning",
        "oracle_high_bias_reference": "Post-hoc true pair-median routing, explicitly not automatic and excluded from deployment claims",
        "los_offset_every_link": "Control applies the LOS-only offset to every link, without automatic routing",
        "P1": "Previously inspected retrospective single position; A1/A3 not separately confirmed direct LOS",
        "scope": "Offline static ranging; automatic inference API exported but not integrated into live GUI or flashed",
    }
    cal.dump_json(OUT / "protocol.json", protocol)
    d0, d0pred, d0inputs = load_stage("D0_LOPO")
    p1, p1pred, p1inputs = load_stage("P1_transfer")
    if len(d0) != 11827 or len(p1) != 1235:raise ValueError("Unexpected event count")
    model_file = SOURCE / "models.json"
    high_full = json.loads(model_file.read_text(encoding="utf-8"))["models"]["ridge_specialized"]
    if high_full["scaler"]["feature_names"] != gate.FULL_GATE_FEATURES:
        raise ValueError("Gate full feature set disagrees with specialized expert")
    inputs = d0inputs + p1inputs + [model_file, SOURCE / "summary.json", Path(__file__), Path(cal.__file__), Path(gate.__file__), Path(feature_bench.__file__)]
    hashes = {str(p): cal.sha256(p) for p in inputs}
    new_methods = ["los_offset_every_link", "automatic_full_hard", "automatic_full_soft",
                   "automatic_compact_hard", "automatic_compact_soft", "oracle_high_bias_reference"]
    d0pred.update({m: np.full(len(d0),np.nan) for m in new_methods})
    probabilities = {name: np.full(len(d0),np.nan) for name in GATE_SETS}
    models, fold_ids = [], [None] * len(d0)
    for fold,(pair,train,test) in enumerate(cal.grouped_folds(d0),1):
        train_rows, test_rows = [d0[i] for i in train], [d0[i] for i in test]
        los_rows = [r for r in train_rows if r["condition"] == "LOS"]
        low_model = cal.fit_model("group_global_offset",los_rows)
        low = np.array([r["wipro_m"] for r in test_rows]) - low_model["offset_to_subtract_m"]
        high = d0pred["ridge_specialized"][test]
        d0pred["los_offset_every_link"][test] = low
        target, medians = high_bias_targets(train_rows)
        weights = cal.pair_balanced_weights(train_rows)
        fitted = {}
        for name,names in GATE_SETS.items():
            fitted[name] = gate.fit_gate([r["features"] for r in train_rows],target,weights,feature_names=names)
            p = gate.predict_probability(fitted[name],[r["features"] for r in test_rows])
            probabilities[name][test] = p
            routed = gate.branch_predictions(low,high,p)
            for mode in ["hard","soft"]:
                d0pred["automatic_" + name + "_" + mode][test] = routed[mode + "_prediction_m"]
        # Only this explicitly marked diagnostic reads held-out reference values.
        oracle, _ = high_bias_targets(test_rows)
        d0pred["oracle_high_bias_reference"][test] = np.where(oracle >= .5,high,low)
        for i in test:fold_ids[i] = pair
        models.append({"held_out_pair":pair,"train_pairs":sorted({r["physical_pair"] for r in train_rows}),
                       "training_pair_bias_medians_m":medians,"los_training_pairs":sorted({r["physical_pair"] for r in los_rows}),
                       "los_offset":low_model,"gates":fitted})
        print(json.dumps({"fold":fold,"held_out":pair,"los_train_pairs":len({r["physical_pair"] for r in los_rows})}),flush=True)
    target,full_medians = high_bias_targets(d0)
    full_gates = {name:gate.fit_gate([r["features"] for r in d0],target,cal.pair_balanced_weights(d0),feature_names=names)
                  for name,names in GATE_SETS.items()}
    full_low = cal.group_offset([r for r in d0 if r["condition"] == "LOS"])
    bundle = {"version":"automatic_branch_v1","los_offset_m":full_low,"specialized_expert":high_full,
              "gates":full_gates,"fit_dataset":"D0 only","inference_input":"Same-event radio features only"}
    transfer = predict_automatic_bundle(bundle,[r["features"] for r in p1])
    np.testing.assert_allclose(transfer["specialized"],p1pred["ridge_specialized"],rtol=0,atol=1e-12)
    for method in new_methods:
        if method == "oracle_high_bias_reference":continue
        p1pred[method] = transfer[method]
    p1oracle,_ = high_bias_targets(p1)
    p1pred["oracle_high_bias_reference"] = np.where(p1oracle>=.5,p1pred["ridge_specialized"],p1pred["los_offset_every_link"])
    for prediction in list(d0pred.values()) + list(p1pred.values()):
        if not np.isfinite(prediction).all():raise ValueError("Nonfinite predictions")
    roundtrip = predict_automatic_bundle(json.loads(json.dumps(bundle)),[r["features"] for r in p1])
    for name,value in transfer.items():np.testing.assert_array_equal(value,roundtrip[name])
    metrics = feature_bench.condition_metrics(d0,d0pred,"D0_LOPO") + feature_bench.condition_metrics(p1,p1pred,"P1_transfer")
    p1prob = {name:transfer[name + "_probability"] for name in GATE_SETS}
    gating = gate_metrics(d0,probabilities,"D0_LOPO") + gate_metrics(p1,p1prob,"P1_transfer")
    cal.write_csv(OUT / "D0_lopo_predictions.csv",cal.export_prediction_rows(d0,d0pred,fold_ids))
    cal.write_csv(OUT / "P1_transfer_predictions.csv",cal.export_prediction_rows(p1,p1pred))
    cal.write_csv(OUT / "condition_metrics.csv",metrics)
    cal.write_csv(OUT / "gate_metrics.csv",gating)
    cal.write_csv(OUT / "per_physical_pair_metrics.csv",pair_metrics(d0,d0pred,"D0_LOPO") + pair_metrics(p1,p1pred,"P1_transfer"))
    cal.write_csv(OUT / "D0_gate_outputs.csv",export_gate_outputs(d0,probabilities,d0pred["los_offset_every_link"],d0pred["ridge_specialized"],fold_ids))
    cal.write_csv(OUT / "P1_gate_outputs.csv",export_gate_outputs(p1,p1prob,p1pred["los_offset_every_link"],p1pred["ridge_specialized"],["D0_full_fit_to_P1"]*len(p1)))
    cal.dump_json(OUT / "fold_models.json",models)
    cal.dump_json(OUT / "automatic_model_bundle.json",bundle)
    cal.dump_json(OUT / "summary.json",{"protocol":protocol,"metrics":metrics,"gate_metrics":gating,"source_hashes":hashes,
                                        "full_training_pair_bias_medians_m":full_medians,"full_los_offset_m":full_low,
                                        "all_inputs_unchanged":all(cal.sha256(p)==h for p,h in hashes.items()),
                                        "elapsed_seconds":time.monotonic()-started})
    (OUT / "benchmark_source.py").write_bytes(Path(__file__).read_bytes())
    cal.dump_json(OUT / "checksums.json",{p.name:cal.sha256(p) for p in OUT.iterdir() if p.is_file()})
    print(json.dumps({"output":str(OUT),"elapsed_seconds":time.monotonic()-started}),flush=True)


if __name__ == "__main__":main()
