#!/usr/bin/env python3
"""Realtime GUI for the strict-epoch 7-anchor ESP32-S3 localizer."""
from __future__ import annotations

import argparse
import json
import math
import queue
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from cir_features import parse_csiraw
from localizer_core import (
    Measurement, StrictEpochLocalizer, parse_mfused, parse_round_start, parse_round_meta
)

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "anchors_7.json"
DEFAULT_OUTPUT = HERE / "results_7anchor_epoch"

class SerialReader(threading.Thread):
    def __init__(self, port: str, baud: int, messages: queue.Queue[str], raw_path: Path):
        super().__init__(daemon=True)
        self.port, self.baud, self.messages, self.raw_path = port, baud, messages, raw_path
        self.stop_event = threading.Event()
        self.connected = False
        self.error = ""

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        try:
            import serial
        except ImportError:
            self.error = "缺少 pyserial"
            return
        self.raw_path.parent.mkdir(parents=True, exist_ok=True)
        with self.raw_path.open("a", encoding="utf-8", buffering=1) as log:
            while not self.stop_event.is_set():
                try:
                    with serial.Serial(self.port, self.baud, timeout=0.15) as ser:
                        self.connected = True
                        self.error = ""
                        while not self.stop_event.is_set():
                            raw = ser.readline()
                            if not raw:
                                continue
                            line = raw.decode("utf-8", errors="ignore").strip()
                            if not line:
                                continue
                            log.write(f"{time.time():.6f},{line}\n")
                            self.messages.put(line)
                except Exception as exc:
                    self.connected = False
                    self.error = str(exc)
                    self.stop_event.wait(1.0)
        self.connected = False


