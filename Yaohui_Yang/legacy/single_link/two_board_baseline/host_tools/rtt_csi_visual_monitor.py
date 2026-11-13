import serial
import time
import math
from collections import deque

import numpy as np
import matplotlib.pyplot as plt
import matplotlib.animation as animation


# ============================================================
# Basic settings
# ============================================================

PORT = "COM7"
BAUD = 921600
SERIAL_TIMEOUT = 0.03

# 显示最近多少秒
WINDOW_SECONDS = 20

# 最多保留多少个 RTT 点
MAX_RTT_POINTS = 300

# 最多保留多少个 CSI packet 用于 heatmap
MAX_CSI_PACKETS = 300

# 每个动画 frame 最多处理多少行串口数据
# 太小会积压，太大 GUI 会卡
MAX_LINES_PER_FRAME = 500

# CSI 子载波显示范围
# None 表示显示全部
CSI_SC_START = 0
CSI_SC_END = None

# GUI 更新间隔，单位 ms
ANIMATION_INTERVAL_MS = 80


# ============================================================
# Serial
# ============================================================

ser = serial.Serial(PORT, BAUD, timeout=SERIAL_TIMEOUT)
time.sleep(2)

print(f"Connected to {PORT} at {BAUD} baud")
print("Visual monitor started.")
print("Close the plot window to stop.\n")


# ============================================================
# Buffers
# ============================================================

start_time = time.time()

rtt_t = deque(maxlen=MAX_RTT_POINTS)
rtt_raw = deque(maxlen=MAX_RTT_POINTS)
rtt_cal = deque(maxlen=MAX_RTT_POINTS)
rtt_filt = deque(maxlen=MAX_RTT_POINTS)
rtt_rssi = deque(maxlen=MAX_RTT_POINTS)

csi_t = deque(maxlen=MAX_CSI_PACKETS)
csi_rssi = deque(maxlen=MAX_CSI_PACKETS)
csi_mean_amp = deque(maxlen=MAX_CSI_PACKETS)
csi_amp_rows = deque(maxlen=MAX_CSI_PACKETS)

rtt_count = 0
csi_count = 0
bad_count = 0

last_print = time.time()


# ============================================================
# Parsers
# ============================================================

def parse_rttdbg(parts):
    """
    Expected format:
    RTTDBG,cycle_id,esp_time_us,status,
    rtt_raw_ns,rtt_est_ns,
    dist_est_cm,dist_raw_cm,dist_cal_cm,dist_filt_cm,
    avg_rssi,report_entries
    """
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
    """
    Expected format:
    CSI,cycle_id,esp_time_us,rssi,len,pair_count,
    real0,imag0,real1,imag1,...
    """
    if len(parts) < 8:
        return None

    try:
        cycle_id = int(parts[1])
        esp_time_us = int(parts[2])
        rssi = int(parts[3])
        csi_len = int(parts[4])
        pair_count = int(parts[5])

        raw = []
        for x in parts[6:]:
            x = x.strip()
            if x:
                raw.append(int(x))

        real = []
        imag = []

        for i in range(0, len(raw) - 1, 2):
            real.append(raw[i])
            imag.append(raw[i + 1])

        n = min(pair_count, len(real), len(imag))

        if n <= 0:
            return None

        real = np.array(real[:n], dtype=np.float32)
        imag = np.array(imag[:n], dtype=np.float32)

        amp = np.sqrt(real * real + imag * imag)

        return {
            "cycle_id": cycle_id,
            "esp_time_us": esp_time_us,
            "rssi": rssi,
            "csi_len": csi_len,
            "pair_count": n,
            "amp": amp,
        }

    except ValueError:
        return None


# ============================================================
# Plot setup
# ============================================================

plt.style.use("default")

fig = plt.figure(figsize=(14, 9))
fig.suptitle("ESP32-S3 RTT + CSI Live Visual Monitor", fontsize=16)

gs = fig.add_gridspec(4, 1, height_ratios=[1.1, 2.2, 1.0, 1.0])

ax_rtt = fig.add_subplot(gs[0])
ax_csi = fig.add_subplot(gs[1])
ax_mean = fig.add_subplot(gs[2])
ax_rssi = fig.add_subplot(gs[3])

