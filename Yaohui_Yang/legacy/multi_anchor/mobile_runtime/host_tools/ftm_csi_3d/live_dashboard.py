#!/usr/bin/env python3
"""Live FTM/CSI dashboard for the five-anchor ESP32-S3 system."""
from __future__ import annotations

import argparse
import json
import queue
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

import matplotlib.animation as animation
import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np
from matplotlib import font_manager

from fused_3d_mapper import DEFAULT_CONFIG, OnlineSystem, parse_mfused


HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[1]
SCAN_RE = re.compile(r"A([1-5]):([0-9a-fA-F:]{17}):ch(\d+):rssi(-?\d+)")


def configure_chinese_font() -> str:
    """Select an installed CJK font before any figure text is created."""
    available = {font.name for font in font_manager.fontManager.ttflist}
    candidates = (
        "Microsoft YaHei",
        "Microsoft JhengHei",
        "SimHei",
        "Noto Sans CJK SC",
    )
    selected = next((name for name in candidates if name in available), "DejaVu Sans")
    mpl.rcParams["font.family"] = "sans-serif"
    mpl.rcParams["font.sans-serif"] = [selected, "DejaVu Sans"]
    mpl.rcParams["axes.unicode_minus"] = False
    return selected


class SerialReader(threading.Thread):
    def __init__(self, port: str, baudrate: int, messages: queue.Queue[str], raw_log: Path):
        super().__init__(daemon=True)
        self.port = port
        self.baudrate = baudrate
        self.messages = messages
        self.raw_log = raw_log
        self.stop_event = threading.Event()
        self.connected = False
        self.error = ""

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        try:
            import serial
        except ImportError:
            self.error = "缺少 pyserial：请运行 pip install -r requirements.txt"
            return

        self.raw_log.parent.mkdir(parents=True, exist_ok=True)
        with self.raw_log.open("a", encoding="utf-8", buffering=1) as log:
            while not self.stop_event.is_set():
                try:
                    with serial.Serial(self.port, self.baudrate, timeout=0.15) as device:
                        self.connected = True
                        self.error = ""
                        while not self.stop_event.is_set():
                            raw = device.readline()
                            if not raw:
                                continue
                            line = raw.decode("utf-8", errors="ignore").strip()
                            if not line:
                                continue
                            log.write(f"{time.time():.6f},{line}\n")
                            self.messages.put(line)
                except serial.SerialException as exc:
                    self.connected = False
                    self.error = str(exc)
                    if self.stop_event.wait(1.0):
                        break
            self.connected = False


