#!/usr/bin/env python3
"""Hardware-accelerated live dashboard for the five-anchor FTM/CSI system."""
from __future__ import annotations

import argparse
import json
import queue
import re
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np
import pyqtgraph as pg
import pyqtgraph.opengl as gl
from PySide6 import QtCore, QtGui, QtWidgets

from fused_3d_mapper import DEFAULT_CONFIG, OnlineSystem, parse_mfused


HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[1]
SCAN_RE = re.compile(r"A([1-5]):([0-9a-fA-F:]{17}):ch(\d+):rssi(-?\d+)")


def display_frame(config: dict[str, Any]) -> tuple[str, np.ndarray, dict[str, np.ndarray]]:
    """Return reference anchor, display origin, and translated anchor coordinates."""
    anchors = config["anchors"]
    requested = str(config.get("display_reference", "lowest_z_anchor"))
    if requested in anchors:
        reference_id = requested
    else:
        reference_id = min(
            anchors,
            key=lambda aid: (float(anchors[aid]["z_m"]), int(aid)),
        )
    reference = anchors[reference_id]
    origin = np.asarray(
        [reference["x_m"], reference["y_m"], reference["z_m"]], dtype=float
    )
    translated = {
        aid: np.asarray([cfg["x_m"], cfg["y_m"], cfg["z_m"]], dtype=float) - origin
        for aid, cfg in anchors.items()
    }
    return reference_id, origin, translated