class Dashboard(QtWidgets.QMainWindow):
    COLORS = {
        "1": "#3498db", "2": "#ff9f43", "3": "#2ecc71", "4": "#ff5252",
        "5": "#a55eea", "6": "#00cec9", "8": "#e84393",
    }

    def __init__(self, config: dict[str, Any], port: str, output: Path):
        super().__init__()
        self.config, self.port, self.output = config, port, output
        self.localizer = StrictEpochLocalizer(config, output)
        self.messages: queue.Queue[str] = queue.Queue()
        self.reader = SerialReader(port, int(config.get("baudrate", 115200)), self.messages,
                                   output / "raw_serial.log")
        self.anchor_ids = sorted(self.localizer.anchors, key=int)
        self.last_solution: dict[str, Any] | None = None
        self.raw_traj: deque[np.ndarray] = deque(maxlen=int(config.get("view", {}).get("trajectory_points", 600)))
        self.filtered_traj: deque[np.ndarray] = deque(maxlen=int(config.get("view", {}).get("trajectory_points", 600)))
        self.time_hist: deque[float] = deque(maxlen=800)
        self.distance_hist = {aid: deque(maxlen=800) for aid in self.anchor_ids}
        self.rssi_hist = {aid: deque(maxlen=800) for aid in self.anchor_ids}
        self.latest_measurement: dict[str, Measurement | None] = {aid: None for aid in self.anchor_ids}
        self.parsed_count = 0
        self.cir_count = 0
        self.bad_count = 0
        self._build_ui()
        self.reader.start()
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(80)

    def _build_ui(self) -> None:
        self.setWindowTitle(f"ESP32-S3 · 7 Anchor · {self.localizer.mode.upper()}动态权重 · 实时二维定位")
        self.resize(1540, 900)
        self.setStyleSheet("QMainWindow{background:#08111f;} QLabel{color:#dce7f5;} QTableWidget{background:#0b1625;color:#dce7f5;gridline-color:#24364b;} QHeaderView::section{background:#122238;color:#dce7f5;padding:4px;border:0;} QStatusBar{background:#0d1928;color:#b8c7d9;}")
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        layout = QtWidgets.QHBoxLayout(central)
        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        layout.addWidget(splitter)

        left = QtWidgets.QWidget(); ll = QtWidgets.QVBoxLayout(left)
        self.info = QtWidgets.QLabel("等待 COM5 定位数据…")
        self.info.setStyleSheet("background:#0d1928;border:1px solid #31506d;border-radius:7px;padding:9px;font-size:14px;")
        ll.addWidget(self.info)
        self.map = pg.PlotWidget(background="#07101d")
        self.map.showGrid(x=True, y=True, alpha=0.2)
        self.map.setLabel("bottom", "X", units="m"); self.map.setLabel("left", "Y", units="m")
        self.map.setAspectLocked(True)
        ll.addWidget(self.map, 1)
        splitter.addWidget(left)

        right = QtWidgets.QWidget(); rl = QtWidgets.QVBoxLayout(right)
        self.summary = QtWidgets.QLabel()
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet("background:#0d1928;border:1px solid #24364b;border-radius:7px;padding:8px;font-size:11px;")
        rl.addWidget(self.summary)
        self.table = QtWidgets.QTableWidget(len(self.anchor_ids), 8)
        self.table.setHorizontalHeaderLabels(["Anchor", "距离 m", "RSSI", "CSI包", "权重可信度", "σ m", "权重", "残差 m"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.setMaximumHeight(235)
        rl.addWidget(self.table)
        self.dist_plot = pg.PlotWidget(background="#07101d", title="本轮/历史 FTM 距离")
        self.dist_plot.showGrid(x=True, y=True, alpha=0.18); self.dist_plot.setLabel("left", "Distance", units="m")
        rl.addWidget(self.dist_plot, 1)
        self.quality_plot = pg.PlotWidget(background="#07101d", title="当前权重可信度（越高越可信）")
        self.quality_plot.setYRange(0, 1.0); self.quality_plot.showGrid(x=True, y=True, alpha=0.18)
        rl.addWidget(self.quality_plot, 1)
        splitter.addWidget(right)
        splitter.setSizes([1050, 490])

        self.anchor_scatter = pg.ScatterPlotItem(size=15, symbol="t")
        pts = []
        for aid in self.anchor_ids:
            a = self.localizer.anchors[aid]
            pts.append({"pos": (a["x_m"], a["y_m"]), "brush": pg.mkBrush(self.COLORS.get(aid, "#ffffff")), "pen": pg.mkPen("#ffffff", width=1)})
            text = pg.TextItem(text=f"A{aid}", color=self.COLORS.get(aid, "#ffffff"), anchor=(0.5, 1.3))
            text.setPos(float(a["x_m"]), float(a["y_m"])); self.map.addItem(text)
        self.anchor_scatter.addPoints(pts); self.map.addItem(self.anchor_scatter)
        self.raw_curve = self.map.plot([], [], pen=pg.mkPen("#95a5a6", width=1), symbol=None)
        self.filtered_curve = self.map.plot([], [], pen=pg.mkPen("#f1c40f", width=2))
        self.raw_point = pg.ScatterPlotItem(size=9, brush=pg.mkBrush("#95a5a6")); self.map.addItem(self.raw_point)
        self.filtered_point = pg.ScatterPlotItem(size=15, brush=pg.mkBrush("#f1c40f"), pen=pg.mkPen("#ffffff", width=1)); self.map.addItem(self.filtered_point)
        self.range_circles = {}
        for aid in self.anchor_ids:
            c = self.map.plot([], [], pen=pg.mkPen(self.COLORS.get(aid, "#777777"), width=1, style=QtCore.Qt.PenStyle.DotLine))
            self.range_circles[aid] = c
        pad = float(self.config.get("view", {}).get("padding_m", 4.0))
        xs = [float(self.localizer.anchors[a]["x_m"]) for a in self.anchor_ids]
        ys = [float(self.localizer.anchors[a]["y_m"]) for a in self.anchor_ids]
        self.map.setXRange(min(xs)-pad, max(xs)+pad, padding=0)
        self.map.setYRange(min(ys)-pad, max(ys)+pad, padding=0)

        self.dist_curves = {}
        for aid in self.anchor_ids:
            self.dist_curves[aid] = self.dist_plot.plot([], [], pen=pg.mkPen(self.COLORS.get(aid, "#ffffff"), width=1.4), name=f"A{aid}")
        self.quality_bars = None
        self.statusBar().showMessage("启动中…")
        self._update_summary()

    def _update_summary(self) -> None:
        text = [f"Anchor 坐标（权重模式：{self.localizer.mode.upper()}）",
                "RTT提供距离；CSI/CIR仅用于动态加权。CIR模式要求真实CSI原始子载波数据。"]
        for aid in self.anchor_ids:
            a = self.localizer.anchors[aid]
            text.append(f"A{aid}: ({float(a['x_m']):.2f}, {float(a['y_m']):.2f}) m")
        self.summary.setText("\n".join(text))

    def _tick(self) -> None:
        processed = 0
        while processed < 500:
            try: line = self.messages.get_nowait()
            except queue.Empty: break
            processed += 1
            self._handle_line(line)
        if self.reader.error:
            self.statusBar().showMessage(f"{self.port} 连接失败：{self.reader.error}")
        elif self.reader.connected:
            self.statusBar().showMessage(f"{self.port} 已连接 · 模式 {self.localizer.mode} · MFUSED {self.parsed_count} · CSIRAW {self.cir_count}")
        else:
            self.statusBar().showMessage(f"正在连接 {self.port}…")

    def _handle_line(self, line: str) -> None:
        raw = parse_csiraw(line)
        if raw:
            self.cir_count += 1
            self.localizer.ingest_cir(raw)
            return
        rs = parse_round_start(line)
        if rs:
            self.localizer.start_epoch(*rs)
            return
        m = parse_mfused(line)
        if m:
            self.parsed_count += 1
            self.latest_measurement[m.anchor_id] = m
            self.localizer.ingest(m)
            return
        meta = parse_round_meta(line)
        if meta:
            rid, start, end, dur, measured = meta
            sol = self.localizer.finalize(rid, start, end, dur, measured)
            if sol:
                self._render_solution(sol)
            else:
                self.info.setText(f"Round {rid}：有效 Anchor 不足、CIR包不足或几何不可解；本轮不生成新位置")
            return

    def _render_solution(self, sol: dict[str, Any]) -> None:
        self.last_solution = sol
        raw, filt = sol["raw"], sol["filtered"]
        self.raw_traj.append(raw.copy()); self.filtered_traj.append(filt.copy())
        if self.raw_traj:
            a = np.asarray(self.raw_traj); self.raw_curve.setData(a[:,0], a[:,1]); self.raw_point.setData([raw[0]],[raw[1]])
        if self.filtered_traj:
            a = np.asarray(self.filtered_traj); self.filtered_curve.setData(a[:,0], a[:,1]); self.filtered_point.setData([filt[0]],[filt[1]])
        self.info.setText(
            f"移动 Target · Round {sol['round']}\n"
            f"Raw: X={raw[0]:.2f} m, Y={raw[1]:.2f} m    |    Filtered: X={filt[0]:.2f} m, Y={filt[1]:.2f} m\n"
            f"使用 {sol['anchor_count']} 个 Anchor · Inlier {sol['inlier_count']} · 残差 RMS={sol['rms']:.2f} m · Epoch={sol['duration_ms']:.1f} ms"
        )
        now = time.monotonic(); self.time_hist.append(now)
        row_by_id = {r["id"]: r for r in sol["rows"]}
        for aid in self.anchor_ids:
            r = row_by_id.get(aid); m = self.latest_measurement.get(aid)
            if r:
                self.distance_hist[aid].append(r["distance"])
                self.rssi_hist[aid].append(m.ftm_rssi if m else math.nan)
                a = self.localizer.anchors[aid]; theta = np.linspace(0, 2*math.pi, 100)
                d = r["distance"]
                self.range_circles[aid].setData(float(a["x_m"])+d*np.cos(theta), float(a["y_m"])+d*np.sin(theta))
            else:
                self.distance_hist[aid].append(math.nan); self.rssi_hist[aid].append(math.nan)
                self.range_circles[aid].setData([], [])
        t = np.arange(len(self.time_hist), dtype=float)
        for aid in self.anchor_ids:
            vals = np.asarray(self.distance_hist[aid], dtype=float)
            # histories can differ by one during startup; align to the right.
            self.dist_curves[aid].setData(np.arange(len(vals)), vals)
        self._update_table_and_quality(row_by_id)

    def _update_table_and_quality(self, row_by_id: dict[str, dict[str, Any]]) -> None:
        reliabilities, xs = [], []
        for i, aid in enumerate(self.anchor_ids):
            r = row_by_id.get(aid); m = self.latest_measurement.get(aid)
            if r and m:
                values = [f"A{aid}", f"{r['distance']:.2f}", f"{m.ftm_rssi:.0f}", str(m.csi_packets),
                          f"{r['csi_reliability']:.3f}", f"{r['csi_sigma']:.2f}",
                          f"{r['weight']:.3f}", f"{r['residual']:+.2f}"]
                reliabilities.append(r['csi_reliability']); xs.append(i+1)
            else:
                values = [f"A{aid}", "--", "--", "--", "--", "--", "0", "--"]
                reliabilities.append(0.0); xs.append(i+1)
            for j, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(value)
                if j == 0:
                    item.setForeground(QtGui.QColor(self.COLORS.get(aid, "#ffffff")))
                if r and j == 4 and r['csi_reliability'] < 0.25:
                    item.setBackground(QtGui.QColor("#7a2634"))
                self.table.setItem(i, j, item)
        self.quality_plot.clear()
        brushes = [pg.mkBrush(self.COLORS.get(aid, "#ffffff")) for aid in self.anchor_ids]
        bars = pg.BarGraphItem(x=np.asarray(xs), height=np.asarray(reliabilities), width=0.65, brushes=brushes)
        self.quality_plot.addItem(bars)
        axis = self.quality_plot.getAxis("bottom")
        axis.setTicks([[(i+1, f"A{aid}") for i, aid in enumerate(self.anchor_ids)]])

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        self.reader.stop(); self.reader.join(timeout=1.5)
        self.localizer.close()
        event.accept()



def main() -> int:
    parser = argparse.ArgumentParser(description="7 Anchor CSI-weighted ESP32-S3 localization dashboard")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--port", default=None)
    parser.add_argument("--mode", choices=["rtt", "csi", "cir", "hybrid"], default=None)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    if args.mode:
        config.setdefault("cir_weighting", {})["mode"] = args.mode
    port = args.port or str(config.get("serial_port", "COM5"))
    app = QtWidgets.QApplication(sys.argv)
    pg.setConfigOptions(antialias=True)
    win = Dashboard(config, port, args.output)
    win.show()
    return int(app.exec())


if __name__ == "__main__":
    raise SystemExit(main())
