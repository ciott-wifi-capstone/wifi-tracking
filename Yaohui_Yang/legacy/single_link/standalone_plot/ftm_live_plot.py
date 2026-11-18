import csv
import re
import time
from collections import deque

import serial
import matplotlib.pyplot as plt
from matplotlib.ticker import AutoMinorLocator, FormatStrFormatter

# =========================
# 配置区
# =========================
PORT = "COM6"              # 发起测距的板子串口
BAUD = 115200
SSID = "FTM"
CSV_FILE = "ftm_live.csv"

# 测量间隔（秒）
MEASURE_INTERVAL_SEC = 0.5

# FTM 参数
# -c 可选 8,16,24,32,64；64 通常更稳
# -p 单位是 100 ms；2 表示 200 ms
FTM_CMD = f"ftm -I -s {SSID} -c 64 -p 2\n"

# 匹配两种格式：
# Estimated RTT = 5 nSec, Estimated Distance = 0.75 meters
# Estimated RTT - 5 nSec, Estimated Distance - 0.75 meters
RESULT_RE = re.compile(
    r"Estimated RTT\s*[-=]\s*(\d+)\s*nSec,\s*Estimated Distance\s*[-=]\s*([0-9.]+)\s*meters",
    re.IGNORECASE
)

# 也兼容可能没有逗号的情况
RESULT_RE_ALT = re.compile(
    r"Estimated RTT\s*[-=]\s*(\d+)\s*nSec.*?Estimated Distance\s*[-=]\s*([0-9.]+)\s*meters",
    re.IGNORECASE | re.DOTALL
)


def moving_average(values, window=5):
    vals = list(values)
    out = []
    for i in range(len(vals)):
        start = max(0, i - window + 1)
        seg = vals[start:i + 1]
        out.append(sum(seg) / len(seg))
    return out


