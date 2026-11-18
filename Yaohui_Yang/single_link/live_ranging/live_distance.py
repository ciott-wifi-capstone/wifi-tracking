#!/usr/bin/env python3
"""Measure current distances from the TAG's live serial FTM/CSI stream.

No training, recording playback, or true-distance input is available here.
Stop after 120 seconds by default; the result window stays open for screenshots.
"""
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import sys
import threading
import time

import numpy as np
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'code'))
from cir_core import load_model, predict
from cir_io import SerialRoundAdapter

KINDS = ['ftm_affine', 'cir_direct', 'cir_residual']
NAMES = {'ftm_affine': 'FTM linear', 'cir_direct': 'CIR only', 'cir_residual': 'FTM + CIR'}
FIELDS = ['pc_utc', 'elapsed_s', 'epoch', 'round_id', 'tag_end_us', 'anchor_id', 'bssid', 'channel',
          'ftm_status', 'raw_ftm_m', 'paired_cir_frames', 'failure_reason']
for _kind in KINDS:
    FIELDS += [_kind + '_m', _kind + '_quality', _kind + '_usable_m']


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n', encoding='utf-8')


def models_from(path):
    return {kind: load_model(Path(path) / (kind + '.json')) for kind in KINDS}


def number(value):
    return '--' if value is None else f'{value:.3f}'


class StreamEngine:
    """One result per fully observed round; no labels enter this interface."""
    def __init__(self, models):
        self.models = models
        self.adapter = SerialRoundAdapter()
        self.ready = {'anchor_id': 'UNKNOWN', 'bssid': '', 'channel': ''}
        self.active = False
        self.link = {}
        self.sources = set()
        self.status = None

    def feed(self, line, elapsed_s, pc_utc=None):
        parts = line.strip().split(',')
        kind = parts[0]
        if kind == 'TARGET_READY':
            fields = dict(p.split('=', 1) for p in parts[1:] if '=' in p)
            for key in self.ready:
                if key in fields:self.ready[key] = fields[key].lower() if key == 'bssid' else fields[key]
        if kind == 'ROUND_START':
            self.adapter.feed(line)
            self.active = True
            self.rid = int(parts[1])
            self.link = self.ready.copy()
            self.sources = set()
            self.status = None
            return None
        if not self.active:
            return None  # Opening a port in the middle of a round must not create a full result.
        if kind == 'LINK_ID' and len(parts) == 6 and int(parts[1]) == self.rid:
            self.link = {'anchor_id': parts[3], 'bssid': parts[4].lower(), 'channel': parts[5]}
        if kind == 'FTM' and len(parts) == 9 and int(parts[1]) == self.rid:
            self.status = int(parts[3])
        if kind == 'CSI' and len(parts) == 13 and int(parts[1]) == self.rid:
            self.sources.add(parts[3].lower())
        if kind == 'CSI_FTM_FRAME' and len(parts) == 14 and int(parts[1]) == self.rid:
            self.sources.add(parts[5].lower())
        features = self.adapter.feed(line)
        if kind != 'ROUND_END' or len(parts) < 2 or int(parts[1]) != self.rid:
            return None
        self.active = False
        if not self.link.get('bssid') and len(self.sources) == 1:
            self.link['bssid'] = next(iter(self.sources))
        mismatch = len(self.sources) > 1 or bool(self.sources and self.link.get('bssid')
                       and self.sources != {self.link['bssid']})
        result = dict(pc_utc=pc_utc or utc_now(), elapsed_s=round(elapsed_s, 3), epoch=self.adapter.epoch,
                      round_id=self.rid, tag_end_us=parts[2] if len(parts) > 2 else '',
                      **self.link, ftm_status=self.status, raw_ftm_m=self.adapter.last_raw_ftm,
                      paired_cir_frames=features['paired_cir_frames'] if features else 0, failure_reason='')
        if mismatch:
            failure, reason = 'SOURCE_MISMATCH', 'CSI source differs from the announced AP BSSID.'
        elif self.status != 0:
            failure, reason = 'FTM_FAILED', f'FTM status={self.status}'
        elif features is None:
            failure, reason = 'CIR_UNAVAILABLE', self.adapter.last_error or 'No complete CIR features.'
        else:
            failure, reason = None, ''
        result['failure_reason'] = reason
        for model_kind, model in self.models.items():
            if failure:
                value = {'distance_m': None, 'quality': failure, 'distance_for_use_m': None}
            else:
                value = predict(model, features)
            result[model_kind + '_m'] = value['distance_m']
            result[model_kind + '_quality'] = value['quality']
            result[model_kind + '_usable_m'] = value['distance_for_use_m']
        return result


