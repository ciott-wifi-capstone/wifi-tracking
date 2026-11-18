import argparse
import csv
import sys
import time
from collections import deque
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation
import serial

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LIVE_LOG_DIR = PROJECT_ROOT / "data" / "live_logs"

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Live plot RTT data from ESP32 serial output.")
    p.add_argument("--port", required=True, help="Serial port, e.g. COM8")
    p.add_argument("--baud", type=int, default=115200, help="Baud rate")
    p.add_argument("--window", type=int, default=120, help="Number of recent samples shown")
    p.add_argument("--csv", type=str, default=str(LIVE_LOG_DIR / "rtt_live_log.csv"), help="CSV file to save RTT data")
    p.add_argument("--dbg_csv", type=str, default=str(LIVE_LOG_DIR / "rtt_debug_log.csv"), help="CSV file to save RTTDBG data")
    return p


class RTTPlotter:
    def __init__(self, port: str, baud: int, window: int, csv_path: str, dbg_csv_path: str):
        self.ser = serial.Serial(port, baud, timeout=0.1)
        self.window = window
        self.csv_path = Path(csv_path)
        self.dbg_csv_path = Path(dbg_csv_path)
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        self.dbg_csv_path.parent.mkdir(parents=True, exist_ok=True)

        self.rounds = deque(maxlen=window)
        self.rtt_ns = deque(maxlen=window)
        self.dist_cm = deque(maxlen=window)
        self.end_us = deque(maxlen=window)

        self.dbg_rounds = deque(maxlen=window)
        self.rssi_dbm = deque(maxlen=window)
        self.entries = deque(maxlen=window)
        self.status_codes = deque(maxlen=window)
        self.quality_scores = deque(maxlen=window)
        self.dist_reg_cm = deque(maxlen=window)

        self.last_round = None
        self.total_points = 0
        self.total_dbg_points = 0

        self.csv_file = self.csv_path.open("a", newline="", encoding="utf-8")
        self.csv_writer = csv.writer(self.csv_file)
        if self.csv_file.tell() == 0:
            self.csv_writer.writerow([
                "pc_time",
                "round_id",
                "rtt_ns",
                "dist_cm",
                "end_us"
            ])

        self.dbg_csv_file = self.dbg_csv_path.open("a", newline="", encoding="utf-8")
        self.dbg_csv_writer = csv.writer(self.dbg_csv_file)
        if self.dbg_csv_file.tell() == 0:
            self.dbg_csv_writer.writerow([
                "pc_time",
                "round_id",
                "status",
                "rtt_raw_ns",
                "rtt_est_ns",
                "dist_est_cm",
                "dist_raw_cm",
                "dist_cal_cm",
                "dist_reg_cm",
                "quality_score",
                "avg_rssi",
                "report_entries"
            ])

        self.fig, (self.ax1, self.ax2, self.ax3) = plt.subplots(3, 1, figsize=(10, 9), sharex=True)
        self.fig.canvas.manager.set_window_title("ESP32 RTT Live Plot")

        self.line_rtt, = self.ax1.plot([], [], linewidth=1.8, label="RTT (ns)")
        self.line_dist, = self.ax2.plot([], [], linewidth=1.8, label="Distance (cm)")
        self.line_rssi, = self.ax3.plot([], [], linewidth=1.8, label="RSSI (dBm)")

        self.ax1.set_ylabel("RTT (ns)")
        self.ax2.set_ylabel("Distance (cm)")
        self.ax3.set_ylabel("RSSI (dBm)")
        self.ax3.set_xlabel("Sample index")

        self.ax1.set_title("ESP32 Wi-Fi RTT Real-time Monitor")
        self.ax1.grid(True, alpha=0.3)
        self.ax2.grid(True, alpha=0.3)
        self.ax3.grid(True, alpha=0.3)

        self.ax1.legend(loc="upper left")
        self.ax2.legend(loc="upper left")
        self.ax3.legend(loc="upper left")

        self.status = self.fig.text(0.02, 0.01, "Waiting for data...", fontsize=9)

    def close(self):
        try:
            self.csv_file.close()
        except Exception:
            pass
        try:
            self.dbg_csv_file.close()
        except Exception:
            pass
        try:
            self.ser.close()
        except Exception:
            pass

    def parse_rtt_line(self, line: str):
        # RTT,round_id,rtt_ns,dist_cm,end_us
        if not line.startswith("RTT,"):
            return None
        parts = line.split(",")
        if len(parts) != 5:
            return None
        try:
            round_id = int(parts[1])
            rtt_ns = int(parts[2])
            dist_cm = int(parts[3])
            end_us = int(parts[4])
            return round_id, rtt_ns, dist_cm, end_us
        except ValueError:
            return None

    def parse_rttdbg_line(self, line: str):
        # RTTDBG,round_id,status,rtt_raw,rtt_est,dist_est,dist_raw,dist_cal,dist_reg,quality,rssi,entries
        if not line.startswith("RTTDBG,"):
            return None
        parts = line.split(",")
        if len(parts) != 12:
            return None
        try:
            return {
                "round_id": int(parts[1]),
                "status": int(parts[2]),
                "rtt_raw_ns": int(parts[3]),
                "rtt_est_ns": int(parts[4]),
                "dist_est_cm": int(parts[5]),
                "dist_raw_cm": int(parts[6]),
                "dist_cal_cm": int(parts[7]),
                "dist_reg_cm": int(parts[8]),
                "quality_score": int(parts[9]),
                "avg_rssi": int(parts[10]),
                "report_entries": int(parts[11]),
            }
        except ValueError:
            return None

    def read_serial(self):
        while True:
            raw = self.ser.readline()
            if not raw:
                break
            try:
                line = raw.decode("utf-8", errors="ignore").strip()
            except Exception:
                continue

            parsed_rtt = self.parse_rtt_line(line)
            if parsed_rtt is not None:
                round_id, rtt_ns, dist_cm, end_us = parsed_rtt

                if self.last_round is not None and round_id == self.last_round:
                    continue

                self.last_round = round_id
                self.total_points += 1

                self.rounds.append(round_id)
                self.rtt_ns.append(rtt_ns)
                self.dist_cm.append(dist_cm)
                self.end_us.append(end_us)

                self.csv_writer.writerow([
                    time.time(),
                    round_id,
                    rtt_ns,
                    dist_cm,
                    end_us
                ])
                self.csv_file.flush()
                continue

            parsed_dbg = self.parse_rttdbg_line(line)
            if parsed_dbg is not None:
                self.total_dbg_points += 1

                self.dbg_rounds.append(parsed_dbg["round_id"])
                self.rssi_dbm.append(parsed_dbg["avg_rssi"])
                self.entries.append(parsed_dbg["report_entries"])
                self.status_codes.append(parsed_dbg["status"])
                self.quality_scores.append(parsed_dbg["quality_score"])
                self.dist_reg_cm.append(parsed_dbg["dist_reg_cm"])

                self.dbg_csv_writer.writerow([
                    time.time(),
                    parsed_dbg["round_id"],
                    parsed_dbg["status"],
                    parsed_dbg["rtt_raw_ns"],
                    parsed_dbg["rtt_est_ns"],
                    parsed_dbg["dist_est_cm"],
                    parsed_dbg["dist_raw_cm"],
                    parsed_dbg["dist_cal_cm"],
                    parsed_dbg["dist_reg_cm"],
                    parsed_dbg["quality_score"],
                    parsed_dbg["avg_rssi"],
                    parsed_dbg["report_entries"],
                ])
                self.dbg_csv_file.flush()
                continue

    def autoscale_axis(self, ax, data):
        if not data:
            return
        ymin = min(data)
        ymax = max(data)
        if ymin == ymax:
            pad = max(1, abs(ymin) * 0.1)
        else:
            pad = max(1, (ymax - ymin) * 0.15)
        ax.set_ylim(ymin - pad, ymax + pad)

    def update(self, _frame):
        self.read_serial()

        if not self.rounds and not self.dbg_rounds:
            self.status.set_text("Waiting for RTT / RSSI data...")
            return self.line_rtt, self.line_dist, self.line_rssi, self.status

        x_rtt = list(range(len(self.rounds)))
        x_dbg = list(range(len(self.dbg_rounds)))

        if self.rounds:
            self.line_rtt.set_data(x_rtt, list(self.rtt_ns))
            self.line_dist.set_data(x_rtt, list(self.dist_cm))
            self.ax1.set_xlim(0, max(10, len(self.rounds) - 1))
            self.ax2.set_xlim(0, max(10, len(self.rounds) - 1))
            self.autoscale_axis(self.ax1, self.rtt_ns)
            self.autoscale_axis(self.ax2, self.dist_cm)

        if self.dbg_rounds:
            self.line_rssi.set_data(x_dbg, list(self.rssi_dbm))
            self.ax3.set_xlim(0, max(10, len(self.dbg_rounds) - 1))
            self.autoscale_axis(self.ax3, self.rssi_dbm)

        latest_text = []
        if self.rounds:
            latest_text.append(
                f"RTT round={self.rounds[-1]} | RTT={self.rtt_ns[-1]} ns | Dist={self.dist_cm[-1]} cm"
            )
        if self.dbg_rounds:
            latest_text.append(
                f"RSSI={self.rssi_dbm[-1]} dBm | entries={self.entries[-1]} | "
                f"status={self.status_codes[-1]} | Q={self.quality_scores[-1]} | "
                f"reg={self.dist_reg_cm[-1]} cm"
            )
        latest_text.append(f"RTT points={self.total_points} | RTTDBG={self.total_dbg_points}")

        self.status.set_text(" | ".join(latest_text))

        return self.line_rtt, self.line_dist, self.line_rssi, self.status


def main():
    args = build_parser().parse_args()

    try:
        plotter = RTTPlotter(args.port, args.baud, args.window, args.csv, args.dbg_csv)
    except serial.SerialException as e:
        print(f"Failed to open serial port: {e}")
        sys.exit(1)

    ani = FuncAnimation(
        plotter.fig,
        plotter.update,
        interval=50,
        blit=False,
        cache_frame_data=False
    )

    try:
        plt.tight_layout(rect=[0, 0.03, 1, 1])
        plt.show()
    finally:
        plotter.close()


if __name__ == "__main__":
    main()
