"""Run frozen Wi-PRO correction or causal position filtering without hardware.

The CSI example checks numerical reconstruction against recorded native ranges.
The positions example demonstrates the fixed Kalman interface. Neither example
contains ground truth for assessing ranging or positioning accuracy.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
import platform

from cv_kalman import CausalCVKalman
from live_automatic import StreamingAutomatic


def finite_number(value, label):
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be a finite number") from error
    if not math.isfinite(number):
        raise ValueError(f"{label} must be a finite number")
    return number


def reconstruct_events(path):
    document = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(document, dict) or not isinstance(document.get("events"), list):
        raise ValueError("CSI input must be an object containing an events list")
    events = document["events"]
    if not events:
        raise ValueError("CSI events list is empty")
    checked = []
    required = {"payload_b64", "raw_m", "native_wipro_m"}
    for index, event in enumerate(events, 1):
        label = f"CSI event {index}"
        if not isinstance(event, dict) or not required <= event.keys():
            raise ValueError(f"{label} requires payload_b64, raw_m and native_wipro_m")
        if set(event) - required - {"rssi_dbm"}:
            raise ValueError(f"{label} has unknown fields")
        if not isinstance(event["payload_b64"], str) or not event["payload_b64"]:
            raise ValueError(f"{label} requires a nonempty base64 CSI payload")
        rssi = event.get("rssi_dbm")
        checked.append({
            "payload_b64": event["payload_b64"],
            "raw_m": finite_number(event["raw_m"], f"{label} raw_m"),
            "native_wipro_m": finite_number(event["native_wipro_m"], f"{label} native_wipro_m"),
            "rssi_dbm": None if rssi is None else finite_number(rssi, f"{label} rssi_dbm"),
        })

    # This loads the original model and verifies every pinned helper and asset.
    engine = StreamingAutomatic()
    helpers = engine.helpers
    diagnostic = helpers["wipro_diagnostic_features"]
    payloads = [event["payload_b64"] for event in checked]
    raw = [event["raw_m"] for event in checked]
    native = [event["native_wipro_m"] for event in checked]
    diagnostics, audit = diagnostic.extract_batch_diagnostic_features(payloads, raw, native)
    if audit["valid_csi_n"] != len(checked):
        raise ValueError("Every CSI event must decode as a valid 384-byte HT40 payload")
    if audit["native_reconstruction_mismatch_n"]:
        failed = [index + 1 for index in audit["native_reconstruction_mismatch_event_indices"]]
        raise ValueError(f"Native reconstruction exceeds 0.0001 m tolerance at events {failed}")

    # Match the live event-local feature extraction and frozen inference exactly.
    features = []
    for event, extra in zip(checked, diagnostics):
        feature = helpers["calibration_bench"].extract_signal_features(
            event["native_wipro_m"], event["raw_m"], event["rssi_dbm"], event["payload_b64"])
        feature.update(extra)
        feature["relative_correction"] = (
            (event["raw_m"] - event["native_wipro_m"]) / (abs(event["raw_m"]) + 1.0))
        features.append(feature)
    prediction = helpers["automatic_branch_bench"].predict_automatic_bundle(engine.bundle, features)
    reconstructed = {row["event_index"]: row["native_reconstruction_m"]
                     for row in audit["native_reconstruction"]}
    rows = []
    for index, event in enumerate(checked):
        score = float(prediction["compact_probability"][index])
        rows.append({
            "event_index": index + 1,
            "raw_m": event["raw_m"],
            "native_wipro_m": event["native_wipro_m"],
            "reconstructed_native_m": reconstructed[index],
            "reconstruction_abs_difference_m": abs(reconstructed[index] - event["native_wipro_m"]),
            "rssi_dbm": event["rssi_dbm"],
            "rssi_missing": bool(features[index]["rssi_missing"]),
            "offset_prediction_m": float(prediction["los_offset_every_link"][index]),
            "expert_prediction_m": float(prediction["specialized"][index]),
            "automatic_prediction_m": float(prediction["automatic_compact_hard"][index]),
            "gate_score": score,
            "branch": "specialized" if score >= 0.5 else "los_offset",
        })
    metadata = engine.metadata()
    summary = {
        "mode": "frozen_csi_inference", "input": str(path), "events_n": len(rows),
        "input_provenance": document.get("provenance"),
        "native_reconstruction_tolerance_m": audit["native_reconstruction_tolerance_m"],
        "maximum_native_reconstruction_difference_m": audit["native_reconstruction_abs_error_max_m"],
        "native_reconstruction_passed": True,
        "rssi_missing_n": sum(row["rssi_missing"] for row in rows),
        "missing_rssi_policy": "Frozen training imputation and missing flags, identical to live inference",
        "model_sha256": metadata["model_sha256"], "helper_sha256": metadata["helper_sha256"],
        "los_offset_m": metadata["los_offset_m"], "fitting": False, "clipping": False,
        "accuracy_evaluated": False,
        "scope": "Recorded native-output reconstruction and frozen prediction demonstration, no ground-truth accuracy scores",
        "python": platform.python_version(),
    }
    return rows, summary


def read_positions(path):
    observations = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames
        if fields is None or len(fields) != len(set(fields)):
            raise ValueError("Position CSV requires unique t, x and y column headers")
        required = {"t", "x", "y"}
        if not required <= set(fields) or set(fields) - required - {"new_information"}:
            raise ValueError("Position CSV columns must be t,x,y and optional new_information")
        for row in reader:
            label = f"Position CSV line {reader.line_num}"
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"{label} has a different number of fields from its header")
            flag = row.get("new_information", "true").strip().lower()
            if flag not in {"true", "false", "1", "0"}:
                raise ValueError(f"{label} new_information must be true, false, 1 or 0")
            new_information = flag in {"true", "1"}
            x, y = row["x"].strip(), row["y"].strip()
            xy = None
            if x or y:
                xy = [finite_number(x, f"{label} x"), finite_number(y, f"{label} y")]
            if new_information and xy is None:
                raise ValueError(f"{label} requires x and y for a new observation")
            observations.append((reader.line_num, finite_number(row["t"], f"{label} t"), xy, new_information))
    if not observations:
        raise ValueError("Position CSV contains no observations")
    return observations


def replay_positions(path):
    model = CausalCVKalman()
    records, rows = [], []
    for line, timestamp, xy, new_information in read_positions(path):
        try:
            record = model.step(timestamp, xy=xy, new_information=new_information)
        except (TypeError, ValueError) as error:
            raise ValueError(f"Position CSV line {line}: {error}") from error
        records.append(record)
        output = record["xy"]
        state = record["state"]
        rows.append({
            "row_index": len(rows) + 1, "t": timestamp,
            "input_x": None if xy is None else xy[0],
            "input_y": None if xy is None else xy[1],
            "new_information": new_information, "published": record["published"],
            "x": None if output is None else output[0],
            "y": None if output is None else output[1],
            "vx": None if state is None else state[2],
            "vy": None if state is None else state[3],
            "measurement_used": record["measurement_used"],
            "update_reason": record["update_reason"],
            "observation_age_s": record["observation_age_s"], "reset": record["reset"],
        })
    summary = {
        "mode": "causal_position_replay", "input": str(path), "rows_n": len(rows),
        "filter_specification": model.specification(),
        "measurement_rows_n": sum(row["measurement_used"] for row in rows),
        "prediction_only_rows_n": sum(not row["measurement_used"] for row in rows),
        "published_rows_n": sum(row["published"] for row in rows),
        "reset_rows_n": sum(row["reset"] for row in rows),
        "new_information_policy": "Use the supplied flag, default true when the column is absent. The caller identifies genuinely new information.",
        "timestamps_resampled": False, "fitting": False, "clipping": False,
        "accuracy_evaluated": False,
        "scope": "Fixed-parameter causal filtering demonstration, no ground-truth accuracy scores",
        "python": platform.python_version(),
    }
    return rows, records, summary


def write_csv(path, rows):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def write_json(path, document):
    path.write_text(json.dumps(document, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--input", type=Path, help="JSON with recorded native ranges and HT40 CSI events")
    mode.add_argument("--positions", type=Path, help="CSV with t,x,y and optional new_information")
    parser.add_argument("--out", type=Path, required=True, help="Fresh output directory")
    args = parser.parse_args()
    try:
        if args.out.exists():
            raise ValueError(f"Output directory already exists: {args.out}. Choose a fresh directory.")
        if args.input is not None:
            rows, summary = reconstruct_events(args.input)
            records = None
        else:
            rows, records, summary = replay_positions(args.positions)
        # Validate and calculate every input before creating any result files.
        args.out.mkdir(parents=True, exist_ok=False)
        if records is None:
            write_csv(args.out / "event_predictions.csv", rows)
        else:
            write_csv(args.out / "kalman_replay.csv", rows)
            write_json(args.out / "kalman_replay.json", records)
        write_json(args.out / "summary.json", summary)
    except (OSError, TypeError, ValueError) as error:
        parser.error(str(error))
    print(f"Wrote {len(rows)} rows to {args.out}")


if __name__ == "__main__":
    main()