class Collector:
    def __init__(self, args, models, destination):
        self.args, self.models, self.destination = args, models, destination
        self.events = queue.Queue()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        import serial
        device = None
        started = time.monotonic()
        total, rounds, count = 0, 0, 0
        reason, error = 'stopped', None
        metadata = {'source_mode': 'LIVE_SERIAL', 'state': 'RUNNING', 'port': self.args.port,
                    'baud': self.args.baud, 'requested_seconds': self.args.seconds, 'started_utc': utc_now(),
                    'ground_truth_used_for_inference': False, 'fit_during_capture': False,
                    'model_hashes': {kind: hashlib.sha256((Path(self.args.models)/(kind+'.json')).read_bytes()).hexdigest()
                                     for kind in KINDS}}
        write_json(self.destination / 'measurement.json', metadata)
        try:
            device = serial.Serial(port=None, baudrate=self.args.baud, timeout=.2)
            device.dtr = False
            device.rts = False
            device.port = self.args.port
            device.open()
            started = time.monotonic()
            self.events.put(('status', 'LIVE: serial port open; waiting for a complete FTM round.'))
            engine, buffer = StreamEngine(self.models), b''
            last_status = started
            with (self.destination/'serial_raw.log').open('w', encoding='utf-8') as raw, \
                 (self.destination/'live_predictions.csv').open('w', encoding='utf-8-sig', newline='') as csv_file:
                writer = csv.DictWriter(csv_file, fieldnames=FIELDS)
                writer.writeheader()
                while not self.stop.is_set():
                    elapsed = time.monotonic() - started
                    if self.args.seconds > 0 and elapsed >= self.args.seconds:
                        reason = 'timer_complete'
                        break
                    chunk = device.read(min(8192, max(1, device.in_waiting)))
                    total += len(chunk)
                    buffer += chunk
                    while b'\n' in buffer:
                        encoded, buffer = buffer.split(b'\n', 1)
                        line = encoded.decode('utf-8', errors='replace').strip()
                        raw.write(line + '\n')
                        try:
                            result = engine.feed(line, time.monotonic() - started)
                        except (ValueError, KeyError, IndexError) as exc:
                            self.events.put(('status', 'Malformed serial record: ' + str(exc)))
                            continue
                        if result is not None:
                            writer.writerow(result)
                            csv_file.flush()
                            raw.flush()
                            rounds += 1
                            count += int(result['cir_direct_m'] is not None)
                            self.events.put(('result', result))
                            print(f"ROUND={result['round_id']} BSSID={result['bssid']} "
                                  f"RAW_FTM={number(result['raw_ftm_m'])}m "
                                  f"CIR={number(result['cir_direct_m'])}m [{result['cir_direct_quality']}] "
                                  f"FTM+CIR={number(result['cir_residual_m'])}m [{result['cir_residual_quality']}]", flush=True)
                        if 'waiting for AP' in line:
                            self.events.put(('status', 'TAG is waiting for its Anchor: check power and matching SSID.'))
                    if len(buffer) > 65536:
                        buffer = b''
                        self.events.put(('status', 'Discarded an oversized unterminated serial record.'))
                    if time.monotonic() - last_status >= 1:
                        self.events.put(('traffic', {'bytes': total, 'elapsed_s': time.monotonic() - started}))
                        last_status = time.monotonic()
                if buffer:
                    raw.write(buffer.decode('utf-8', errors='replace'))
                raw.flush()
        except (serial.SerialException, OSError) as exc:
            reason, error = 'serial_error', str(exc)
            self.events.put(('error', f'{exc} | Close IDF monitor/other programs using the same COM port.'))
            print('SERIAL ERROR:', error, flush=True)
        except Exception as exc:
            reason, error = 'processing_error', str(exc)
            self.events.put(('error', str(exc)))
            print('PROCESSING ERROR:', error, flush=True)
        finally:
            if device is not None and device.is_open:device.close()
            metadata.update(state='ERROR' if error else 'FINISHED', ended_utc=utc_now(),
                            elapsed_s=round(time.monotonic()-started, 3), completed_rounds=rounds,
                            cir_prediction_rounds=count, received_bytes=total, stop_reason=reason, error=error)
            write_json(self.destination/'measurement.json', metadata)
            self.events.put(('finished', metadata))
            print('CAPTURE FINISHED:', self.destination.resolve(), flush=True)


