import serial
import csv
import time
import os
from pathlib import Path
from datetime import datetime

# ============================================================
# RTT + CSI RAW DATA LOGGER
# No real-time plot. No full CSI printing.
# Goal: reduce PC load and save clean raw data for offline analysis.
# ============================================================

PORT = "COM7"
BAUD = 921600
SERIAL_TIMEOUT = 0.05

# Change this before each experiment, e.g. "d1m", "d2m", "point_A"
EXPERIMENT_LABEL = "test"

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = str(PROJECT_ROOT / "data" / "sessions")
FLUSH_INTERVAL_S = 1.0
STATUS_INTERVAL_S = 1.0

# Wide format is recommended:
# one row = one CSI packet, real_values / imag_values are space-separated raw integers.
SAVE_CSI_PACKET_WIDE = True

# Long format creates very large files:
# one row = one CSI subcarrier pair.
SAVE_CSI_LONG = False

# Set to None to run until Ctrl+C.
RUN_SECONDS = None


os.makedirs(OUT_DIR, exist_ok=True)
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
base = f"{timestamp}_{EXPERIMENT_LABEL}"

rtt_path = os.path.join(OUT_DIR, f"{base}_rtt_raw.csv")
csi_packet_path = os.path.join(OUT_DIR, f"{base}_csi_packet_raw.csv")
csi_long_path = os.path.join(OUT_DIR, f"{base}_csi_long_raw.csv")
event_path = os.path.join(OUT_DIR, f"{base}_events.txt")


def parse_rttdbg(parts):
    if len(parts) != 12:
        return None
    try:
        return {
            "cycle_id": int(parts[1]),
            "esp_time_us": int(parts[2]),
            "status": int(parts[3]),
            "rtt_raw_ns": int(parts[4]),
            "rtt_est_ns": int(parts[5]),
            "dist_est_cm": int(parts[6]),
            "dist_raw_cm": int(parts[7]),
            "dist_cal_cm": int(parts[8]),
            "dist_filt_cm": int(parts[9]),
            "avg_rssi": int(parts[10]),
            "report_entries": int(parts[11]),
        }
    except ValueError:
        return None


def parse_csi(parts):
    if len(parts) < 8:
        return None
    try:
        cycle_id = int(parts[1])
        esp_time_us = int(parts[2])
        csi_rssi = int(parts[3])
        csi_len = int(parts[4])
        pair_count = int(parts[5])

        raw_vals = []
        for x in parts[6:]:
            x = x.strip()
            if x:
                raw_vals.append(int(x))

        real = []
        imag = []
        for i in range(0, len(raw_vals) - 1, 2):
            real.append(raw_vals[i])
            imag.append(raw_vals[i + 1])

        n = min(pair_count, len(real), len(imag))

        return {
            "cycle_id": cycle_id,
            "esp_time_us": esp_time_us,
            "csi_rssi": csi_rssi,
            "csi_len": csi_len,
            "pair_count": n,
            "real": real[:n],
            "imag": imag[:n],
            "raw_value_count": len(raw_vals),
        }
    except ValueError:
        return None


