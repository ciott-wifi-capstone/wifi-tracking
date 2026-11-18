#!/usr/bin/env python3
"""One-known-point quick bias calibration for the five FTM anchors."""
from __future__ import annotations

import argparse
import json
import math
import shutil
import statistics
import time
from pathlib import Path

from fused_3d_mapper import DEFAULT_CONFIG, parse_mfused


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--port", help="Override serial port")
    parser.add_argument("--position", nargs=3, type=float, metavar=("X", "Y", "Z"), required=True)
    parser.add_argument("--seconds", type=float, default=25.0)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    port = args.port or config["serial_port"]
    try:
        import serial
    except ImportError as exc:
        raise SystemExit("Missing pyserial; run: pip install -r requirements.txt") from exc

    samples: dict[str, list[float]] = {aid: [] for aid in config["anchors"]}
    print(f"Keep Tag still at {tuple(args.position)} m for {args.seconds:.0f}s")
    deadline = time.monotonic() + args.seconds
    with serial.Serial(port, int(config.get("baudrate", 115200)), timeout=1) as device:
        while time.monotonic() < deadline:
            parsed = parse_mfused(device.readline().decode("utf-8", errors="ignore"))
            if not parsed or parsed["status"] != 0 or parsed["anchor"] not in samples:
                continue
            if parsed["filtered_cm"] > 0:
                samples[parsed["anchor"]].append(parsed["filtered_cm"] / 100.0)

    tag = args.position
    for aid, anchor in config["anchors"].items():
        values = samples[aid]
        if len(values) < 5:
            print(f"A{aid}: only {len(values)} samples; bias unchanged")
            continue
        true_distance = math.dist(tag, [anchor["x_m"], anchor["y_m"], anchor["z_m"]])
        measured = statistics.median(values)
        old_bias = float(anchor.get("bias_m", 0.0))
        # Config formula is scale * firmware_range + bias.
        anchor["bias_m"] = round(old_bias + true_distance - measured, 6)
        print(f"A{aid}: n={len(values)} true={true_distance:.3f}m median={measured:.3f}m "
              f"bias={anchor['bias_m']:+.3f}m")

    backup = args.config.with_suffix(args.config.suffix + f".backup-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(args.config, backup)
    args.config.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Updated {args.config}; backup: {backup}")


if __name__ == "__main__":
    main()