# RTT lines
line_rtt_raw, = ax_rtt.plot([], [], linewidth=1.2, alpha=0.6, label="RTT raw distance")
line_rtt_cal, = ax_rtt.plot([], [], linewidth=1.2, alpha=0.8, label="RTT calibrated")
line_rtt_filt, = ax_rtt.plot([], [], linewidth=2.0, label="RTT filtered")

ax_rtt.set_ylabel("Distance (cm)")
ax_rtt.grid(True)
ax_rtt.legend(loc="upper right")

# CSI heatmap
empty_heatmap = np.zeros((10, 10))
im = ax_csi.imshow(
    empty_heatmap,
    aspect="auto",
    origin="lower",
    interpolation="nearest"
)
ax_csi.set_ylabel("CSI packet index")
ax_csi.set_xlabel("Subcarrier index")
ax_csi.set_title("CSI amplitude heatmap, all available subcarriers")
cbar = fig.colorbar(im, ax=ax_csi)
cbar.set_label("Amplitude")

# CSI mean amplitude
line_csi_mean, = ax_mean.plot([], [], linewidth=1.5, label="CSI mean amplitude")
ax_mean.set_ylabel("Mean amp")
ax_mean.grid(True)
ax_mean.legend(loc="upper right")

# RSSI lines
line_csi_rssi, = ax_rssi.plot([], [], linewidth=1.5, label="CSI RSSI")
line_rtt_rssi, = ax_rssi.plot([], [], linewidth=1.5, alpha=0.8, label="RTT avg RSSI")
ax_rssi.set_ylabel("RSSI (dBm)")
ax_rssi.set_xlabel("Time (s)")
ax_rssi.grid(True)
ax_rssi.legend(loc="upper right")

status_text = fig.text(
    0.01,
    0.01,
    "",
    fontsize=10,
    family="monospace"
)


# ============================================================
# Helper functions
# ============================================================

def trim_time_window():
    """
    只显示最近 WINDOW_SECONDS 的数据。
    deque 本身限制最大长度，这里主要用于坐标轴显示。
    """
    now_t = time.time() - start_time
    left = max(0, now_t - WINDOW_SECONDS)
    right = max(WINDOW_SECONDS, now_t)
    return left, right


def read_serial_lines():
    """
    每一帧从串口中尽量多读一些数据，但限制上限，防止 GUI 卡住。
    """
    global rtt_count, csi_count, bad_count, last_print

    processed = 0

    while ser.in_waiting and processed < MAX_LINES_PER_FRAME:
        processed += 1

        raw_line = ser.readline().decode("utf-8", errors="ignore").strip()

        if not raw_line:
            continue

        parts = raw_line.split(",")

        t = time.time() - start_time

        if parts[0] == "RTTDBG":
            data = parse_rttdbg(parts)

            if data is None:
                bad_count += 1
                continue

            rtt_count += 1

            if data["status"] == 0:
                rtt_t.append(t)
                rtt_raw.append(data["dist_raw_cm"])
                rtt_cal.append(data["dist_cal_cm"])
                rtt_filt.append(data["dist_filt_cm"])
                rtt_rssi.append(data["avg_rssi"])

        elif parts[0] == "CSI":
            data = parse_csi(parts)

            if data is None:
                bad_count += 1
                continue

            csi_count += 1

            amp = data["amp"]

            if CSI_SC_END is not None:
                amp = amp[CSI_SC_START:CSI_SC_END]
            else:
                amp = amp[CSI_SC_START:]

            if len(amp) <= 0:
                bad_count += 1
                continue

            csi_t.append(t)
            csi_rssi.append(data["rssi"])
            csi_mean_amp.append(float(np.mean(amp)))
            csi_amp_rows.append(amp)

        else:
            # 忽略 ESP-IDF 普通 log
            continue

    now = time.time()

    if now - last_print >= 1.0:
        last_rtt = rtt_filt[-1] if rtt_filt else None
        last_csi_amp = csi_mean_amp[-1] if csi_mean_amp else None
        print(
            f"RTT={rtt_count:5d} | "
            f"CSI={csi_count:6d} | "
            f"bad={bad_count:4d} | "
            f"last_RTT={last_rtt} cm | "
            f"last_CSI_mean_amp={last_csi_amp}"
        )
        last_print = now