def main():
    ser = serial.Serial(PORT, BAUD, timeout=SERIAL_TIMEOUT)
    time.sleep(2)

    print(f"Connected to {PORT} at {BAUD} baud")
    print(f"Saving RTT to: {rtt_path}")
    print(f"Saving CSI packets to: {csi_packet_path}")
    print("Press Ctrl+C to stop.\n")

    rtt_file = open(rtt_path, "w", newline="", encoding="utf-8")
    csi_packet_file = open(csi_packet_path, "w", newline="", encoding="utf-8")
    event_file = open(event_path, "w", encoding="utf-8")
    csi_long_file = None

    try:
        rtt_writer = csv.writer(rtt_file)
        csi_packet_writer = csv.writer(csi_packet_file)

        rtt_writer.writerow([
            "pc_time_s", "pc_time_iso", "experiment_label",
            "cycle_id", "esp_time_us", "status",
            "rtt_raw_ns", "rtt_est_ns",
            "dist_est_cm", "dist_raw_cm", "dist_cal_cm", "dist_filt_cm",
            "avg_rssi", "report_entries"
        ])

        csi_packet_writer.writerow([
            "pc_time_s", "pc_time_iso", "experiment_label",
            "cycle_id", "esp_time_us", "csi_rssi", "csi_len",
            "pair_count", "raw_value_count", "real_values", "imag_values"
        ])

        csi_long_writer = None
        if SAVE_CSI_LONG:
            csi_long_file = open(csi_long_path, "w", newline="", encoding="utf-8")
            csi_long_writer = csv.writer(csi_long_file)
            csi_long_writer.writerow([
                "pc_time_s", "pc_time_iso", "experiment_label",
                "cycle_id", "esp_time_us", "csi_rssi", "csi_len",
                "pair_count", "subcarrier_index", "real", "imag"
            ])

        start_time = time.time()
        last_flush = start_time
        last_status = start_time

        rtt_count = 0
        csi_count = 0
        bad_count = 0
        other_count = 0
        last_rtt_cm = None
        last_csi_pairs = None
        last_csi_rssi = None

        while True:
            now = time.time()
            if RUN_SECONDS is not None and now - start_time >= RUN_SECONDS:
                print("Reached RUN_SECONDS. Stopping.")
                break

            try:
                raw_line = ser.readline().decode("utf-8", errors="ignore").strip()
            except Exception as e:
                bad_count += 1
                event_file.write(f"{time.time():.6f},SERIAL_READ_ERROR,{repr(e)}\n")
                continue

            if not raw_line:
                continue

            pc_time_s = time.time()
            pc_time_iso = datetime.now().isoformat(timespec="milliseconds")
            parts = raw_line.split(",")

            if parts[0] == "RTTDBG":
                data = parse_rttdbg(parts)
                if data is None:
                    bad_count += 1
                    event_file.write(f"{pc_time_s:.6f},BAD_RTT,{raw_line}\n")
                    continue

                rtt_writer.writerow([
                    pc_time_s, pc_time_iso, EXPERIMENT_LABEL,
                    data["cycle_id"], data["esp_time_us"], data["status"],
                    data["rtt_raw_ns"], data["rtt_est_ns"],
                    data["dist_est_cm"], data["dist_raw_cm"],
                    data["dist_cal_cm"], data["dist_filt_cm"],
                    data["avg_rssi"], data["report_entries"]
                ])

                rtt_count += 1
                if data["status"] == 0:
                    last_rtt_cm = data["dist_filt_cm"] or data["dist_cal_cm"] or data["dist_raw_cm"]

            elif parts[0] == "CSI":
                data = parse_csi(parts)
                if data is None:
                    bad_count += 1
                    event_file.write(f"{pc_time_s:.6f},BAD_CSI,{raw_line[:500]}\n")
                    continue

                real_values = " ".join(map(str, data["real"]))
                imag_values = " ".join(map(str, data["imag"]))

                if SAVE_CSI_PACKET_WIDE:
                    csi_packet_writer.writerow([
                        pc_time_s, pc_time_iso, EXPERIMENT_LABEL,
                        data["cycle_id"], data["esp_time_us"],
                        data["csi_rssi"], data["csi_len"],
                        data["pair_count"], data["raw_value_count"],
                        real_values, imag_values
                    ])

                if SAVE_CSI_LONG and csi_long_writer is not None:
                    for sc_idx, (re, im) in enumerate(zip(data["real"], data["imag"])):
                        csi_long_writer.writerow([
                            pc_time_s, pc_time_iso, EXPERIMENT_LABEL,
                            data["cycle_id"], data["esp_time_us"],
                            data["csi_rssi"], data["csi_len"],
                            data["pair_count"], sc_idx, re, im
                        ])

                csi_count += 1
                last_csi_pairs = data["pair_count"]
                last_csi_rssi = data["csi_rssi"]

            else:
                other_count += 1
                if other_count <= 50:
                    event_file.write(f"{pc_time_s:.6f},OTHER,{raw_line[:500]}\n")

            now = time.time()

            if now - last_flush >= FLUSH_INTERVAL_S:
                rtt_file.flush()
                csi_packet_file.flush()
                event_file.flush()
                if csi_long_file is not None:
                    csi_long_file.flush()
                last_flush = now

            if now - last_status >= STATUS_INTERVAL_S:
                elapsed = now - start_time
                print(
                    f"t={elapsed:7.1f}s | "
                    f"RTT={rtt_count:5d} | "
                    f"CSI={csi_count:6d} | "
                    f"bad={bad_count:4d} | "
                    f"other={other_count:4d} | "
                    f"last_RTT={last_rtt_cm} cm | "
                    f"CSI_pairs={last_csi_pairs} | "
                    f"CSI_RSSI={last_csi_rssi}"
                )
                last_status = now

    except KeyboardInterrupt:
        print("\nStopped by user.")

    finally:
        print("Closing files and serial port...")
        for f in [rtt_file, csi_packet_file, event_file, csi_long_file]:
            if f is not None:
                try:
                    f.flush()
                    f.close()
                except Exception:
                    pass
        ser.close()

        print("\nSaved files:")
        print(rtt_path)
        print(csi_packet_path)
        print(event_path)
        if SAVE_CSI_LONG:
            print(csi_long_path)


if __name__ == "__main__":
    main()