def anchor_view_bounds(
    anchor_positions: dict[str, np.ndarray], padding_ratio: float = 0.18
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Fit stable 3-D bounds to anchors only, preserving one metre on every axis."""
    points = np.vstack(list(anchor_positions.values()))
    minimum = points.min(axis=0)
    maximum = points.max(axis=0)
    raw_span = maximum - minimum
    common_span = max(float(np.max(raw_span)), 1.0)
    padding = np.maximum(raw_span * padding_ratio, common_span * 0.10)
    lower = minimum - padding
    upper = maximum + padding
    span = np.maximum(upper - lower, common_span * 0.35)
    center = (minimum + maximum) / 2.0
    lower = center - span / 2.0
    upper = center + span / 2.0
    return lower, upper, center, span


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
            self.error = "缺少 pyserial，请运行 pip install -r requirements.txt"
            return

        self.raw_log.parent.mkdir(parents=True, exist_ok=True)
        with self.raw_log.open("a", encoding="utf-8", buffering=1) as log:
            while not self.stop_event.is_set():
                try:
                    with serial.Serial(self.port, self.baudrate, timeout=0.12) as device:
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


class FastDashboard(QtWidgets.QMainWindow):
    COLORS = {
        "1": (52, 152, 219, 255),
        "2": (255, 159, 67, 255),
        "3": (46, 204, 113, 255),
        "4": (255, 82, 82, 255),
        "5": (165, 94, 234, 255),
    }

    def __init__(self, config: dict[str, Any], port: str, output_dir: Path, window_s: float):
        super().__init__()
        self.config = config
        self.port = port
        self.output_dir = output_dir
        self.window_s = window_s
        self.start_monotonic = time.monotonic()
        self.messages: queue.Queue[str] = queue.Queue()
        self.system = OnlineSystem(config, output_dir)
        self.reader = SerialReader(
            port,
            int(config.get("baudrate", 115200)),
            self.messages,
            output_dir / "raw_serial.log",
        )

        self.anchor_ids = sorted(config["anchors"], key=int)
        self.reference_anchor_id, self.display_origin, self.anchor_positions = display_frame(config)
        self.view_lower, self.view_upper, self.view_center, self.view_span = anchor_view_bounds(
            self.anchor_positions, float(config.get("display_padding_ratio", 0.18))
        )
        self.history: dict[str, dict[str, deque[float]]] = {
            aid: {
                "t": deque(maxlen=900),
                "distance": deque(maxlen=900),
                "rssi": deque(maxlen=900),
                "csi": deque(maxlen=900),
            }
            for aid in self.anchor_ids
        }
        self.latest: dict[str, dict[str, Any]] = {
            aid: {
                "status": None,
                "distance": None,
                "rssi": None,
                "scan_rssi": None,
                "entries": 0,
                "csi": 0,
                "ok": 0,
                "fail": 0,
                "updated": None,
            }
            for aid in self.anchor_ids
        }
        self.trajectory: deque[np.ndarray] = deque(maxlen=800)
        self.last_solution: dict[str, Any] | None = None
        self.round_id = 0
        self.parsed_rows = 0
        self.bad_lines = 0
        self._history_dirty = True
        self._position_dirty = True
        self._closed = False

        self._build_ui()
        self.reader.start()
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.update_dashboard)
        self.timer.start(100)

    def _build_ui(self) -> None:
        self.setWindowTitle("ESP32-S3 五锚点 FTM + CSI 实时三维定位（OpenGL）")
        self.resize(1560, 900)
        self.setMinimumSize(1100, 680)
        self.setStyleSheet(
            "QMainWindow{background:#08111f;}"
            "QSplitter::handle{background:#1d2b3d;width:2px;}"
            "QStatusBar{background:#0d1928;color:#b8c7d9;border-top:1px solid #24364b;}"
            "QPushButton{background:#1c78c0;color:white;border:0;border-radius:5px;padding:7px 12px;}"
            "QPushButton:hover{background:#2493e8;}"
        )

        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        self.setCentralWidget(splitter)

        left = QtWidgets.QWidget()
        left_layout = QtWidgets.QVBoxLayout(left)
        left_layout.setContentsMargins(8, 8, 4, 4)
        left_layout.setSpacing(6)
        heading = QtWidgets.QLabel("实时三维位置 · 视野按 5 个 Anchor 自动适配")
        heading.setStyleSheet("color:#f2f7ff;font-size:18px;font-weight:600;padding:4px;")
        left_layout.addWidget(heading)

        self.scene_container = QtWidgets.QWidget()
        stack = QtWidgets.QStackedLayout(self.scene_container)
        stack.setStackingMode(QtWidgets.QStackedLayout.StackingMode.StackAll)
        stack.setContentsMargins(0, 0, 0, 0)
        self.view = gl.GLViewWidget()
        self.view.setBackgroundColor((7, 16, 29, 255))
        stack.addWidget(self.view)

        overlay = QtWidgets.QWidget()
        # Let all empty overlay space pass mouse input directly to GLViewWidget.
        # A full-size normal QWidget here would make 3-D rotation appear broken/laggy.
        overlay.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        overlay_layout = QtWidgets.QVBoxLayout(overlay)
        overlay_layout.setContentsMargins(14, 12, 14, 12)
        top_row = QtWidgets.QHBoxLayout()
        self.position_card = QtWidgets.QLabel("移动板\n等待定位数据")
        self.position_card.setMinimumWidth(245)
        self.position_card.setStyleSheet(
            "background:rgba(9,24,40,210);color:#f5f8fc;border:1px solid #31506d;"
            "border-radius:8px;padding:10px;font-size:14px;"
        )
        top_row.addWidget(self.position_card, 0, QtCore.Qt.AlignmentFlag.AlignTop)
        top_row.addStretch(1)
        overlay_layout.addLayout(top_row)
        overlay_layout.addStretch(1)
        hint = QtWidgets.QLabel("左键旋转  ·  中键平移  ·  滚轮缩放")
        hint.setStyleSheet(
            "background:rgba(9,24,40,190);color:#aebfd2;border-radius:5px;padding:6px 9px;"
        )
        overlay_layout.addWidget(hint, 0, QtCore.Qt.AlignmentFlag.AlignLeft)
        stack.addWidget(overlay)
        stack.setCurrentWidget(overlay)
        self.reset_button = QtWidgets.QPushButton("重置视角", self.scene_container)
        self.reset_button.setFixedSize(92, 34)
        self.reset_button.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.reset_button.clicked.connect(self.fit_anchor_view)
        self.reset_button.raise_()
        self.scene_container.installEventFilter(self)
        left_layout.addWidget(self.scene_container, 1)
        splitter.addWidget(left)

        right = QtWidgets.QWidget()
        right.setMinimumWidth(330)
        right.setMaximumWidth(520)
        right_layout = QtWidgets.QVBoxLayout(right)
        right_layout.setContentsMargins(4, 8, 8, 4)
        right_layout.setSpacing(8)
        self.anchor_summary = QtWidgets.QLabel()
        self.anchor_summary.setWordWrap(True)
        self.anchor_summary.setStyleSheet(
            "background:#0d1928;color:#c8d5e5;border:1px solid #24364b;"
            "border-radius:7px;padding:8px;font-size:11px;"
        )
        right_layout.addWidget(self.anchor_summary)
        self.distance_plot = self._make_plot("FTM / RTT 距离", "距离 (m)")
        self.csi_plot = self._make_plot("CSI 包数", "包/次")
        self.rssi_plot = self._make_plot("RSSI", "dBm")
        self.rssi_plot.setLabel("bottom", "最近时间", units="s")
        self.rssi_plot.setYRange(-100, -15, padding=0)
        self.csi_plot.setXLink(self.distance_plot)
        self.rssi_plot.setXLink(self.distance_plot)
        right_layout.addWidget(self.distance_plot, 1)
        right_layout.addWidget(self.csi_plot, 1)
        right_layout.addWidget(self.rssi_plot, 1)
        splitter.addWidget(right)
        splitter.setSizes([1160, 400])
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)

        self.curves: dict[str, dict[str, pg.PlotDataItem]] = {
            "distance": {}, "csi": {}, "rssi": {}
        }
        plot_map = {
            "distance": self.distance_plot,
            "csi": self.csi_plot,
            "rssi": self.rssi_plot,
        }
        for metric, plot in plot_map.items():
            for aid in self.anchor_ids:
                color = self.COLORS.get(aid, (210, 210, 210, 255))
                self.curves[metric][aid] = plot.plot(
                    [], [], pen=pg.mkPen(color, width=1.7), name=f"A{aid}",
                    skipFiniteCheck=True,
                )
        self.distance_plot.addLegend(offset=(5, 5), colCount=5, labelTextSize="8pt")
        # Re-add legend entries because the legend is created after the curves.
        legend = self.distance_plot.plotItem.legend
        if legend is not None:
            for aid in self.anchor_ids:
                legend.addItem(self.curves["distance"][aid], f"A{aid}")

        self._build_scene()
        self.fit_anchor_view()
        self._update_anchor_summary()
        self.statusBar().showMessage(f"{self.port} 正在连接 · 输出目录：{self.output_dir}")

    def eventFilter(self, watched: QtCore.QObject, event: QtCore.QEvent) -> bool:  # noqa: N802
        if watched is self.scene_container and event.type() == QtCore.QEvent.Type.Resize:
            margin = 14
            self.reset_button.move(
                max(margin, self.scene_container.width() - self.reset_button.width() - margin),
                margin,
            )
            self.reset_button.raise_()
        return super().eventFilter(watched, event)

    def _make_plot(self, title: str, y_label: str) -> pg.PlotWidget:
        plot = pg.PlotWidget(background="#0b1625", title=title)
        plot.setLabel("left", y_label)
        plot.showGrid(x=True, y=True, alpha=0.18)
        plot.setMenuEnabled(False)
        plot.setMouseEnabled(x=True, y=False)
        plot.getPlotItem().setClipToView(True)
        plot.getPlotItem().setDownsampling(auto=True, mode="peak")
        plot.getAxis("left").setTextPen("#aebfd2")
        plot.getAxis("bottom").setTextPen("#aebfd2")
        plot.getAxis("left").setPen("#40536a")
        plot.getAxis("bottom").setPen("#40536a")
        return plot

    def _build_scene(self) -> None:
        grid = gl.GLGridItem(color=(74, 96, 120, 90), antialias=False)
        grid.setSize(x=float(self.view_span[0]), y=float(self.view_span[1]), z=0.0)
        spacing = self._nice_grid_spacing(float(max(self.view_span[0], self.view_span[1])))
        grid.setSpacing(x=spacing, y=spacing, z=spacing)
        grid.translate(float(self.view_center[0]), float(self.view_center[1]), 0.0)
        self.view.addItem(grid)

        axis_length = max(float(np.max(self.view_span)) * 0.32, 0.8)
        axis_specs = (
            ("X", np.asarray([[0, 0, 0], [axis_length, 0, 0]], dtype=float),
             (1.0, 0.35, 0.35, 0.86), (255, 90, 90, 255)),
            ("Y", np.asarray([[0, 0, 0], [0, axis_length, 0]], dtype=float),
             (0.31, 0.88, 0.55, 0.86), (80, 225, 140, 255)),
            ("Z", np.asarray([[0, 0, 0], [0, 0, axis_length]], dtype=float),
             (0.35, 0.61, 1.0, 0.86), (90, 155, 255, 255)),
        )
        for name, points, line_color, text_color in axis_specs:
            self.view.addItem(gl.GLLinePlotItem(
                pos=points, color=line_color, width=2.0, antialias=False
            ))
            axis_label = gl.GLTextItem(
                pos=points[-1], text=name, color=text_color,
                font=QtGui.QFont("Segoe UI", 10, QtGui.QFont.Weight.Bold),
            )
            self.view.addItem(axis_label)

        positions = np.vstack([self.anchor_positions[aid] for aid in self.anchor_ids])
        colors = np.asarray([self.COLORS.get(aid, (220, 220, 220, 255)) for aid in self.anchor_ids]) / 255.0
        self.anchor_scatter = gl.GLScatterPlotItem(
            pos=positions, color=colors, size=15.0, pxMode=True
        )
        self.view.addItem(self.anchor_scatter)
        label_lift = max(float(self.view_span[2]) * 0.035, 0.08)
        self.anchor_labels: list[gl.GLTextItem] = []
        for aid in self.anchor_ids:
            position = self.anchor_positions[aid].copy()
            position[2] += label_lift
            suffix = " · 基准" if aid == self.reference_anchor_id else ""
            label = gl.GLTextItem(
                pos=position,
                color=self.COLORS.get(aid, (240, 240, 240, 255)),
                text=f"A{aid}{suffix}",
                font=QtGui.QFont("Microsoft YaHei", 10, QtGui.QFont.Weight.DemiBold),
            )
            self.view.addItem(label)
            self.anchor_labels.append(label)

        self.trajectory_line = gl.GLLinePlotItem(
            pos=np.empty((0, 3)), color=(0.70, 0.77, 0.84, 0.62), width=2.0,
            antialias=False, mode="line_strip"
        )
        self.tag_scatter = gl.GLScatterPlotItem(
            pos=np.empty((0, 3)), color=(1.0, 0.86, 0.34, 1.0), size=18.0, pxMode=True
        )
        self.view.addItem(self.trajectory_line)
        self.view.addItem(self.tag_scatter)

    @staticmethod
    def _nice_grid_spacing(span: float) -> float:
        target = max(span / 6.0, 0.1)
        power = 10.0 ** np.floor(np.log10(target))
        normalized = target / power
        nice = 1.0 if normalized < 1.5 else 2.0 if normalized < 3.5 else 5.0
        return float(nice * power)

    def fit_anchor_view(self) -> None:
        """Reset camera to a stable view fitted only to the five anchors."""
        max_span = float(np.max(self.view_span))
        center = QtGui.QVector3D(*[float(value) for value in self.view_center])
        self.view.opts["center"] = center
        # The grid remains anchor-fitted, while the extra camera margin keeps a
        # temporarily out-of-envelope solution visible instead of silently clipping it.
        self.view.setCameraPosition(distance=max(3.8, max_span * 2.15), elevation=24, azimuth=-52)

    def _update_anchor_summary(self) -> None:
        fragments = []
        for aid in self.anchor_ids:
            p = self.anchor_positions[aid]
            color = self.COLORS.get(aid, (220, 220, 220, 255))
            hex_color = "#{:02x}{:02x}{:02x}".format(*color[:3])
            reference = " 基准" if aid == self.reference_anchor_id else ""
            fragments.append(
                f'<span style="color:{hex_color};font-weight:600">A{aid}{reference}</span> '
                f'({p[0]:+.2f}, {p[1]:+.2f}, {p[2]:+.2f}) m'
            )
        self.anchor_summary.setText("<b>Anchor 坐标（显示坐标系）</b><br>" + "<br>".join(fragments))

    def _elapsed(self) -> float:
        return time.monotonic() - self.start_monotonic

    def _consume(self) -> None:
        for _ in range(3000):
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
                    history = self.history[aid]
                    history["t"].append(now)
                    history["distance"].append(distance)
                    history["rssi"].append(float(parsed["ftm_rssi"]))
                    history["csi"].append(float(parsed["csi_packets"]))
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

    def _update_position(self) -> None:
        if not self.trajectory:
            return
        points = np.asarray(self.trajectory, dtype=float) - self.display_origin
        # The OpenGL items are persistent: only their small data buffers change.
        self.trajectory_line.setData(pos=points)
        self.tag_scatter.setData(pos=points[-1:].copy())
        current = points[-1]
        rms = float(self.last_solution["rms"]) if self.last_solution else float("nan")
        anchors = len(self.last_solution["rows"]) if self.last_solution else 0
        self.position_card.setText(
            "<b>移动板实时位置</b><br>"
            f"X&nbsp;&nbsp;{current[0]:+.2f} m&nbsp;&nbsp;&nbsp;Y&nbsp;&nbsp;{current[1]:+.2f} m<br>"
            f"Z&nbsp;&nbsp;{current[2]:+.2f} m&nbsp;&nbsp;&nbsp;残差&nbsp;&nbsp;{rms:.2f} m<br>"
            f"轮次 {self.round_id} · 使用 {anchors} 个 Anchor"
        )

    def _update_history(self) -> None:
        now = self._elapsed()
        left = max(0.0, now - self.window_s)
        right = max(self.window_s, now)
        max_csi = 1.0
        for aid in self.anchor_ids:
            history = self.history[aid]
            times = np.asarray(history["t"], dtype=float)
            self.curves["distance"][aid].setData(times, np.asarray(history["distance"], dtype=float))
            self.curves["csi"][aid].setData(times, np.asarray(history["csi"], dtype=float))
            self.curves["rssi"][aid].setData(times, np.asarray(history["rssi"], dtype=float))
            if history["csi"]:
                max_csi = max(max_csi, max(history["csi"]))
        self.distance_plot.setXRange(left, right, padding=0)
        self.csi_plot.setYRange(0.0, max_csi * 1.18, padding=0)

    def update_dashboard(self) -> None:
        self._consume()
        if self._position_dirty:
            self._update_position()
            self._position_dirty = False
        if self._history_dirty:
            self._update_history()
            self._history_dirty = False

        now = self._elapsed()
        valid = sum(
            state["updated"] is not None
            and now - state["updated"] < 4.0
            and state["status"] == 0
            for state in self.latest.values()
        )
        link = "已连接" if self.reader.connected else "等待串口"
        warning = f" · {self.reader.error}" if self.reader.error else ""
        self.statusBar().showMessage(
            f"{self.port} {link}  |  轮次 {self.round_id}  |  有效 Anchor {valid}/5  |  "
            f"融合记录 {self.parsed_rows}  |  OpenGL 流畅模式{warning}"
        )

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # noqa: N802 (Qt API)
        if not self._closed:
            self._closed = True
            self.timer.stop()
            self.reader.stop()
            self.reader.join(timeout=2.0)
            self.system.close()
        event.accept()


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
        PROJECT_ROOT / "results" / "ftm_csi_3d_live" / time.strftime("session-%Y%m%d-%H%M%S")
    )

    pg.setConfigOptions(antialias=False, foreground="#b8c7d9")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    app.setApplicationName("ESP32-S3 FTM CSI 3D")
    app.setStyle("Fusion")
    window = FastDashboard(config, port, output, args.window)
    window.show()
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