def median_filter(values, window=5):
    vals = list(values)
    out = []
    for i in range(len(vals)):
        start = max(0, i - window + 1)
        seg = sorted(vals[start:i + 1])
        n = len(seg)
        if n % 2 == 1:
            out.append(seg[n // 2])
        else:
            out.append((seg[n // 2 - 1] + seg[n // 2]) / 2.0)
    return out


def wait_for_prompt(ser, timeout=10):
    end = time.time() + timeout
    buf = ""
    while time.time() < end:
        if ser.in_waiting:
            chunk = ser.read(ser.in_waiting).decode(errors="ignore")
            print(chunk, end="")
            buf += chunk
            if "ftm>" in buf:
                return True
        time.sleep(0.05)
    return False


def send_ftm_and_collect(ser, cmd, timeout=15):
    ser.reset_input_buffer()
    ser.write(cmd.encode())
    ser.flush()

    end = time.time() + timeout
    buf = ""

    while time.time() < end:
        if ser.in_waiting:
            chunk = ser.read(ser.in_waiting).decode(errors="ignore")
            print(chunk, end="")
            buf += chunk

            # 看到结果后稍微多等一下，避免最后一行还没刷完
            if "Estimated RTT" in buf and "Estimated Distance" in buf:
                time.sleep(0.25)

            if "ftm>" in buf:
                break
        time.sleep(0.05)

    return buf


def parse_result(text):
    m = RESULT_RE.search(text)
    if m:
        return int(m.group(1)), float(m.group(2))

    m = RESULT_RE_ALT.search(text)
    if m:
        return int(m.group(1)), float(m.group(2))

    return None, None


def print_recent_rows(times, rtts, dists, n=10):
    print("\n最近测量结果：")
    print(f"{'No.':>4} | {'Time(s)':>8} | {'RTT(ns)':>8} | {'Distance(m)':>11}")
    print("-" * 42)

    start_idx = max(0, len(times) - n)
    for i in range(start_idx, len(times)):
        print(f"{i+1:>4} | {times[i]:>8.3f} | {rtts[i]:>8d} | {dists[i]:>11.3f}")


def update_plot(fig, ax, times, rtts, dists):
    raw_times = list(times)
    raw_dists = list(dists)

    ma_dists = moving_average(raw_dists, window=5)
    med_dists = median_filter(raw_dists, window=5)

    ax.clear()

    # 原始值
    ax.plot(
        raw_times,
        raw_dists,
        marker="o",
        markersize=3,
        linewidth=1,
        alpha=0.35,
        label="Raw"
    )

    # 中值滤波
    ax.plot(
        raw_times,
        med_dists,
        linewidth=1.8,
        label="Median(5)"
    )

    # 滑动平均
    ax.plot(
        raw_times,
        ma_dists,
        linewidth=2.2,
        label="Moving Avg(5)"
    )

    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Distance (m)")
    ax.set_title("ESP32-S3 FTM Live Distance")
    ax.yaxis.set_major_formatter(FormatStrFormatter("%.3f"))

    ax.xaxis.set_minor_locator(AutoMinorLocator())
    ax.yaxis.set_minor_locator(AutoMinorLocator())
    ax.grid(True, which="major", linestyle="-", alpha=0.5)
    ax.grid(True, which="minor", linestyle=":", alpha=0.3)

    if len(raw_dists) >= 1:
        ymin = min(raw_dists)
        ymax = max(raw_dists)

        # 给图留一点上下边距，避免挤在一起
        margin = max(0.02, (ymax - ymin) * 0.25 if ymax > ymin else 0.05)
        ax.set_ylim(ymin - margin, ymax + margin)

        # 标记最新点
        latest_t = raw_times[-1]
        latest_d = raw_dists[-1]
        ax.annotate(
            f"{latest_d:.3f} m",
            xy=(latest_t, latest_d),
            xytext=(8, 8),
            textcoords="offset points"
        )

    ax.legend(loc="best")
    fig.tight_layout()
    plt.pause(0.01)


def main():
    print(f"正在连接串口 {PORT} ...")
    ser = serial.Serial(PORT, BAUD, timeout=0.1)
    time.sleep(2)

    # 唤醒命令行
    ser.write(b"\n")
    ser.flush()

    print("等待 ftm> 提示符...")
    if not wait_for_prompt(ser, timeout=10):
        print("\n没有等到 ftm>，请按一下 COM6 那块板子的 RESET，再重试。")
        ser.close()
        return

    print("\n已进入 ftm>，默认跳过 scan，直接开始循环测距。")
    print("请确认另一块板子（COM5）已经运行：ap FTM password\n")

    plt.ion()
    fig, ax = plt.subplots(figsize=(10, 5), dpi=130)

    times = deque(maxlen=300)
    rtts = deque(maxlen=300)
    dists = deque(maxlen=300)

    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp_s", "rtt_ns", "distance_m"])
        f.flush()

        start = time.time()
        idx = 0

        while True:
            idx += 1
            print(f"\n===== 第 {idx} 次测量 =====")
            out = send_ftm_and_collect(ser, FTM_CMD, timeout=15)

            rtt_ns, dist_m = parse_result(out)

            if rtt_ns is not None and dist_m is not None:
                t = time.time() - start

                writer.writerow([f"{t:.3f}", rtt_ns, f"{dist_m:.3f}"])
                f.flush()

                times.append(t)
                rtts.append(rtt_ns)
                dists.append(dist_m)

                update_plot(fig, ax, times, rtts, dists)

                print(f"\n保存成功: t={t:.3f}s, RTT={rtt_ns}ns, Distance={dist_m:.3f}m")
                print_recent_rows(list(times), list(rtts), list(dists), n=8)
            else:
                print("\n这次没有解析到测距结果。")
                print("本次原始输出如下：")
                print(out)
                plt.pause(0.01)

            time.sleep(MEASURE_INTERVAL_SEC)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已停止。")