class LiveView:
    def __init__(self, collector, source_label='LIVE SERIAL'):
        import matplotlib.pyplot as plt
        from matplotlib.widgets import Button, RadioButtons
        self.plt, self.collector = plt, collector
        self.model = collector.args.model
        self.rows, self.phase, self.message = [], 'WAITING', 'Waiting for a complete FTM + CIR round.'
        self.last_arrival, self.elapsed, self.received_bytes = None, 0., 0
        self.source_label = source_label
        self.fig = plt.figure(figsize=(14, 8))
        self.fig.canvas.manager.set_window_title('Live CIR distance measurement')
        self.fig.text(.04, .955, f'{source_label} | Current single-link distance', fontsize=17, weight='bold')
        self.fig.text(.04, .915, f'Port: {collector.args.port} | Baud: {collector.args.baud} | Frozen models | No truth input', fontsize=10)
        self.big = self.fig.text(.05, .75, '-- m', fontsize=48, weight='bold', color='#176D89')
        self.detail = self.fig.text(.05, .70, '', fontsize=10, va='top')
        self.source = self.fig.text(.05, .64, '', fontsize=9, va='top')
        self.status = self.fig.text(.04, .10, self.message, fontsize=10)
        self.counts = self.fig.text(.04, .06, '', fontsize=9)
        self.fig.text(.04, .025, 'EXPERIMENTAL: out-of-domain/negative results remain visible. '
                      'Missing CIR is shown as --, without replacing it by raw FTM.', fontsize=8.5)
        self.trace = self.fig.add_axes([.41, .45, .55, .40])
        self.table_ax = self.fig.add_axes([.41, .20, .55, .17])
        self.radio = RadioButtons(self.fig.add_axes([.05, .39, .27, .17]), [NAMES[k] for k in KINDS],
                                 active=KINDS.index(self.model))
        self.radio.on_clicked(self.change_model)
        self.stop_button = Button(self.fig.add_axes([.05, .27, .27, .06]), 'Stop capture; keep window')
        self.save_button = Button(self.fig.add_axes([.05, .18, .27, .06]), 'Save current view (PNG)')
        self.stop_button.on_clicked(lambda _: self.request_stop())
        self.save_button.on_clicked(lambda _: self.save())
        self.timer = self.fig.canvas.new_timer(interval=200)
        self.timer.add_callback(self.tick)
        self.redraw()

    def change_model(self, label):
        self.model = next(k for k in KINDS if NAMES[k] == label)
        self.redraw()

    def request_stop(self):
        self.collector.stop.set()
        self.message = 'Stopping capture. The window will stay open for screenshots.'
        self.redraw()

    def tick(self):
        while True:
            try:kind, value = self.collector.events.get_nowait()
            except queue.Empty:break
            if kind == 'result':
                self.rows.append(value)
                self.last_arrival = time.monotonic()
                self.phase = 'LIVE'
                self.message = 'New measurement from the serial port.'
            elif kind == 'traffic':
                self.elapsed, self.received_bytes = value['elapsed_s'], value['bytes']
            elif kind == 'error':
                self.phase, self.message = 'ERROR', value
            elif kind == 'status':
                self.message = value
            elif kind == 'finished':
                self.phase = 'ERROR' if value['error'] else 'FINISHED'
                self.elapsed, self.received_bytes = value['elapsed_s'], value['received_bytes']
                if value['error']:
                    self.message = value['error']
                else:
                    self.message = f"Capture finished ({value['stop_reason']}); showing the last captured result."
        self.redraw()

    def redraw(self):
        row = self.rows[-1] if self.rows else None
        age = None if self.last_arrival is None else time.monotonic() - self.last_arrival
        stale = self.phase == 'LIVE' and age is not None and age > 12
        value = row[self.model+'_m'] if row and self.phase != 'ERROR' and not stale else None
        quality = row[self.model+'_quality'] if row else 'WAITING'
        if stale:quality = 'STALE: no new result for >12 seconds'
        if self.phase == 'ERROR':quality = 'SERIAL/PROCESSING ERROR'
        self.big.set_text(f'{number(value)} m')
        self.big.set_color('#176D89' if quality == 'EXPERIMENTAL' else '#B76513')
        self.detail.set_text(f'{NAMES[self.model]} | {self.phase}\n{quality}')
        if row:
            self.source.set_text(f"Anchor: {row['anchor_id']}\nAP BSSID: {row['bssid'] or 'unknown'}\n"
                                 f"Round: {row['round_id']} | Epoch: {row['epoch']} | CIR pairs: {row['paired_cir_frames']}\n"
                                 f"Captured UTC: {row['pc_utc']}")
        else:
            self.source.set_text('No complete FTM round received yet.\nKeep TAG and the matching Anchor powered.')
        ax = self.trace
        ax.clear()
        if self.rows:
            x = [r['elapsed_s'] for r in self.rows]
            raw = [np.nan if r['raw_ftm_m'] is None else r['raw_ftm_m'] for r in self.rows]
            fitted = [np.nan if r[self.model+'_m'] is None else r[self.model+'_m'] for r in self.rows]
            ax.plot(x, raw, '.-', color='#778695', linewidth=1, markersize=3, label='Raw FTM')
            ax.plot(x, fitted, '.-', color='#7653A4', linewidth=1.3, markersize=4, label=NAMES[self.model])
            ax.legend(fontsize=9)
        ax.set(xlabel='PC elapsed time (s)', ylabel='Distance (m)', title='Measurements received during this capture')
        ax.grid(alpha=.2)
        ax = self.table_ax
        ax.clear()
        ax.axis('off')
        cells = [['Raw FTM', number(row['raw_ftm_m']) if row else '--',
                  'RAW' if row and row['ftm_status'] == 0 else 'NO SUCCESSFUL FTM']]
        cells += [[NAMES[k], number(row[k+'_m']) if row else '--', row[k+'_quality'] if row else 'WAITING'] for k in KINDS]
        table = ax.table(cellText=cells, colLabels=['Method', 'Last range (m)', 'Quality'],
                         colWidths=[.27, .22, .51], cellLoc='center', bbox=[0, 0, 1, 1])
        table.auto_set_font_size(False)
        table.set_fontsize(8.5)
        for (i, j), cell in table.get_celld().items():
            cell.set_edgecolor('#D3DBE5')
            if i == 0:cell.set_facecolor('#E7EEF5')
        ax.set_title('Last completed round (historical value after stop/error)', fontsize=9, pad=8)
        self.status.set_text(self.message[:175])
        self.counts.set_text(f'Completed rounds: {len(self.rows)} | Received bytes: {self.received_bytes} | '
                             f'Elapsed: {self.elapsed:.1f}s | Output: {self.collector.destination.name}')
        self.fig.canvas.draw_idle()

    def save(self, filename=None):
        self.tick()
        filename = filename or f'snapshot_{self.model}_{datetime.now().strftime("%H%M%S_%f")}.png'
        path = self.collector.destination / filename
        self.fig.savefig(path, dpi=220, bbox_inches='tight')
        print('PNG SAVED:', path.resolve(), flush=True)
        return path


