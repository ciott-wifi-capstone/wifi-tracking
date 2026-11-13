#!/usr/bin/env python3
"""Live six-anchor 2D map and surveyed static-point collection. Run on the TAG PC."""
import argparse
from collections import deque
from datetime import datetime
import hashlib
import math
from pathlib import Path
import queue
import sys
import time

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT/'code'))
from serial_engine import models_from
from acquisition import Recorder
from positioning import METHODS, load_config, load_calibration, model_hashes


class View:
    def __init__(self, recorder, layout_only=False):
        import matplotlib.pyplot as plt
        from matplotlib.widgets import Button, TextBox, RadioButtons
        self.plt, self.recorder, self.layout_only = plt, recorder, layout_only
        self.config = recorder.config
        self.rows, self.arrivals, self.points = {}, {}, {}
        self.trace = deque(maxlen=1200)
        self.latest, self.last_wls, self.position_arrival = None, None, None
        self.phase = 'LAYOUT ONLY' if layout_only else 'WAITING'
        self.message = 'Survey actual TAG antenna x/y. Truth labels never enter positioning.'
        self.point_message = 'No active point. Move, stop, enter measured coordinates, then collect.'
        self.stats = dict(bytes=0, elapsed_s=0., scans=0, ranges=0)
        self.last_draw = 0
        self.fig = plt.figure(figsize=(16, 9))
        self.fig.canvas.manager.set_window_title('Six-anchor FTM + CIR | 2D positioning and ground truth')
        self.fig.text(.04, .953, 'Six-anchor 2D positioning | '+self.phase, fontsize=18, weight='bold')
        self.header = self.fig.text(.04, .912, '', fontsize=10)
        self.position_text = self.fig.text(.65, .827, 'TAG: waiting for ranges', fontsize=17, weight='bold', color='#156789')
        self.position_detail = self.fig.text(.65, .778, '', fontsize=10, va='top')
        self.map_ax = self.fig.add_axes([.055, .36, .555, .50])
        self.table_ax = self.fig.add_axes([.65, .42, .32, .23])
        self.status = self.fig.text(.045, .326, '', fontsize=10)
        self.point_status = self.fig.text(.045, .287, '', fontsize=10, color='#9B5310')
        self.fig.text(.045, .242, 'STATIC POINT: keep the TAG antenna still for every requested complete scan.', fontsize=10, weight='bold')
        self.id_box = TextBox(self.fig.add_axes([.09, .182, .10, .035]), 'Point ID ', initial='K1')
        self.x_box = TextBox(self.fig.add_axes([.24, .182, .09, .035]), 'X (m) ', initial='')
        self.y_box = TextBox(self.fig.add_axes([.38, .182, .09, .035]), 'Y (m) ', initial='')
        self.scan_box = TextBox(self.fig.add_axes([.535, .182, .055, .035]), 'Scans ', initial='5')
        self.purpose = RadioButtons(self.fig.add_axes([.64, .155, .17, .09]), ['CALIBRATION', 'TEST'], active=0)
        self.buttons = []
        for bounds, label, callback in [
            ([.045, .10, .17, .046], 'Collect static point', self.collect),
            ([.23, .10, .13, .046], 'Cancel point', lambda _: recorder.commands.put(('cancel', None))),
            ([.375, .10, .13, .046], 'Rescan anchors', lambda _: recorder.commands.put(('rescan', None))),
            ([.52, .10, .13, .046], 'Save PNG', self.save),
            ([.67, .10, .20, .046], 'Stop; keep this window', self.stop)]:
            b = Button(self.fig.add_axes(bounds), label); b.on_clicked(callback); self.buttons.append(b)
        self.fig.text(.045, .06, 'CIR inference uses frozen features/models. Calibration uses separate CALIBRATION labels only. '
                      'WLS assumes a static scan; EKF uses sequential session times.', fontsize=9)
        self.fig.text(.045, .032, 'EXPERIMENTAL | Survey independent test points before reporting accuracy. '
                      'Walking segments need external time-aligned ground truth.', fontsize=9, color='#9B5310')
        self.fig.canvas.mpl_connect('close_event', lambda _: recorder.stop.set())
        self.timer = self.fig.canvas.new_timer(interval=300)
        self.timer.add_callback(self.tick)
        self.redraw()

    def collect(self, _):
        try:
            if self.layout_only or self.recorder.stop.is_set(): raise ValueError('No active live serial capture.')
            pid = self.id_box.text.strip()
            x, y = float(self.x_box.text), float(self.y_box.text)
            scans = int(self.scan_box.text)
            if not pid or not all(math.isfinite(v) for v in [x, y]) or not 1 <= scans <= 200:
                raise ValueError('Enter a point ID, finite X/Y, and 1-200 scans.')
            self.recorder.request_point(pid, x, y, self.purpose.value_selected, scans)
            self.point_message = 'Request queued; remain still until COMPLETE. Do not walk yet.'
        except (ValueError, TypeError) as exc:
            self.point_message = str(exc)
        self.redraw()

    def stop(self, _):
        self.recorder.stop.set()
        self.message = 'Stopping serial capture; the result window stays open.'
        self.redraw()

    def save(self, _):
        name = self.recorder.directory/('map_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'.png')
        self.fig.savefig(name, dpi=180)
        self.message = 'Saved '+str(name.resolve())
        self.redraw()

    def tick(self):
        while True:
            try: kind, data = self.recorder.events.get_nowait()
            except queue.Empty: break
            if kind == 'range':
                self.rows[data['anchor_id']] = data
                self.arrivals[data['anchor_id']] = time.monotonic()
                self.phase = 'LIVE'
            elif kind == 'position':
                if data['kind'] == 'WLS_SCAN': self.last_wls = data
                elif data.get('valid'):
                    self.latest = data; self.position_arrival = time.monotonic()
                    self.trace.append((data['x'], data['y']))
            elif kind == 'scan':
                print(f"SCAN={data['scan_id']} duration={data['scan_duration_s']:.2f}s "
                      f"anchors={data['anchors']}/6 WLS={data['reason']}", flush=True)
            elif kind == 'boot':
                self.latest, self.last_wls, self.position_arrival = None, None, None
                self.rows.clear(); self.arrivals.clear(); self.trace.clear()
                self.message = 'TAG boot detected. Waiting for a fresh position.'
            elif kind == 'point':
                self.points[data['token']] = data
                n = len(data.get('observed_scans', []))
                self.point_message = f"{data['point_id']} | {data['purpose']} | {data['status']} | {n}/{data['requested_scans']} scans"
                if data['status'] == 'COMPLETE': self.point_message += ' | You may move now.'
                if data['status'] == 'INCOMPLETE': self.point_message += ' | Missing records: repeat with a new point ID.'
                print('POINT:', self.point_message, flush=True)
            elif kind == 'traffic': self.stats = data
            elif kind in ('status', 'error'): self.message = data
            elif kind == 'finished':
                self.phase = 'ERROR' if data['error'] else 'FINISHED'
                self.message = (data['error'] or 'Capture saved. Window retained for screenshots.')
        self.redraw()

    def redraw(self):
        now = time.monotonic()
        calibration = 'per-anchor calibration loaded' if self.recorder.calibration else 'UNCALIBRATED board transfer'
        policy = 'OOD allowed with low weight' if self.config.get('allow_ood') else 'OOD excluded'
        self.header.set_text(f"{self.phase} | {self.recorder.args.port} / {self.recorder.args.baud} | "
                             f"{self.recorder.args.method} | {calibration} | {policy}")
        ax = self.map_ax; ax.clear()
        xx, yy = [], []
        for aid, a in self.config['anchors'].items():
            ax.scatter(a['x'], a['y'], marker='^', s=100, color='#156789', zorder=4)
            ax.annotate(f"{aid} ({a['x']:.2f}, {a['y']:.2f})", (a['x'], a['y']), xytext=(5, 9),
                        textcoords='offset points', fontsize=8.5)
            xx.append(a['x']); yy.append(a['y'])
        if self.trace:
            tx, ty = zip(*self.trace); ax.plot(tx, ty, '-', color='#2C9A88', alpha=.55, linewidth=1, label='Range EKF trace')
            xx.extend(tx); yy.extend(ty)
        for point in self.points.values():
            if point['status'] != 'COMPLETE': continue
            ax.scatter(point['x'], point['y'], marker='+', color='#CA6D1C', s=80, zorder=4)
            ax.annotate(point['point_id'], (point['x'], point['y']), xytext=(4, -12), textcoords='offset points', fontsize=8)
        age = None if self.position_arrival is None else now-self.position_arrival+self.latest.get('rf_to_round_end_s', 0.)
        if self.latest:
            s = self.latest
            stale = age > self.config['filter_reset_gap_s'] or self.phase in ('ERROR', 'FINISHED')
            ax.scatter(s['x'], s['y'], s=110, c='#8A8A8A' if stale else '#C13B55', zorder=5, label='TAG estimate')
            self.position_text.set_text(f"TAG: ({s['x']:.2f}, {s['y']:.2f}) m")
            self.position_detail.set_text(f"{'LAST / STALE' if stale else 'EKF at latest range time'} | age >= {age:.1f} s\n"
                                          f"Model sigma: {s['position_sigma_m']:.2f} m (heuristic)\n{s['reason']}")
        else:
            self.position_text.set_text('TAG: waiting for >=4 anchors')
            self.position_detail.set_text('Keep TAG still for initialization.\nCheck per-anchor quality below.')
        if self.last_wls and self.last_wls.get('valid'):
            w = self.last_wls
            ax.scatter(w['x'], w['y'], marker='x', s=85, c='#7652A4', label='Static scan WLS')
        if self.trace or self.latest: ax.legend(loc='lower left', fontsize=8)
        ax.set(xlabel='X (m)', ylabel='Y (m)', title=f"Surveyed anchor coordinates | TAG z={self.config['tag_height_m']:.2f} m")
        ax.set_aspect('equal', adjustable='box'); ax.grid(alpha=.23)
        ax.set_xlim(min(xx)-3, max(xx)+4); ax.set_ylim(min(yy)-3, max(yy)+4)
        table = []
        for aid in self.config['anchors']:
            row = self.rows.get(aid)
            if row:
                v = row['corrected_m']
                d = '--' if v is None else f'{v:.2f}'
                quality = 'OK' if row['range_usable'] else row['exclusion']
                if row['range_quality'] == 'OUT_OF_DOMAIN' and row['range_usable']: quality = 'OOD / low weight'
                radio_age = (row.get('rf_to_round_end_s') or 0.)+now-self.arrivals[aid]
                table.append([aid, d, f"{radio_age:.1f}", quality[:23]])
            else: table.append([aid, '--', '--', 'WAITING'])
        self.table_ax.clear(); self.table_ax.axis('off')
        t = self.table_ax.table(cellText=table, colLabels=['Anchor', 'Range m', 'Age s', 'Quality'],
                                colWidths=[.14, .19, .15, .52], cellLoc='center', loc='center')
        t.auto_set_font_size(False); t.set_fontsize(8); t.scale(1., 1.5)
        duration = '--' if self.last_wls is None else f"{self.last_wls['scan_duration_s']:.2f} s"
        self.status.set_text(f"Scan time: {duration} | Full scans: {self.stats['scans']} | "
                             f"RX {self.stats['bytes']} bytes | {self.message[:150]}")
        self.point_status.set_text(self.point_message)
        self.fig.canvas.draw_idle()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--port', default='COM11')
    p.add_argument('--baud', type=int, default=921600)
    p.add_argument('--config', type=Path, default=ROOT/'anchors.json')
    p.add_argument('--models', type=Path, default=ROOT/'models')
    p.add_argument('--calibration', type=Path)
    p.add_argument('--method', choices=METHODS, default='cir_residual')
    p.add_argument('--output', type=Path, required=True, help='New session directory; existing nonempty paths are refused.')
    policy = p.add_mutually_exclusive_group()
    policy.add_argument('--allow-ood', action='store_true')
    policy.add_argument('--reject-ood', action='store_true')
    p.add_argument('--show-layout', action='store_true', help='Show real configured anchors only, without serial measurements.')
    args = p.parse_args()
    if args.output.exists() and any(args.output.iterdir()): p.error('Output folder is not empty. Use a new name.')
    config = load_config(args.config)
    if args.allow_ood: config['allow_ood'] = True
    if args.reject_ood: config['allow_ood'] = False
    models = models_from(args.models)
    calibration = load_calibration(args.calibration, config, model_hashes(args.models), args.method)
    args.calibration_sha256 = hashlib.sha256(args.calibration.read_bytes()).hexdigest() if args.calibration else None
    args.output.mkdir(parents=True, exist_ok=True)
    recorder = Recorder(args, config, models, calibration, args.output)
    view = View(recorder, args.show_layout)
    if not args.show_layout: recorder.thread.start()
    view.timer.start()
    print('Session:', args.output.resolve(), flush=True)
    view.plt.show()
    recorder.stop.set()
    if recorder.thread.is_alive(): recorder.thread.join(timeout=5)


if __name__ == '__main__':
    main()