def make_csi_matrix():
    """
    把不同长度的 CSI amplitude 行补齐成矩阵。
    """
    if len(csi_amp_rows) == 0:
        return None

    max_len = max(len(row) for row in csi_amp_rows)
    matrix = np.full((len(csi_amp_rows), max_len), np.nan, dtype=np.float32)

    for i, row in enumerate(csi_amp_rows):
        n = min(len(row), max_len)
        matrix[i, :n] = row[:n]

    return matrix


# ============================================================
# Animation update
# ============================================================

def update(_frame):
    read_serial_lines()

    left, right = trim_time_window()

    # ---------------- RTT plot ----------------
    if len(rtt_t) > 0:
        line_rtt_raw.set_data(list(rtt_t), list(rtt_raw))
        line_rtt_cal.set_data(list(rtt_t), list(rtt_cal))
        line_rtt_filt.set_data(list(rtt_t), list(rtt_filt))

        ax_rtt.set_xlim(left, right)

        y_vals = list(rtt_raw) + list(rtt_cal) + list(rtt_filt)
        if y_vals:
            y_min = min(y_vals)
            y_max = max(y_vals)
            pad = max(50, 0.1 * (y_max - y_min + 1))
            ax_rtt.set_ylim(y_min - pad, y_max + pad)

    # ---------------- CSI heatmap ----------------
    matrix = make_csi_matrix()

    if matrix is not None:
        # 使用 percentile，避免极端值导致整张图颜色不好看
        finite_vals = matrix[np.isfinite(matrix)]

        if finite_vals.size > 0:
            vmin = np.percentile(finite_vals, 5)
            vmax = np.percentile(finite_vals, 95)

            if vmax <= vmin:
                vmax = vmin + 1

            im.set_data(matrix)
            im.set_clim(vmin=vmin, vmax=vmax)

            ax_csi.set_xlim(0, matrix.shape[1])
            ax_csi.set_ylim(0, matrix.shape[0])

    # ---------------- CSI mean amplitude ----------------
    if len(csi_t) > 0:
        line_csi_mean.set_data(list(csi_t), list(csi_mean_amp))
        ax_mean.set_xlim(left, right)

        y_vals = list(csi_mean_amp)
        y_min = min(y_vals)
        y_max = max(y_vals)
        pad = max(5, 0.1 * (y_max - y_min + 1))
        ax_mean.set_ylim(y_min - pad, y_max + pad)

    # ---------------- RSSI plot ----------------
    if len(csi_t) > 0:
        line_csi_rssi.set_data(list(csi_t), list(csi_rssi))

    if len(rtt_t) > 0:
        line_rtt_rssi.set_data(list(rtt_t), list(rtt_rssi))

    ax_rssi.set_xlim(left, right)

    rssi_vals = list(csi_rssi) + list(rtt_rssi)
    if rssi_vals:
        y_min = min(rssi_vals)
        y_max = max(rssi_vals)
        pad = max(3, 0.1 * (y_max - y_min + 1))
        ax_rssi.set_ylim(y_min - pad, y_max + pad)

    # ---------------- Status text ----------------
    last_rtt = rtt_filt[-1] if rtt_filt else None
    last_csi = csi_mean_amp[-1] if csi_mean_amp else None

    status_text.set_text(
        f"RTT rows: {rtt_count} | "
        f"CSI packets: {csi_count} | "
        f"Bad lines: {bad_count} | "
        f"Last RTT filtered: {last_rtt} cm | "
        f"Last CSI mean amp: {last_csi}"
    )

    return (
        line_rtt_raw,
        line_rtt_cal,
        line_rtt_filt,
        im,
        line_csi_mean,
        line_csi_rssi,
        line_rtt_rssi,
        status_text,
    )


# ============================================================
# Main
# ============================================================

try:
    ani = animation.FuncAnimation(
        fig,
        update,
        interval=ANIMATION_INTERVAL_MS,
        blit=False,
        cache_frame_data=False
    )

    plt.tight_layout(rect=[0, 0.03, 1, 0.96])
    plt.show()

except KeyboardInterrupt:
    print("Stopped by user.")

finally:
    print("Closing serial port...")
    ser.close()