def collect_console(collector):
    collector.thread.start()
    try:
        while collector.thread.is_alive():collector.thread.join(.2)
    except KeyboardInterrupt:
        collector.stop.set()
        collector.thread.join(5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', default='COM11')
    parser.add_argument('--baud', type=int, default=115200)
    parser.add_argument('--seconds', type=float, default=120, help='0=until stopped; timer stops collection, keeps window open')
    parser.add_argument('--models', default=str(ROOT/'models'))
    parser.add_argument('--model', choices=KINDS, default='cir_direct', help='Initial display; all methods are always computed')
    parser.add_argument('--out', default=str(ROOT/'measurements'))
    parser.add_argument('--no-window', action='store_true', help='Collect to CSV and print ranges without a GUI')
    args = parser.parse_args()
    if not math.isfinite(args.seconds) or args.seconds < 0:parser.error('--seconds must be nonnegative.')
    try:import serial
    except ImportError:raise SystemExit('Install dependencies: python -m pip install -r requirements.txt')
    if Path(args.models).resolve() == (ROOT/'models').resolve():
        expected = json.loads((ROOT/'frozen_model_info.json').read_text())['model_hashes']
        for kind, digest in expected.items():
            actual = hashlib.sha256((ROOT/'models'/(kind+'.json')).read_bytes()).hexdigest()
            if actual != digest:raise SystemExit('Frozen model changed: '+kind+'; restore the supplied models.')
    models = models_from(args.models)
    destination = Path(args.out)/('live_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
    destination.mkdir(parents=True, exist_ok=False)
    print('LIVE serial measurement:', args.port, '| No true-distance input. Frozen models; no fitting during capture.')
    print('OUTPUT:', destination.resolve(), flush=True)
    print('Timer:', args.seconds, 'seconds. Close IDF monitor before opening this program.', flush=True)
    collector = Collector(args, models, destination)
    if args.no_window:
        collect_console(collector)
        return
    import matplotlib
    if os.name == 'nt':matplotlib.use('TkAgg')
    import matplotlib.pyplot as plt
    try:
        view = LiveView(collector)
    except Exception as exc:
        print('Cannot open GUI:', exc)
        print('Continuing LIVE measurement in the terminal. Use Windows Python with Tkinter for a graphical window.')
        plt.close('all')
        collect_console(collector)
        return
    collector.thread.start()
    view.timer.start()
    try:
        plt.show()
    except KeyboardInterrupt:
        pass
    finally:
        collector.stop.set()
        collector.thread.join(5)
        view.timer.stop()
        view.save('final_view.png')


if __name__ == '__main__':
    main()
