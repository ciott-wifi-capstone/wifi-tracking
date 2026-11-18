"""Read-only, exact-packet burst diagnostics for the 2026-10-19 benchmark.

A burst is a successfully decoded *actual FTM serial packet*, never a cluster of
nearby host timestamps. This separate decoder recovers the first report omitted
by the historical host parser (payload starts at fields[2], not fields[3]).

Current-completed features are available at the end of the FTM+CSI serial frame,
after its earlier RANGE messages. They are appropriate for static calibration
benchmarks scored at that completion time, not an event-arrival latency claim.
Previous-completed features use only the most recent packet for the same directed
radio link that ended strictly before the current RANGE message ended. No labels,
reference distances, positions, class labels, or following bursts enter features.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import math
import pathlib
import re
import struct
from collections import Counter, defaultdict

import numpy as np

_MAC = re.compile(r"[0-9A-F]{2}(?::[0-9A-F]{2}){5}\Z")
_METRICS = (
    "nvalid", "ncsi384", "valid_fraction", "csi384_fraction",
    "wipro_median_m", "wipro_iqr_m", "wipro_median_minus_q10_m",
    "correction_median_m", "correction_iqr_m", "correction_median_minus_q10_m",
    "event_minus_wipro_median_m", "event_minus_correction_median_m", "missing",
)
FEATURE_NAMES_COMPLETED = tuple("burst_" + name for name in _METRICS)
FEATURE_NAMES_PREVIOUS = tuple("prevburst_" + name for name in _METRICS)
PROTOCOL = {
    "version": "raw_packet_burst_features_v1",
    "packet_definition": "one actual strict count-validated FTM serial frame",
    "same_event_key": "full own AP MAC, target AP MAC, exact hardware t2",
    "completed_availability": "end of same FTM+CSI serial packet; includes its emitted finite RANGE outputs, no later packet",
    "previous_availability": "latest same-directed-link FTM packet ending strictly before current RANGE frame end",
    "future_bursts_used": False,
    "labels_used": False,
    "negative_finite_ranges": "retained, consistent with existing ranging benchmark",
    "current_completed_self_inclusion": "allowed because every burst output is available at declared completion; no target/reference is aggregated",
    "receive_delay": "historical ftm.csv exact-key packet receipt minus range.csv exact-key receipt; packet receipt unavailable if historical parser omitted every report",
    "timestamp_limit": "host receive times are not physical RF times; byte order establishes causality even without receive timestamps",
}


def event_key(row):
    return (str(row["own_mac"]).strip().upper(),
            str(row.get("target_mac", row.get("tgt_mac", ""))).strip().upper(),
            int(row.get("t2", row.get("timestamp"))))


def _read_csv(path):
    path = pathlib.Path(path)
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def _diff48(a, b):
    difference = (a - b) & ((1 << 48) - 1)
    return difference - (1 << 48) if difference >= (1 << 47) else difference


def iter_serial_frames(data):
    """Yield (start offset, end-exclusive offset, kind, text fields).

    Bytes between frames are diagnostic console text. A nested STX resynchronizes
    to the later frame, rather than treating diagnostic corruption as a packet.
    """
    cursor = 0
    while True:
        start = data.find(b"\x02", cursor)
        if start < 0:
            return
        end = data.find(b"\x03", start + 1)
        if end < 0:
            return
        nested = data.find(b"\x02", start + 1, end)
        if nested >= 0:
            cursor = nested
            continue
        cursor = end + 1
        try:
            chunks = data[start + 1:end].decode("ascii", errors="strict").split("\x01")
        except UnicodeDecodeError:
            continue
        yield start, end + 1, chunks[0].strip(), [x.strip() for x in chunks[1:]]


def parse_ftm_fields(fields):
    """Strict actual-firmware layout decoder; raises ValueError on corrupt FTM.

    A report is 40 bytes: token at0, RSSI at1, timestamps at8,16,24,32.
    CSI is embedded after the CSI sentinel in five-field groups. CSI pairing uses
    signed-48 modular distance under 2000ps, as the firmware/export convention.
    Ambiguous duplicate CSI associations are flagged missing rather than guessed.
    """
    fields = list(fields)
    while fields and fields[-1] == "":
        fields.pop()
    if len(fields) < 3 or not _MAC.fullmatch(fields[0].upper()):
        raise ValueError("invalid FTM header")
    try:
        expected = int(fields[1])
    except ValueError as error:
        raise ValueError("invalid FTM report count") from error
    if expected < 1 or expected > 64:
        raise ValueError("FTM report count outside firmware capacity")
    try:
        sentinel = fields.index("CSI", 2)
    except ValueError:
        sentinel = len(fields)
    reports = []
    for encoded in fields[2:sentinel]:
        try:
            payload = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as error:
            raise ValueError("invalid FTM base64") from error
        if not payload or len(payload) % 40:
            raise ValueError("FTM report bytes not a positive multiple of40")
        for offset in range(0, len(payload), 40):
            chunk = payload[offset:offset + 40]
            t1, t2, t3, t4 = struct.unpack_from("<QQQQ", chunk, 8)
            reports.append({
                "token": chunk[0], "rssi": struct.unpack_from("<b", chunk, 1)[0],
                "t1": t1, "t2": t2, "t3": t3, "t4": t4,
                "raw_m": (_diff48(t4, t1) - _diff48(t3, t2)) * 299792458e-12 / 2,
                "csi_b64": "", "csi_pair_ambiguous": False,
            })
    if len(reports) != expected:
        raise ValueError("FTM decoded report count differs from declared count")
    if len({report["t2"] for report in reports}) != len(reports):
        raise ValueError("duplicate t2 within actual FTM packet")
    csi_errors = 0
    tail = fields[sentinel + 1:] if sentinel < len(fields) else []
    if len(tail) % 5:
        csi_errors += 1
        tail = []
    associations = defaultdict(list)
    for index in range(0, len(tail), 5):
        ch, ch2, encoded, timestamp, timestamp_mac = tail[index:index + 5]
        try:
            int(ch), int(ch2), int(timestamp_mac)
            timestamp_ps = int(timestamp) * 1000
            csi = base64.b64decode(encoded, validate=True)
            if not csi or len(csi) % 2:
                raise ValueError("invalid complex CSI bytes")
        except (ValueError, TypeError):
            csi_errors += 1
            continue
        matches = [i for i, report in enumerate(reports)
                   if abs(_diff48(timestamp_ps, report["t2"])) < 2000]
        if len(matches) == 1:
            associations[matches[0]].append(encoded)
        else:
            csi_errors += 1
    for index, values in associations.items():
        unique = set(values)
        if len(unique) == 1:
            reports[index]["csi_b64"] = values[0]
        else:
            reports[index]["csi_pair_ambiguous"] = True
            csi_errors += 1
    return fields[0].upper(), reports, csi_errors


def _fallback_own(capture, ftm_rows, range_rows):
    own_values = {row["own_mac"].strip().upper() for row in ftm_rows + range_rows}
    if len(own_values) == 1:
        return next(iter(own_values))
    metadata_path = capture / "capture_metadata.json"
    if metadata_path.exists():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        for field in ("expected_own_ap_mac", "own_ap_mac"):
            value = str(metadata.get(field, "")).upper()
            if _MAC.fullmatch(value):
                return value
    if len(own_values) > 1:
        raise ValueError("multiple own MACs without a unique raw startup identity")
    return ""


def load_raw_capture(capture):
    """Return immutable-input packet/event index and explicit coverage audit."""
    capture = pathlib.Path(capture)
    raw_path = capture / "serial.raw"
    data = raw_path.read_bytes()
    ftm_rows, range_rows = _read_csv(capture / "ftm.csv"), _read_csv(capture / "range.csv")
    ftm_time, range_time = {}, {}
    for rows, destination in ((ftm_rows, ftm_time), (range_rows, range_time)):
        for row in rows:
            key = event_key(row)
            destination.setdefault(key, []).append(float(row["t_ms"]))
    own = _fallback_own(capture, ftm_rows, range_rows)
    events, ranges, bursts, conflicts = {}, {}, [], set()
    counts = Counter()
    for start, end, kind, fields in iter_serial_frames(data):
        if kind == "MAC" and fields and _MAC.fullmatch(fields[0].upper()):
            own = fields[0].upper()
        elif kind == "RANGE":
            counts["raw_range_frames"] += 1
            try:
                target, t2, wipro = fields
                target, t2, wipro = target.upper(), int(t2), float(wipro)
                if not _MAC.fullmatch(target) or not _MAC.fullmatch(own) or not math.isfinite(wipro):
                    raise ValueError("invalid RANGE")
            except (ValueError, TypeError):
                counts["invalid_range_frames"] += 1
                continue
            key = (own, target, t2)
            if key in ranges and ranges[key]["wipro_m"] != wipro:
                conflicts.add(key)
            else:
                ranges[key] = {"wipro_m": wipro, "start_offset": start, "end_offset": end}
        elif kind == "FTM":
            counts["raw_ftm_frames"] += 1
            try:
                target, reports, csi_errors = parse_ftm_fields(fields)
                if not _MAC.fullmatch(own):
                    raise ValueError("missing own MAC")
            except ValueError:
                counts["invalid_ftm_frames"] += 1
                continue
            # Extra CSI callbacks can have no matching FTM report; this counter
            # combines those with malformed/ambiguous groups and is not RF loss.
            counts["csi_unmatched_or_invalid_groups"] += csi_errors
            keys = [(own, target, report["t2"]) for report in reports]
            receive_times = [value for key in keys for value in ftm_time.get(key, [])]
            burst = {"burst_id": len(bursts), "own_mac": own, "target_mac": target,
                     "start_offset": start, "end_offset": end, "keys": keys,
                     "report_count": len(reports), "received_host_ms": max(receive_times) if receive_times else None,
                     "reports": reports}
            bursts.append(burst)
            for key, report in zip(keys, reports):
                enriched = {**report, "own_mac": own, "target_mac": target,
                            "burst_id": burst["burst_id"], "packet_end_offset": end}
                if key in events:
                    # Even identical repeated transmissions are ambiguous burst
                    # memberships and are excluded from exact event attachment.
                    conflicts.add(key)
                else:
                    events[key] = enriched
    for key in conflicts:
        events.pop(key, None)
        ranges.pop(key, None)
    for burst in bursts:
        valid_reports = []
        for key, report in zip(burst["keys"], burst["reports"]):
            if key not in events or key not in ranges:
                continue
            ranging = ranges[key]
            # The firmware emits RANGE before serializing this same FTM packet.
            # Reject unexpected ordering rather than attaching future data.
            if ranging["end_offset"] >= burst["start_offset"]:
                counts["range_not_before_own_ftm_packet"] += 1
                continue
            valid_reports.append((report, ranging["wipro_m"]))
        wipro = np.asarray([value for _, value in valid_reports], dtype=float)
        corrections = np.asarray([report["raw_m"] - value for report, value in valid_reports], dtype=float)
        ncsi = sum(len(base64.b64decode(report["csi_b64"])) == 384 for report, _ in valid_reports)
        stats = {"nvalid": float(len(valid_reports)), "ncsi384": float(ncsi),
                 "valid_fraction": len(valid_reports) / burst["report_count"],
                 "csi384_fraction": ncsi / len(valid_reports) if valid_reports else 0.0}
        for name, values in (("wipro", wipro), ("correction", corrections)):
            stats[name + "_median_m"] = float(np.median(values)) if len(values) else float("nan")
            stats[name + "_iqr_m"] = float(np.quantile(values, .75) - np.quantile(values, .25)) if len(values) else float("nan")
            stats[name + "_median_minus_q10_m"] = float(np.median(values) - np.quantile(values, .1)) if len(values) else float("nan")
        burst["stats"] = stats
    first_report_keys = {burst["keys"][0] for burst in bursts}
    historical_keys = {event_key(row) for row in ftm_rows}
    delays = []
    for key, event in events.items():
        completion_ms = bursts[event["burst_id"]]["received_host_ms"]
        if completion_ms is not None and key in ranges and key in range_time:
            delays.append(completion_ms - min(range_time[key]))
    audit = {
        "capture": str(capture), "serial_raw_sha256": hashlib.sha256(data).hexdigest(),
        **dict(counts), "valid_ftm_packets": len(bursts), "decoded_reports": len(events),
        "historical_ftm_rows": len(ftm_rows), "first_report_keys_missing_from_historical_csv": len(first_report_keys - historical_keys),
        "conflicting_or_duplicate_keys": len(conflicts),
        "packet_receive_timestamp_missing": sum(burst["received_host_ms"] is None for burst in bursts),
        "completion_minus_range_host_receipt_ms": {
            "n": len(delays), "median": float(np.median(delays)) if delays else None,
            "p90": float(np.quantile(delays, .9)) if delays else None,
            "min": float(min(delays)) if delays else None, "max": float(max(delays)) if delays else None,
            "negative_n": sum(value < 0 for value in delays),
        },
    }
    return {"events": events, "ranges": ranges, "bursts": bursts, "audit": audit,
            "range_times": range_time}


def _feature_values(burst, prefix, wipro_m, raw_m):
    if burst is None or not burst["stats"]["nvalid"]:
        return {prefix + name: (1.0 if name == "missing" else float("nan")) for name in _METRICS}
    values = dict(burst["stats"])
    values["event_minus_wipro_median_m"] = float(wipro_m) - values["wipro_median_m"]
    values["event_minus_correction_median_m"] = float(raw_m) - float(wipro_m) - values["correction_median_m"]
    values["missing"] = 0.0
    return {prefix + name: values[name] for name in _METRICS}


def attach_burst_features(rows, capture, raw_index=None):
    """Augment row['features'] in place; return audit, keep every input row.

    Explicit feature-name constants select the previous-completed or the delayed
    current-completed ablation. Missing values require train-fold-only imputation.
    Extra row['burst_provenance'] is diagnostic metadata, never a model input.
    """
    index = raw_index if raw_index is not None else load_raw_capture(capture)
    by_link = defaultdict(list)
    for burst in index["bursts"]:
        by_link[(burst["own_mac"], burst["target_mac"])].append(burst)
    missing_current = missing_previous = missing_range = 0
    delays = []
    for row in rows:
        key = event_key(row)
        event = index["events"].get(key)
        ranging = index["ranges"].get(key)
        current = index["bursts"][event["burst_id"]] if event is not None else None
        if event is not None and not math.isclose(event["raw_m"], float(row["raw_m"]), abs_tol=1e-8, rel_tol=0):
            raise ValueError("exact-key raw RTT differs from paired event")
        previous = None
        if ranging is not None:
            for burst in by_link[key[:2]]:
                if burst["end_offset"] < ranging["end_offset"]:
                    previous = burst
                else:
                    break
        completed = _feature_values(current, "burst_", row["wipro_m"], row["raw_m"])
        prior = _feature_values(previous, "prevburst_", row["wipro_m"], row["raw_m"])
        row.setdefault("features", {}).update(completed)
        row["features"].update(prior)
        delay = None
        if current is not None and current["received_host_ms"] is not None and key in index["range_times"]:
            delay = current["received_host_ms"] - min(index["range_times"][key])
            delays.append(delay)
        row["burst_provenance"] = {
            "current_burst_id": current["burst_id"] if current else None,
            "previous_burst_id": previous["burst_id"] if previous else None,
            "completed_extra_receive_delay_ms": delay,
            "previous_is_before_range_frame": previous is not None and ranging is not None and previous["end_offset"] < ranging["end_offset"],
        }
        missing_current += int(completed["burst_missing"])
        missing_previous += int(prior["prevburst_missing"])
        missing_range += int(ranging is None)
    return {**index["audit"], "attached_events": len(rows), "completed_features_missing": missing_current,
            "previous_features_missing": missing_previous, "raw_range_key_missing": missing_range,
            "attached_completion_delay_ms": {"n": len(delays),
                "median": float(np.median(delays)) if delays else None,
                "p90": float(np.quantile(delays, .9)) if delays else None}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=pathlib.Path)
    args = parser.parse_args()
    print(json.dumps(load_raw_capture(args.capture)["audit"], indent=2, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