class LiveDashboard:
    def __init__(self, config: dict[str, Any], port: str, output_dir: Path, window_s: float):
        self.config = config
        self.port = port
        self.output_dir = output_dir
        self.window_s = window_s
        self.start_monotonic = time.monotonic()
        self.messages: queue.Queue[str] = queue.Queue()
        self.system = OnlineSystem(config, output_dir)
        self.reader = SerialReader(
            port, int(config.get("baudrate", 115200)), self.messages,
            output_dir / "raw_serial.log")
        self.reader.start()

        self.anchor_ids = sorted(config["anchors"], key=int)
        self.colors = {aid: plt.get_cmap("tab10")((int(aid) - 1) % 10)
                       for aid in self.anchor_ids}
        self.history: dict[str, dict[str, deque[float]]] = {
            aid: {
                "t": deque(maxlen=600),
                "distance": deque(maxlen=600),
                "rssi": deque(maxlen=600),
                "csi": deque(maxlen=600),
            }
            for aid in self.anchor_ids
        }
        self.latest: dict[str, dict[str, Any]] = {
            aid: {
                "status": None, "distance": None, "rssi": None,
                "scan_rssi": None, "entries": 0, "csi": 0,
                "ok": 0, "fail": 0, "updated": None,
            }
            for aid in self.anchor_ids
        }
        self.trajectory: deque[np.ndarray] = deque(maxlen=500)
        self.last_solution: dict[str, Any] | None = None
        self.round_id = 0
        self.bad_lines = 0
        self.parsed_rows = 0
        anchor_items = list(config["anchors"].items())
        self.reference_anchor_id, reference_cfg = min(
            anchor_items, key=lambda item: (float(item[1]["z_m"]), int(item[0])))
        self.display_origin = np.asarray([
            reference_cfg["x_m"], reference_cfg["y_m"], reference_cfg["z_m"]
        ], dtype=float)
        self._position_dirty = True
        self._history_dirty = True
        self._dragging_3d = False

        self.font_name = configure_chinese_font()
        self.figure = plt.figure(figsize=(16, 9))
        self.figure.canvas.manager.set_window_title("ESP32-S3 FTM + CSI 实时定位")
        grid = self.figure.add_gridspec(
            3, 2,
            width_ratios=(3.4, 1.15),
            height_ratios=(1.0, 1.0, 1.0),
            wspace=0.18,
            hspace=0.42,
        )
        self.ax_position = self.figure.add_subplot(grid[:, 0], projection="3d")
        self.ax_distance = self.figure.add_subplot(grid[0, 1])
        self.ax_csi = self.figure.add_subplot(grid[1, 1], sharex=self.ax_distance)
        self.ax_rssi = self.figure.add_subplot(grid[2, 1], sharex=self.ax_distance)
        self.ax_position.view_init(elev=24, azim=-58)
        self.figure.suptitle("ESP32-S3 五锚点 FTM + CSI 实时三维定位")
        self.status_text = self.figure.text(
            0.01, 0.01, "", family=self.font_name, fontsize=9)
        self.figure.subplots_adjust(left=0.035, right=0.985, bottom=0.075, top=0.92)
        self.figure.canvas.mpl_connect("close_event", self._on_close)
        self.figure.canvas.mpl_connect("button_press_event", self._on_button_press)
        self.figure.canvas.mpl_connect("button_release_event", self._on_button_release)

    def _on_close(self, _event: Any) -> None:
        self.reader.stop()
        self.reader.join(timeout=2.0)
        self.system.close()

    def _on_button_press(self, event: Any) -> None:
        """Pause periodic redraws while the user rotates the 3D axes."""
        if event.inaxes is not self.ax_position:
            return
        self._dragging_3d = True
        if hasattr(self, "animation"):
            self.animation.event_source.stop()

    def _on_button_release(self, _event: Any) -> None:
        if not self._dragging_3d:
            return
        self._dragging_3d = False
        if hasattr(self, "animation"):
            self.animation.event_source.start()
        self.figure.canvas.draw_idle()

    def _elapsed(self) -> float:
        return time.monotonic() - self.start_monotonic

    def _consume(self) -> None:
        for _ in range(2000):
            try:
                line = self.messages.get_nowait()
            except queue.Empty:
                break

            parsed = parse_mfused(line)
            if parsed:
                self.parsed_rows += 1
                aid = parsed["anchor"]
                if aid not in self.latest:
                    continue
                now = self._elapsed()
                state = self.latest[aid]
                state.update({
                    "status": parsed["status"],
                    "rssi": parsed["ftm_rssi"] if parsed["entries"] else parsed["scan_rssi"],
                    "scan_rssi": parsed["scan_rssi"],
                    "entries": parsed["entries"],
                    "csi": parsed["csi_packets"],
                    "ok": parsed["ok_count"],
                    "fail": parsed["fail_count"],
                    "updated": now,
                })
                if parsed["status"] == 0 and parsed["filtered_cm"] > 0:
                    distance = parsed["filtered_cm"] / 100.0
                    state["distance"] = distance
                    self.history[aid]["t"].append(now)
                    self.history[aid]["distance"].append(distance)
                    self.history[aid]["rssi"].append(float(parsed["ftm_rssi"]))
                    self.history[aid]["csi"].append(float(parsed["csi_packets"]))
                    self._history_dirty = True
                self.system.ingest(parsed)
                continue

            if line.startswith("MSCAN,"):
                for aid, _bssid, _channel, rssi in SCAN_RE.findall(line):
                    if aid in self.latest:
                        self.latest[aid]["scan_rssi"] = int(rssi)
                        if self.latest[aid]["rssi"] is None:
                            self.latest[aid]["rssi"] = int(rssi)
                continue

            if line.startswith("MROUND,"):
                try:
                    round_id = int(line.split(",", 2)[1])
                except (IndexError, ValueError):
                    self.bad_lines += 1
                    continue
                self.round_id = round_id
                solution = self.system.finish_round(round_id)
                if solution:
                    self.last_solution = solution
                    self.trajectory.append(solution["position"].copy())
                    self._position_dirty = True

    def _display_geometry(
            self) -> tuple[dict[str, np.ndarray], np.ndarray, np.ndarray, np.ndarray]:
        """Translate the display frame and derive stable equal-scale 3D bounds."""
        anchors = {
            aid: np.asarray([cfg["x_m"], cfg["y_m"], cfg["z_m"]], dtype=float)
            - self.display_origin
            for aid, cfg in self.config["anchors"].items()
        }
        trajectory = (
            np.asarray(self.trajectory, dtype=float) - self.display_origin
            if self.trajectory else np.empty((0, 3), dtype=float)
        )
        cloud = np.vstack([*anchors.values(), trajectory]) if trajectory.size else np.vstack(
            list(anchors.values()))
        data_min = cloud.min(axis=0)
        data_max = cloud.max(axis=0)
        largest_span = float(np.max(data_max - data_min))
        padding_ratio = float(self.config.get("display_padding_ratio", 0.12))
        padding = max(0.5, largest_span * padding_ratio)
        cube_span = max(float(self.config.get("display_min_span_m", 4.0)),
                        largest_span + 2.0 * padding)
        lower = np.empty(3, dtype=float)
        upper = np.empty(3, dtype=float)
        for axis in range(3):
            if data_min[axis] >= -1e-9:
                lower[axis], upper[axis] = 0.0, cube_span
            elif data_max[axis] <= 1e-9:
                lower[axis], upper[axis] = -cube_span, 0.0
            else:
                center = (data_min[axis] + data_max[axis]) / 2.0
                lower[axis], upper[axis] = center - cube_span / 2.0, center + cube_span / 2.0
        return anchors, trajectory, lower, upper

    def _draw_position(self) -> None:
        ax = self.ax_position
        ax.clear()
        anchor_positions, points, minimum, maximum = self._display_geometry()
        ax.set_xlim3d(minimum[0], maximum[0])
        ax.set_ylim3d(minimum[1], maximum[1])
        ax.set_zlim3d(minimum[2], maximum[2])
        ax.set_box_aspect((1.0, 1.0, 1.0))
        ax.set_xlabel("X (m)")
        ax.set_ylabel("Y (m)")
        ax.set_zlabel("Z (m)")
        title = f"实时 3D 定位轨迹（基准 A{self.reference_anchor_id}=0,0,0；自适应范围）"
        if not self.config.get("coordinates_verified", False):
            title += "（坐标尚未标定）"
        ax.set_title(title, pad=14)
        ax.grid(True, alpha=0.25)

        for aid, cfg in self.config["anchors"].items():
            position = anchor_positions[aid]
            ax.scatter(*position, marker="^", s=90,
                       color=self.colors[aid], edgecolor="black", linewidth=0.6)
            label = f"A{aid} 基准" if aid == self.reference_anchor_id else f"A{aid}"
            label_offset = max(0.18, (maximum[2] - minimum[2]) * 0.025)
            ax.text(position[0], position[1], position[2] + label_offset,
                    label, color=self.colors[aid], ha="center")

        if points.size:
            ax.plot(points[:, 0], points[:, 1], points[:, 2],
                    color="0.45", linewidth=1.5,
                    alpha=0.8, label="轨迹")
            current = points[-1]
            ax.scatter(current[0], current[1], current[2], marker="o", s=120,
                       color="black", label="移动板")
            point_offset = max(0.25, (maximum[2] - minimum[2]) * 0.035)
            ax.text(current[0], current[1], current[2] + point_offset,
                    f"({current[0]:.2f}, {current[1]:.2f}, {current[2]:.2f}) m",
                    ha="center")
            ax.legend(loc="upper right")
        else:
            ax.text2D(0.5, 0.5, "等待至少 4 个有效锚点距离",
                      ha="center", va="center", transform=ax.transAxes)

    def _draw_history(self) -> None:
        now = self._elapsed()
        left = max(0.0, now - self.window_s)
        right = max(self.window_s, now)

        self.ax_distance.clear()
        self.ax_distance.set_title(f"最近 {self.window_s:.0f} 秒 FTM 距离")
        self.ax_distance.set_ylabel("RTT (m)")
        self.ax_distance.set_xlim(left, right)
        self.ax_distance.grid(True, alpha=0.25)

        self.ax_csi.clear()
        self.ax_csi.set_title(f"最近 {self.window_s:.0f} 秒 CSI 包数")
        self.ax_csi.set_ylabel("CSI 包/次")
        self.ax_csi.set_xlim(left, right)
        self.ax_csi.grid(True, alpha=0.25)

        self.ax_rssi.clear()
        self.ax_rssi.set_title(f"最近 {self.window_s:.0f} 秒 RSSI")
        self.ax_rssi.set_xlabel("时间 (s)")
        self.ax_rssi.set_ylabel("RSSI (dBm)")
        self.ax_rssi.set_xlim(left, right)
        self.ax_rssi.set_ylim(-100, -15)
        self.ax_rssi.grid(True, alpha=0.25)

        plotted = False
        for aid in self.anchor_ids:
            history = self.history[aid]
            if not history["t"]:
                continue
            plotted = True
            label = f"A{aid}"
            self.ax_distance.plot(history["t"], history["distance"],
                                  color=self.colors[aid], linewidth=1.5, label=label)
            self.ax_csi.plot(history["t"], history["csi"],
                             color=self.colors[aid], linewidth=1.2, label=label)
            self.ax_rssi.plot(history["t"], history["rssi"],
                              color=self.colors[aid], linewidth=1.3, label=label)
        if plotted:
            legend_args = {"loc": "upper right", "ncols": 5,
                           "fontsize": 7, "columnspacing": 0.6,
                           "handlelength": 1.2}
            self.ax_distance.legend(**legend_args)
            self.ax_csi.legend(**legend_args)
            self.ax_rssi.legend(**legend_args)
            max_csi = max(max(self.history[aid]["csi"], default=0.0)
                          for aid in self.anchor_ids)
            self.ax_csi.set_ylim(0.0, max(1.0, max_csi * 1.15))

    def update(self, _frame: int) -> tuple[()]:
        self._consume()
        if self._dragging_3d:
            return ()
        if self._position_dirty:
            self._draw_position()
            self._position_dirty = False
        if self._history_dirty:
            self._draw_history()
            self._history_dirty = False
        valid = sum(
            state["updated"] is not None
            and self._elapsed() - state["updated"] < 4.0
            and state["status"] == 0
            for state in self.latest.values()
        )
        link = "已连接" if self.reader.connected else "等待串口"
        warning = f" | {self.reader.error}" if self.reader.error else ""
        self.status_text.set_text(
            f"{self.port} {link} | 测量轮次={self.round_id} | 有效锚点={valid}/5 | "
            f"融合记录={self.parsed_rows} | 字体={self.font_name}{warning}")
        return ()

    def show(self) -> None:
        self.animation = animation.FuncAnimation(
            self.figure, self.update, interval=500, blit=False,
            cache_frame_data=False)
        plt.show()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--port", help="覆盖配置文件中的串口")
    parser.add_argument("--window", type=float, default=60.0, help="曲线时间窗口（秒）")
    parser.add_argument("--output", type=Path, help="本次运行的输出目录")
    args = parser.parse_args()

    config = json.loads(args.config.read_text(encoding="utf-8"))
    port = args.port or config["serial_port"]
    output = args.output or (
        PROJECT_ROOT / "results" / "ftm_csi_3d_live"
        / time.strftime("session-%Y%m%d-%H%M%S"))
    dashboard = LiveDashboard(config, port, output, args.window)
    dashboard.show()


if __name__ == "__main__":
    main()
