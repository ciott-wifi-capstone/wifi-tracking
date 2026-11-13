"""Six-anchor serial protocol, durable capture, and inference separated from labels."""
import json
import math
from pathlib import Path
import queue
import threading
import time
import uuid
from datetime import datetime, timezone

from serial_engine import StreamEngine
from positioning import select_range, solve_wls, RangeTracker, geometry_id, model_hashes, model_training_points
from cir_core import SCHEMA


def utc():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def write_json(path, data):
    path = Path(path)
    temp = path.with_suffix(path.suffix+'.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    temp.replace(path)


class Processor:
    """No point coordinates enter this class. Token strings only join later evaluation."""
    def __init__(self, models, config, method, calibration, emit):
        self.models, self.config, self.method, self.calibration, self.emit = models, config, method, calibration, emit
        self.engine = StreamEngine(models)
        self.tracker = RangeTracker(config)
        self.boot, self.scan, self.token = None, None, 'none'
        self.full_scan = False
        self.profile = None
        self.rows, self.times, self.drops, self.unavailable = [], {}, {}, {}

    def reset_boot(self, boot):
        if boot != self.boot:
            old = self.boot
            self.boot = boot
            self.engine = StreamEngine(self.models)
            self.tracker.reset()
            self.full_scan = False
            self.emit('boot', {'old_boot': old, 'boot': boot})

    def feed(self, line, elapsed_s):
        p = line.strip().split(',')
        if p[0] == 'MULTI_BOOT' and len(p) >= 2:
            self.reset_boot(p[1])
            self.emit('status', 'TAG boot: '+line.strip())
            return
        if p[0] == 'SCAN_START' and len(p) == 5:
            self.reset_boot(p[1])
            self.scan, self.scan_start_us, self.token = int(p[2]), int(p[3]), p[4]
            self.profile = None
            self.rows, self.times, self.drops, self.unavailable = [], {}, {}, {}
            self.full_scan = True
            return
        if p[0] in ('MARK_ACK', 'MARK_REJECTED', 'POINT_DONE', 'POINT_CANCELLED'):
            self.emit('protocol', p)
            return
        if p[0] in ('COMMAND_ERROR', 'SCAN_ERROR', 'DISCOVER'):
            self.emit('status', line.strip())
        if p[0] == 'PROFILE' and len(p) == 5:
            self.profile = ':'.join(p[1:])
            return
        if not self.full_scan:
            return
        if p[0] == 'SCAN_END' and len(p) == 5:
            if p[1] != self.boot or int(p[2]) != self.scan or p[4] != self.token:
                self.full_scan = False
                self.emit('status', 'Scan boundary mismatch; incomplete scan excluded.')
                return
            complete = len(self.rows) == 6 and {r['anchor_id'] for r in self.rows} == set(self.config['anchors'])
            obs = [r for r in self.rows if r['range_usable']]
            solution = solve_wls(obs, self.config) if complete else dict(valid=False, reason='INCOMPLETE_SERIAL_SCAN', anchors=len(obs))
            solution.update(kind='WLS_SCAN', boot=self.boot, scan_id=self.scan, token=self.token,
                            pc_utc=utc(), method=self.method, full_scan=complete,
                            scan_start_us=self.scan_start_us, scan_end_us=int(p[3]),
                            scan_duration_s=(int(p[3])-self.scan_start_us)/1e6,
                            rf_time_s=max([r['rf_time_s'] for r in obs], default=int(p[3])/1e6),
                            rf_span_s=(max(r['rf_time_s'] for r in obs)-min(r['rf_time_s'] for r in obs)) if obs else None,
                            note='Sequential scan: static-point WLS; moving estimates use individual-time EKF.')
            self.emit('position', solution)
            self.emit('scan', solution)
            self.full_scan = False
            return
        if p[0] == 'LINK_TIME' and len(p) == 5:
            self.times[int(p[1])] = [int(p[2]), int(p[3]), int(p[4])]
        elif p[0] == 'PROBE_SUMMARY' and len(p) == 6:
            self.drops[int(p[1])] = int(p[5])
        elif p[0] == 'LINK_UNAVAILABLE' and len(p) == 4:
            self.unavailable[int(p[1])] = p[3]
        row = self.engine.feed(line, elapsed_s)
        if row is None: return
        row.update(boot=self.boot, scan_id=self.scan, token=self.token, firmware_profile=self.profile,
                   features=self.engine.last_features, csi_dropped=self.drops.get(row['round_id'], 0),
                   unavailable_reason=self.unavailable.get(row['round_id'], ''))
        timing = self.times.get(row['round_id'])
        row['rf_time_s'] = (timing[0]+timing[1])/2e6 if timing else None
        row['rf_session_start_us'] = timing[0] if timing else None
        row['rf_report_us'] = timing[1] if timing else None
        row['rf_to_round_end_s'] = max(0., float(row['tag_end_us'])/1e6-row['rf_time_s']) if timing else None
        row['time_source'] = 'TAG_FTM_SESSION_MIDPOINT' if timing else 'MISSING'
        selected = select_range(row, self.method, self.config, self.calibration)
        if timing is None:
            selected.update(range_usable=False, exclusion='MISSING_RF_TIME')
        aid = row['anchor_id']
        if aid not in self.config['anchors']:
            selected.update(range_usable=False, exclusion='UNKNOWN_ANCHOR_ID')
        else:
            expected = self.config['anchors'][aid].get('expected_bssid')
            if expected and expected.lower() != row['bssid']:
                selected.update(range_usable=False, exclusion='CONFIG_BSSID_MISMATCH')
        row.update(selected)
        self.rows.append(row)
        self.emit('range', row)
        if row['range_usable']:
            position = self.tracker.update(row)
            if position:
                position.update(boot=self.boot, scan_id=self.scan, token=self.token, pc_utc=utc(),
                                round_id=row['round_id'], anchor_id=aid, method=self.method,
                                measurement_quality=row['range_quality'],
                                rf_to_round_end_s=row['rf_to_round_end_s'])
                self.emit('position', position)


class Recorder:
    def __init__(self, args, config, models, calibration, directory):
        self.args, self.config, self.models, self.calibration = args, config, models, calibration
        self.directory = Path(directory)
        self.events, self.commands = queue.Queue(), queue.Queue()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.points, self.active_token, self.observed_scans = {}, None, {}
        self.started, self.range_count, self.scan_count, self.bytes_count = None, 0, 0, 0

    def request_point(self, point_id, x, y, purpose, scans):
        # This is metadata only; only the random token and scan count reach TAG.
        token = 'p'+uuid.uuid4().hex[:20]
        point = dict(token=token, point_id=str(point_id), x=float(x), y=float(y),
                     z=float(self.config['tag_height_m']), purpose=purpose,
                     requested_scans=int(scans), status='PENDING', created_utc=utc())
        self.commands.put(('point', point))
        return token

    def emit(self, kind, data):
        if kind == 'boot' and data['old_boot'] is not None:
            self.abort_point('TAG_REBOOT')
        if kind == 'range':
            self.range_log.write(json.dumps(data, ensure_ascii=False, allow_nan=False)+'\n')
            self.range_log.flush(); self.range_count += 1
        elif kind == 'position':
            self.position_log.write(json.dumps(data, ensure_ascii=False, allow_nan=False)+'\n')
            self.position_log.flush()
        elif kind == 'scan':
            self.scan_count += 1
            if data['token'] in self.points and data['full_scan']:
                self.observed_scans.setdefault(data['token'], []).append([data['boot'], data['scan_id']])
                point = self.points[data['token']]
                point['observed_scans'] = self.observed_scans[data['token']]
                write_json(self.directory/'points.json', self.points)
                self.events.put(('point', point.copy()))
        elif kind == 'protocol':
            p = data
            if p[0] == 'MARK_ACK' and len(p) == 4 and p[2] in self.points:
                if self.points[p[2]]['status'] != 'ABORTED':
                    self.points[p[2]].update(status='ACTIVE', boot=p[1], started_utc=utc())
            elif p[0] == 'POINT_DONE' and len(p) == 5 and p[2] in self.points:
                point = self.points[p[2]]
                count = len(self.observed_scans.get(p[2], []))
                complete = count == point['requested_scans'] == int(p[3])
                if point['status'] != 'ABORTED':
                    point.update(status='COMPLETE' if complete else 'INCOMPLETE', ended_utc=utc(),
                                 firmware_completed_scans=int(p[3]))
                if self.active_token == p[2]: self.active_token = None
            elif p[0] == 'POINT_CANCELLED' and len(p) >= 3 and p[2] in self.points:
                self.points[p[2]].update(status='ABORTED', reason='USER_CANCELLED', ended_utc=utc())
                if self.active_token == p[2]: self.active_token = None
            elif p[0] == 'MARK_REJECTED' and len(p) >= 2 and p[1] in self.points:
                self.points[p[1]].update(status='REJECTED', reason=','.join(p[2:]))
                if self.active_token == p[1]: self.active_token = None
            for point in self.points.values():
                if point['token'] in p: self.events.put(('point', point.copy()))
            write_json(self.directory/'points.json', self.points)
        self.events.put((kind, data))

    def abort_point(self, reason):
        for point in self.points.values():
            if point['status'] in ('ACTIVE', 'PENDING'):
                point.update(status='ABORTED', reason=reason, ended_utc=utc())
                self.events.put(('point', point.copy()))
        self.active_token = None
        write_json(self.directory/'points.json', self.points)

    def run(self):
        import serial
        device = None
        self.started = time.monotonic()
        hashes = model_hashes(self.args.models)
        metadata = dict(schema_version=1, source='LIVE_SERIAL', state='RUNNING', started_utc=utc(),
                        port=self.args.port, baud=self.args.baud, selected_method=self.args.method,
                        config=self.config, geometry_sha256=geometry_id(self.config), model_hashes=hashes,
                        calibration_sha256=self.args.calibration_sha256,
                        cir_feature_schema=SCHEMA,
                        model_training_points=model_training_points(self.args.models),
                        calibration_training_points=self.calibration.get('training_points', []) if self.calibration else [],
                        truth_used_for_inference=False,
                        rf_time='TAG boot-relative FTM session midpoint, approximate; not optical motion ground truth')
        write_json(self.directory/'session.json', metadata)
        write_json(self.directory/'points.json', self.points)
        error = None
        try:
            device = serial.Serial(port=None, baudrate=self.args.baud, timeout=.1, write_timeout=1)
            device.dtr = False; device.rts = False
            device.port = self.args.port; device.open()
            self.events.put(('status', 'LIVE serial opened. Waiting for full SCAN_START...SCAN_END.'))
            processor = Processor(self.models, self.config, self.args.method, self.calibration, self.emit)
            buffer = b''; last_traffic = self.started
            with (self.directory/'serial_raw.log').open('wb') as raw, \
                 (self.directory/'ranges.jsonl').open('w', encoding='utf-8') as self.range_log, \
                 (self.directory/'positions.jsonl').open('w', encoding='utf-8') as self.position_log, \
                 (self.directory/'host_commands.jsonl').open('w', encoding='utf-8') as commands:
                while not self.stop.is_set():
                    while True:
                        try: kind, data = self.commands.get_nowait()
                        except queue.Empty: break
                        command = None
                        if kind == 'point':
                            if self.active_token:
                                self.events.put(('error', 'A point is pending/active. Wait for COMPLETE or cancel it.'))
                                continue
                            if any(p['point_id'] == data['point_id'] and p['status'] == 'COMPLETE' for p in self.points.values()):
                                self.events.put(('error', 'Point ID already completed in this session. Use a new ID.'))
                                continue
                            self.points[data['token']] = data
                            self.active_token = data['token']
                            write_json(self.directory/'points.json', self.points)
                            self.events.put(('point', data.copy()))
                            command = f"MARK,{data['token']},{data['requested_scans']}"
                        elif kind == 'cancel' and self.active_token:
                            command = 'CANCEL,'+self.active_token
                            self.abort_point('USER_CANCELLED')
                        elif kind == 'rescan' and not self.active_token:
                            command = 'RESCAN'
                        if command:
                            device.write((command+'\n').encode('ascii')); device.flush()
                            commands.write(json.dumps({'pc_utc': utc(), 'command': command})+'\n'); commands.flush()
                    chunk = device.read(min(16384, max(1, device.in_waiting)))
                    if chunk:
                        raw.write(chunk); raw.flush(); self.bytes_count += len(chunk); buffer += chunk
                    while b'\n' in buffer:
                        line, buffer = buffer.split(b'\n', 1)
                        line = line.decode('utf-8', errors='replace').strip()
                        try: processor.feed(line, time.monotonic()-self.started)
                        except (ValueError, KeyError, IndexError, OverflowError) as exc:
                            self.events.put(('status', 'Malformed serial record: '+str(exc)))
                    if len(buffer) > 131072:
                        buffer = b''; self.events.put(('error', 'Unterminated serial record dropped; check baud rate.'))
                    if time.monotonic()-last_traffic >= 1:
                        self.events.put(('traffic', {'bytes': self.bytes_count, 'elapsed_s': time.monotonic()-self.started,
                                                    'scans': self.scan_count, 'ranges': self.range_count}))
                        last_traffic = time.monotonic()
                if self.active_token:
                    try: device.write(('CANCEL,'+self.active_token+'\n').encode('ascii')); device.flush()
                    except (OSError, serial.SerialException): pass
        except Exception as exc:
            error = str(exc)
            self.events.put(('error', error+' | Close IDF monitor/other programs using this COM port.'))
        finally:
            if device is not None and device.is_open: device.close()
            self.abort_point('CAPTURE_STOPPED' if error is None else 'CAPTURE_ERROR')
            metadata.update(state='ERROR' if error else 'FINISHED', error=error, ended_utc=utc(),
                            completed_scans=self.scan_count, completed_ranges=self.range_count,
                            received_bytes=self.bytes_count, elapsed_s=time.monotonic()-self.started)
            write_json(self.directory/'session.json', metadata)
            self.events.put(('finished', metadata))
