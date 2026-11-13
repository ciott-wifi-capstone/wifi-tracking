import serial
import csv
import time
import math
from collections import deque
import matplotlib.pyplot as plt
import matplotlib.animation as animation
from pathlib import Path

# =========================
# Basic settings
# =========================
PORT = "COM7"
BAUD = 921600  # If you set ESP-IDF console to 921600, change this to 921600 too.

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LIVE_LOG_DIR = PROJECT_ROOT / "data" / "live_logs"
LIVE_LOG_DIR.mkdir(parents=True, exist_ok=True)
RTT_CSV = LIVE_LOG_DIR / "rtt_log.csv"
CSI_CSV = LIVE_LOG_DIR / "csi_full_packet_log.csv"

MAX_POINTS = 200
PLOT_SC_A = 10      # SC0 is often null/zero, so do not plot SC0
PLOT_SC_B = 80
PRINT_INTERVAL_S = 1.0
FLUSH_INTERVAL_S = 1.0

# =========================
# Buffers
# =========================
rtt_time = deque(maxlen=MAX_POINTS)
rtt_dist = deque(maxlen=MAX_POINTS)
csi_time = deque(maxlen=MAX_POINTS)
csi_amp_a = deque(maxlen=MAX_POINTS)
csi_amp_b = deque(maxlen=MAX_POINTS)
csi_rssi = deque(maxlen=MAX_POINTS)

ser = serial.Serial(PORT, BAUD, timeout=0.05)
time.sleep(2)
print(f"Connected to {PORT} at {BAUD} baud")

rtt_file = open(RTT_CSV, "w", newline="", encoding="utf-8")
csi_file = open(CSI_CSV, "w", newline="", encoding="utf-8")
rtt_writer = csv.writer(rtt_file)
csi_writer = csv.writer(csi_file)

rtt_writer.writerow([
    "pc_time_s", "cycle_id", "esp_time_us", "status",
    "rtt_raw_ns", "rtt_est_ns", "dist_est_cm", "dist_raw_cm",
    "dist_cal_cm", "dist_filt_cm", "avg_rssi", "report_entries"
])

# One CSV row = one complete CSI packet. This is MUCH faster than 128 rows per packet.
csi_writer.writerow([
    "pc_time_s", "cycle_id", "esp_time_us", "csi_rssi", "csi_len", "pair_count",
    "real_values", "imag_values", "amp_values", "phase_values"
])

fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(10, 8))
fig.suptitle("RTT + Full CSI Real-time Monitor")
line_rtt, = ax1.plot([], [], label="RTT distance (cm)")
line_csi_a, = ax2.plot([], [], label=f"CSI SC{PLOT_SC_A} amp")
line_csi_b, = ax2.plot([], [], label=f"CSI SC{PLOT_SC_B} amp")
line_rssi, = ax3.plot([], [], label="CSI RSSI (dBm)")

ax1.set_ylabel("Distance (cm)")
ax2.set_ylabel("CSI amplitude")
ax3.set_ylabel("RSSI (dBm)")
ax3.set_xlabel("Time (s)")
for ax in (ax1, ax2, ax3):
    ax.grid(True)
    ax.legend(loc="upper right")

start_time = time.time()
last_print = start_time
last_flush = start_time
rtt_count = 0
csi_count = 0
bad_count = 0

def parse_rttdbg(parts):
    if len(parts) != 12:
        return None
    try:
        return {
            "cycle_id": int(parts[1]), "esp_time_us": int(parts[2]), "status": int(parts[3]),
            "rtt_raw_ns": int(parts[4]), "rtt_est_ns": int(parts[5]),
            "dist_est_cm": int(parts[6]), "dist_raw_cm": int(parts[7]),
            "dist_cal_cm": int(parts[8]), "dist_filt_cm": int(parts[9]),
            "avg_rssi": int(parts[10]), "report_entries": int(parts[11]),
        }
    except ValueError:
        return None

def parse_csi(parts):
    if len(parts) < 8:
        return None
    try:
        cycle_id = int(parts[1])
        esp_time_us = int(parts[2])
        rssi = int(parts[3])
        csi_len = int(parts[4])
        pair_count = int(parts[5])
        raw = [int(x) for x in parts[6:] if x != ""]
        real, imag = [], []
        for i in range(0, len(raw) - 1, 2):
            real.append(raw[i])
            imag.append(raw[i + 1])
        n = min(pair_count, len(real), len(imag))
        return cycle_id, esp_time_us, rssi, csi_len, n, real[:n], imag[:n]
    except ValueError:
        return None

def update(_frame):
    global last_print, last_flush, rtt_count, csi_count, bad_count

    # Limit work per GUI frame so plotting does not freeze if serial backlog is large.
    lines_processed = 0
    while ser.in_waiting and lines_processed < 300:
        lines_processed += 1
        raw_line = ser.readline().decode("utf-8", errors="ignore").strip()
        if not raw_line:
            continue

        now = time.time()
        t = now - start_time
        parts = raw_line.split(",")

        if parts[0] == "RTTDBG":
            data = parse_rttdbg(parts)
            if data is None:
                bad_count += 1
                continue
            rtt_writer.writerow([
                now, data["cycle_id"], data["esp_time_us"], data["status"],
                data["rtt_raw_ns"], data["rtt_est_ns"], data["dist_est_cm"],
                data["dist_raw_cm"], data["dist_cal_cm"], data["dist_filt_cm"],
                data["avg_rssi"], data["report_entries"]
            ])
            rtt_count += 1
            if data["status"] == 0:
                dist = data["dist_filt_cm"] or data["dist_cal_cm"] or data["dist_raw_cm"]
                rtt_time.append(t)
                rtt_dist.append(dist)

        elif parts[0] == "CSI":
            parsed = parse_csi(parts)
            if parsed is None:
                bad_count += 1
                continue
            cycle_id, esp_time_us, rssi, csi_len, n, real, imag = parsed
            amp = [math.hypot(re, im) for re, im in zip(real, imag)]
            phase = [math.atan2(im, re) for re, im in zip(real, imag)]

            # Full unpacked CSI packet saved here.
            csi_writer.writerow([
                now, cycle_id, esp_time_us, rssi, csi_len, n,
                " ".join(map(str, real)),
                " ".join(map(str, imag)),
                " ".join(f"{x:.3f}" for x in amp),
                " ".join(f"{x:.6f}" for x in phase),
            ])
            csi_count += 1

            if amp:
                csi_time.append(t)
                csi_amp_a.append(amp[PLOT_SC_A] if len(amp) > PLOT_SC_A else 0)
                csi_amp_b.append(amp[PLOT_SC_B] if len(amp) > PLOT_SC_B else 0)
                csi_rssi.append(rssi)

        else:
            # Ignore ESP-IDF log lines and other non-CSV lines.
            continue

        if now - last_flush >= FLUSH_INTERVAL_S:
            rtt_file.flush()
            csi_file.flush()
            last_flush = now

        if now - last_print >= PRINT_INTERVAL_S:
            last_rtt = rtt_dist[-1] if rtt_dist else None
            print(f"RTT rows={rtt_count}, CSI packets={csi_count}, bad={bad_count}, last RTT={last_rtt} cm")
            last_print = now

    line_rtt.set_data(list(rtt_time), list(rtt_dist))
    ax1.relim(); ax1.autoscale_view()
    line_csi_a.set_data(list(csi_time), list(csi_amp_a))
    line_csi_b.set_data(list(csi_time), list(csi_amp_b))
    ax2.relim(); ax2.autoscale_view()
    line_rssi.set_data(list(csi_time), list(csi_rssi))
    ax3.relim(); ax3.autoscale_view()
    return line_rtt, line_csi_a, line_csi_b, line_rssi

try:
    ani = animation.FuncAnimation(fig, update, interval=100, blit=False, cache_frame_data=False)
    plt.tight_layout()
    plt.show()
except KeyboardInterrupt:
    print("Stopped by user")
finally:
    print("Closing files and serial port...")
    rtt_file.flush(); csi_file.flush()
    rtt_file.close(); csi_file.close(); ser.close()
