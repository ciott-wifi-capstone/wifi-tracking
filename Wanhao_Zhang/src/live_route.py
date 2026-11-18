"""Record Wi-PRO ranges and serve the local walking-test dashboard.

The recorder checks the configured A0 identity before capture. Manual marks are
saved as reference observations independently of the position solver.
"""
from __future__ import annotations

import argparse
from collections import Counter, deque
import csv
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import re
import secrets
import statistics
import sys
import threading
import time
from urllib.parse import urlsplit
import uuid
from zoneinfo import ZoneInfo

import wipro_live as live
from route_localization import solve_position
from live_automatic import StreamingAutomatic
from guarded_localization import DEFAULT_CONFIG as GUARDED_CONFIG, solve_guarded
from guarded_observations import summarize_completed

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
APP_SHA = '170e90965728c6f0b9b7829260bae19b9e2061d899bef679d647ad286d734494'
CANDIDATE_SHA = '3826bbc046dda7700236f135293c0895e3bc27b2c06f574cb246e3472acd1b6f'
MAC = re.compile(r'(?:[0-9A-F]{2}:){5}[0-9A-F]{2}\Z')
ACTIVE = {'starting', 'recording', 'stopping'}
GUARDED_METHOD = 'age_geometry_guarded_huber_v2_native_fallback'


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def mac(value):
    result = str(value).upper()
    if not MAC.fullmatch(result):
        raise ValueError('Invalid full MAC address')
    return result


def write_json(path, document):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    temporary.replace(path)


def load_configuration(registry_path, candidate_path=None, height=None):
    registry_bytes = Path(registry_path).read_bytes()
    registry = json.loads(registry_bytes.decode('utf-8-sig'))
    nodes = registry['nodes']
    owner = nodes['A0']
    owner_base, owner_ap = mac(owner['mac']), mac(owner['ap_mac'])
    anchors, addresses = [], {owner_base, owner_ap}
    for label in [f'A{i}' for i in range(1, 7)]:
        node = nodes[label]
        base, ap = mac(node['mac']), mac(node['ap_mac'])
        xy, z = node['position_xy_m'], node['antenna_height_m']
        if (len(xy) != 2 or not all(finite(v) for v in [*xy, z]) or z < 0
                or base in addresses or ap in addresses or base == ap):
            raise ValueError(f'Invalid identity/geometry: {label}')
        addresses.update([base, ap])
        anchors.append(dict(id=label, x=xy[0], y=xy[1], z=z, base_mac=base, ap_mac=ap))
    if owner_base == owner_ap:
        raise ValueError('A0 base and AP identity must differ')
    expected_firmware = registry.get('expected_firmware_sha256', APP_SHA)
    if not isinstance(expected_firmware, str) or not re.fullmatch(r'[0-9a-f]{64}', expected_firmware):
        raise ValueError('expected_firmware_sha256 must be a lowercase SHA-256 digest')
    for label in ['A0', *[a['id'] for a in anchors]]:
        if nodes[label].get('firmware', {}).get('application_sha256') != expected_firmware:
            raise ValueError(f'{label} does not match the registered firmware')
    tag_height = owner['antenna_height_m'] if height is None else height
    if not finite(tag_height) or tag_height < 0:
        raise ValueError('Finite nonnegative tag height required')
    candidate, candidate_bytes = None, None
    if candidate_path is not None:
        candidate_bytes = Path(candidate_path).read_bytes()
        if hashlib.sha256(candidate_bytes).hexdigest() != CANDIDATE_SHA:
            raise ValueError('Frozen candidate hash differs; no automatic fitting permitted')
        candidate = json.loads(candidate_bytes.decode('utf-8-sig'))
        if not finite(candidate.get('offset_to_subtract_m')):
            raise ValueError('Invalid frozen offset')
    return dict(registry=registry, registry_bytes=registry_bytes, anchors=anchors,
                owner_base=owner_base, owner_ap=owner_ap, tag_height_m=tag_height,
                candidate=candidate, candidate_bytes=candidate_bytes,
                expected_firmware_sha256=expected_firmware)


def validate_route(body, height):
    if not isinstance(body, dict):
        raise ValueError('Route must be an object')
    name = body.get('name', '实验路线')
    points = body.get('waypoints', [])
    if not isinstance(name, str) or len(name) > 120 or not isinstance(points, list) or len(points) > 200:
        raise ValueError('Route name/waypoints are invalid (maximum 200 points)')
    clean, used = [], set()
    for point in points:
        if not isinstance(point, dict):
            raise ValueError('Waypoint must be an object')
        label = point.get('id')
        if (not isinstance(label, str) or not label.strip() or len(label) > 64
                or label in used or any(ord(c) < 32 for c in label)):
            raise ValueError('Waypoint IDs must be unique, nonempty and at most 64 characters')
        if not all(finite(point.get(k)) for k in ['x', 'y']):
            raise ValueError('Waypoint x/y must be finite numbers in metres')
        used.add(label)
        clean.append(dict(id=label, x=point['x'], y=point['y'], z=height))
    return dict(name=name, waypoints=clean, reference_status='operator_entered_coordinates; uncertainty_unquantified')


class RouteCapture(live.Capture):
    """Reuse the existing CSV/TLV parser, with strict owner and durable raw bytes."""
    def __init__(self, port, directory, owner, targets, interval_ms, on_event, on_ready):
        super().__init__(port, directory, [], force=False, per_run=False)
        self.expected_owner = owner
        self.targets = set(targets)
        self.interval_ms = interval_ms
        self.on_event, self.on_ready = on_event, on_ready
        self.sent_commands = []
        self.raw = None
        self.raw_chunks = None
        self.buffer = bytearray()
        self.text_tail = ''
        self.stop_ack = False
        self.stop_error = None
        self.skipped_before_identity = 0
        self.device = None

    def _send(self, command):
        self.device.write((command + '\r\n').encode('ascii'))
        self.device.flush()
        self.sent_commands.append(dict(utc=utc_now(), unix_ms=time.time()*1000,
                                      monotonic_s=time.monotonic(), command=command))

    def _pump(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            data = self.device.read(8192)
            if not data:
                continue
            received_unix_ms, received_monotonic_s = time.time()*1000, time.monotonic()
            self.current_read_times = (received_unix_ms, received_monotonic_s)
            self.raw.write(data)
            end_offset = self.raw.tell()
            # End offsets are exclusive. Timing is host read completion, not RF time.
            # Log before parsing so a complete TLV's availability can be replayed.
            self.raw_chunks.write(json.dumps(dict(
                unix_ms=received_unix_ms, monotonic_s=received_monotonic_s,
                start_offset=end_offset-len(data), end_offset=end_offset,
                bytes_len=len(data)), allow_nan=False)+'\n')
            self.buffer.extend(data)
            self.text_tail = (self.text_tail + data.decode('utf-8', errors='replace'))[-24000:]
            # USB can begin in the middle of a CSI TLV, then reset/boot logs
            # and a complete MAC TLV follow. A nested STX must resynchronise,
            # otherwise the partial CSI consumes the complete identity reply.
            while self.buffer:
                start = self.buffer.find(live.FRAME_START)
                if start < 0:
                    self.buffer.clear()
                    break
                end = self.buffer.find(live.FRAME_END, start+1)
                nested = self.buffer.find(live.FRAME_START, start+1)
                if nested >= 0 and (end < 0 or nested < end):
                    del self.buffer[:nested]
                    self.dropped['TLV_RESYNC'] += 1
                    continue
                if end < 0:
                    del self.buffer[:start]
                    break
                frame = bytearray(self.buffer[start:end+1])
                del self.buffer[:end+1]
                for kind, fields in live.iter_frames(frame):
                    self._handle(kind, fields)

    def _handle(self, kind, fields):
        if kind == 'MAC':
            if not fields or not MAC.fullmatch(fields[0].strip().upper()):
                self.dropped[kind] += 1
                return
            if fields[0].strip().upper() != self.expected_owner:
                raise RuntimeError('USB A0 identity mismatch; capture stopped')
        elif kind in {'RANGE', 'FTM', 'CSI'} and self.own_mac == 'UNKNOWN':
            self.skipped_before_identity += 1
            return
        if kind in {'RANGE', 'FTM'}:
            if not fields or not MAC.fullmatch(fields[0].strip().upper()):
                self.dropped[kind] += 1
                return
            if fields[0].strip().upper() not in self.targets:
                raise RuntimeError('Unexpected ranging target; raw bytes retained')
        if kind == 'RANGE':
            try:
                if len(fields) < 3 or not math.isfinite(float(fields[2])):
                    raise ValueError('Invalid range')
                int(fields[1])
            except (ValueError, IndexError):
                self.dropped[kind] += 1
                return
        super()._handle(kind, fields)
        received_unix_ms, received_monotonic_s = getattr(
            self, 'current_read_times', (time.time()*1000, time.monotonic()))
        if kind == 'RANGE':
            self.on_event(dict(kind='range', peer=fields[0].upper(),
                               value=float(fields[2]), timestamp=int(fields[1]),
                               unix_ms=received_unix_ms, monotonic_s=received_monotonic_s))
        elif kind == 'FTM':
            peer, reports = live.parse_ftm(fields)
            if reports:
                self.on_event(dict(kind='raw', peer=peer.upper(),
                                   value=statistics.median(r['raw_m'] for r in reports),
                                   unix_ms=received_unix_ms, monotonic_s=received_monotonic_s))
            # A separate strict decoder consumes the COMPLETE same-event FTM+CSI
            # frame, including the first report omitted by the historical CSV.
            self.on_event(dict(kind='ftm_fields', peer=fields[0].upper(), fields=list(fields),
                               unix_ms=received_unix_ms, monotonic_s=received_monotonic_s))
        # Disk has the complete data. The legacy GUI series is not used here.
        with self.lock:
            for peer, rows in self.series.items():
                if len(rows) > 256:
                    del rows[:-128]

    def run(self):
        self.raw = (self.out_dir/'serial.raw').open('xb', buffering=0)
        try:
            self.raw_chunks = (self.out_dir/'raw_chunks.jsonl').open(
                'x', encoding='utf-8', buffering=1)
            self.device = self._open()
            self.device.dtr = False
            self.device.rts = True
            time.sleep(.15)
            self.device.rts = False
            self._pump(.6)
            for _ in range(5):
                self._send('id')
                self._pump(1.)
                if self.own_mac == self.expected_owner or self.stop.is_set(): break
            if self.own_mac != self.expected_owner:
                raise RuntimeError('No verified A0 identity response; did not unmute')
            for command in ['mute 1', 'wipro_iters 3', 'wipro 1',
                            f'interval {self.interval_ms}', 'burst 16', 'channel', 'peers']:
                self._send(command)
                self._pump(.4)
            if 'Current channel: 6 (40MHz)' not in self.text_tail:
                raise RuntimeError('Expected channel 6 / 40MHz not confirmed; did not unmute')
            if not self.stop.is_set():
                self._send('mute 0')
                self.on_ready()
            while not self.stop.is_set():
                self._pump(.1)
        except Exception as exc:
            self.error = f'{type(exc).__name__}: {exc}'
        finally:
            if self.device is not None:
                try:
                    # Search only the response AFTER this command, not earlier mute ACKs.
                    self.text_tail = ''
                    self._send('mute 1')
                    self._pump(1.4)
                    self.stop_ack = 'Mute: true' in self.text_tail
                except Exception as exc:
                    self.stop_error = str(exc)
                finally:
                    self.device.close()
            self.close()
            self.raw.close()
            if self.raw_chunks is not None:
                self.raw_chunks.close()


class RouteApp:
    def __init__(self, config, output_root, port=None, interval_ms=200,
                 max_age_s=4.0, range_window_s=1.5, recorder_factory=RouteCapture):
        self.config, self.output_root, self.port = config, Path(output_root), port
        self.interval_ms, self.max_age_s, self.range_window_s = interval_ms, max_age_s, range_window_s
        self.recorder_factory = recorder_factory
        self.lock = threading.RLock()
        self.phase, self.error = 'ready', None
        self.settings_file = self.output_root/'settings_current.json'
        if self.settings_file.exists():
            saved = json.loads(self.settings_file.read_text(encoding='utf-8'))
            if not finite(saved.get('tag_height_m')) or saved['tag_height_m'] < 0:
                raise ValueError('Invalid saved A0 height')
            self.config = dict(self.config, tag_height_m=saved['tag_height_m'],
                               height_reference_status=saved.get('height_reference_status','operator_entered_height; uncertainty_unquantified'))
        config = self.config
        self.route = validate_route({}, config['tag_height_m'])
        self.route_file = self.output_root/'route_plan_current.json'
        if self.route_file.exists():
            self.route = validate_route(json.loads(self.route_file.read_text(encoding='utf-8')), config['tag_height_m'])
        self.capture, self.worker = None, None
        self.directory, self.origin_mono, self.origin_unix_ms = None, None, None
        self.run_id = None
        self.recorded_height_m = None
        self.marks, self.mark_ids = [], {}
        self.trails = {name: deque(maxlen=10000) for name in
                       ['wipro', 'corrected', 'automatic', 'automatic_native', 'guarded']}
        self.solutions = {}
        self.observations = {a['id']: deque(maxlen=1000) for a in config['anchors']}
        self.raw_latest = {}
        self.automatic = StreamingAutomatic()
        # Pin what this process loaded. A new capture must never attribute an
        # already-running interpreter to source files edited after startup.
        runtime_paths = [Path(__file__), HERE/'live_route.html', HERE/'route_localization.py',
                         HERE/'wipro_live.py', HERE/'node_labels.py', HERE/'live_automatic.py',
                         HERE/'guarded_localization.py', HERE/'guarded_observations.py']
        runtime_paths.extend(p for p in (HERE/'automatic_branch_runtime').rglob('*')
                             if p.is_file() and '__pycache__' not in p.parts)
        self.runtime_sources = {p: p.read_bytes() for p in runtime_paths}
        self.automatic_observations = {a['id']: deque(maxlen=1000) for a in config['anchors']}
        self.automatic_native_observations = {a['id']: deque(maxlen=1000) for a in config['anchors']}
        self.automatic_latest = {}
        self.automatic_file = self.automatic_writer = None
        self.guarded_file = None
        self.by_mac = {a['ap_mac']: a['id'] for a in config['anchors']}
        self.track_file = self.track_writer = self.mark_file = self.mark_writer = None
        self.shutdown_event = threading.Event()
        self.compute_worker = threading.Thread(target=self._compute_loop, daemon=True)
        self.compute_worker.start()

    def set_settings(self, body):
        if not isinstance(body, dict) or not finite(body.get('tag_height_m')) or body['tag_height_m'] < 0:
            raise ValueError('A0 高度必须是以米为单位的有限非负数')
        height = body['tag_height_m']
        with self.lock:
            if self.phase in ACTIVE:
                raise ValueError('采集中高度已锁定，请停止后修改下一组设置')
            settings = dict(tag_height_m=height, height_reference_status='operator_entered_height; uncertainty_unquantified')
            route = validate_route(self.route, height)
            self.output_root.mkdir(parents=True, exist_ok=True)
            write_json(self.settings_file, settings)
            write_json(self.route_file, route)
            self.config = dict(self.config, **settings)
            self.route = route
        return self.state()

    def set_route(self, body):
        route = validate_route(body, self.config['tag_height_m'])
        with self.lock:
            if self.phase in ACTIVE:
                raise ValueError('采集开始后路线已锁定，请先停止再修改')
            self.output_root.mkdir(parents=True, exist_ok=True)
            write_json(self.route_file, route)
            self.route = route
        return self.state()

    def _select_port(self):
        from serial.tools import list_ports
        ports = [p for p in list_ports.comports() if p.vid == 0x303A
                 and (p.serial_number or '').upper() == self.config['owner_base']]
        if len(ports) != 1 or (self.port and self.port.upper() != ports[0].device.upper()):
            raise ValueError('未找到登记的新 A0 原生 USB：' + self.config['owner_base'])
        return ports[0].device

    def start(self):
        with self.lock:
            try:
                return self._start_unchecked()
            except Exception as exc:
                if self.phase == 'starting' and not (self.worker and self.worker.is_alive()):
                    self.phase, self.error = 'error', f'无法开始采集：{exc}'
                    for handle in [self.track_file, self.mark_file, self.automatic_file, self.guarded_file]:
                        if handle and not handle.closed: handle.close()
                    try: self._write_metadata(False)
                    except OSError: pass
                raise

    def _start_unchecked(self):
        with self.lock:
            if self.phase in ACTIVE:
                raise ValueError('已有采集正在进行')
            if self.worker is not None and self.worker.is_alive():
                raise ValueError('上一组采集尚未关闭')
            if any(not p.exists() or p.read_bytes() != data for p, data in self.runtime_sources.items()):
                raise ValueError('程序文件已更新，请重启本地服务后再采集，以保存正确的算法版本')
            port = self._select_port()
            self.output_root.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(ZoneInfo('Australia/Sydney')).strftime('%Y%m%d_%H%M%S')
            directory = self.output_root / f'A0_route_{stamp}_{uuid.uuid4().hex[:6]}'
            directory.mkdir(exist_ok=False)
            self.directory, self.port = directory, port
            self.run_id = uuid.uuid4().hex
            self.recorded_height_m = self.config['tag_height_m']
            self.origin_mono, self.origin_unix_ms = time.monotonic(), time.time()*1000
            self.phase, self.error = 'starting', None
            self.capture, self.worker = None, None
            self.marks, self.mark_ids = [], {}
            self.solutions = {}
            for series in self.trails.values(): series.clear()
            for series in self.observations.values(): series.clear()
            for series in self.automatic_observations.values(): series.clear()
            for series in self.automatic_native_observations.values(): series.clear()
            self.automatic.reset()
            self.automatic_latest = {}
            self.raw_latest = {}
            (directory/'node_registry.json').write_bytes(self.config['registry_bytes'])
            if self.config['candidate_bytes']:
                (directory/'frozen_candidate.json').write_bytes(self.config['candidate_bytes'])
            write_json(directory/'route_plan.json', self.route)
            provenance = directory/'provenance'
            provenance.mkdir()
            for source, data in self.runtime_sources.items():
                target = provenance/source.relative_to(HERE)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            self.track_file = (directory/'track.csv').open('x', newline='', encoding='utf-8')
            self.track_writer = csv.DictWriter(self.track_file, fieldnames=[
                't_ms','relative_s','method','x','y','residual_rms_m','anchors_n',
                'max_age_s','measurement_span_s','ranges_json','excluded_json'])
            self.track_writer.writeheader()
            self.mark_file = (directory/'marks.csv').open('x', newline='', encoding='utf-8')
            self.mark_writer = csv.DictWriter(self.mark_file, fieldnames=[
                'seq','waypoint_id','x','y','z','t_ms','relative_s','utc',
                'monotonic_s','client_unix_ms','client_event_id','reference_status'])
            self.mark_writer.writeheader()
            self.mark_file.flush()
            self.automatic_file = (directory/'automatic_ranges.csv').open('x', newline='', encoding='utf-8')
            self.automatic_writer = csv.DictWriter(self.automatic_file, fieldnames=[
                'peer','t2','native_m','automatic_m','gate_score','branch','features_status',
                'native_unix_ms','native_monotonic_s','available_unix_ms','available_monotonic_s',
                'prediction_ready_unix_ms','prediction_ready_monotonic_s','feature_wait_s'])
            self.automatic_writer.writeheader()
            self.automatic_file.flush()
            self.guarded_file = (directory/'guarded_diagnostics.jsonl').open('x', encoding='utf-8', buffering=1)
            self.capture = self.recorder_factory(port, directory, self.config['owner_ap'],
                self.by_mac, self.interval_ms, self.receive_event, self._ready)
            self._write_metadata(False)
            self.worker = threading.Thread(target=self._record, daemon=True)
            self.worker.start()
        return self.state()

    def _ready(self):
        with self.lock:
            if self.phase == 'starting':
                self.phase = 'recording'

    def receive_event(self, event):
        with self.lock:
            label = self.by_mac[event['peer']]
            if event['kind'] == 'range':
                self.observations[label].append(dict(event))
                outputs = self.automatic.ingest_range(event['peer'], event['timestamp'], event['value'],
                                                       event['unix_ms'], event['monotonic_s'])
            elif event['kind'] == 'ftm_fields':
                outputs = self.automatic.ingest_ftm(event['fields'], event['unix_ms'], event['monotonic_s'])
            else:
                self.raw_latest[label] = dict(event)
                return
            ready_unix_ms, ready_monotonic_s = time.time()*1000, time.monotonic()
            for result in outputs:
                result_label = self.by_mac[result['peer']]
                # Features must have arrived before this prediction is emitted.
                # Freshness remains anchored to the ORIGINAL RANGE receipt; the
                # CSI wait does not make an old measurement appear newer.
                native_mono = result['native_monotonic_s']
                common = dict(monotonic_s=native_mono, unix_ms=result['native_unix_ms'],
                              available_monotonic_s=result['monotonic_s'],
                              prediction_ready_monotonic_s=ready_monotonic_s,
                              timestamp=result['timestamp'])
                self.automatic_observations[result_label].append(dict(common, value=result['value'],
                                                                     native_value=result['native_value']))
                self.automatic_native_observations[result_label].append(dict(common, value=result['native_value']))
                self.automatic_latest[result_label] = dict(result)
                self.automatic_writer.writerow(dict(peer=result['peer'], t2=result['timestamp'],
                    native_m=result['native_value'], automatic_m=result['value'], gate_score=result['p'],
                    branch=result['branch'], features_status=result['features_status'],
                    native_unix_ms=result['native_unix_ms'], native_monotonic_s=native_mono,
                    available_unix_ms=result['unix_ms'], available_monotonic_s=result['monotonic_s'],
                    prediction_ready_unix_ms=ready_unix_ms, prediction_ready_monotonic_s=ready_monotonic_s,
                    feature_wait_s=max(0., result['monotonic_s']-native_mono)))
            if outputs:
                self.automatic_file.flush()

    def _record(self):
        try:
            self.capture.run()
        except Exception as exc:
            self.capture.error = f'{type(exc).__name__}: {exc}'
        finally:
            with self.lock:
                self.error = self.capture.error
                if not self.capture.stop_ack:
                    self.error = self.error or '串口已关闭，但未确认最终 mute 1 应答；请检查 A0'
                self.phase = 'error' if self.error else 'stopped'
                self.ended_mono = time.monotonic()
                for handle in [self.track_file, self.mark_file, self.automatic_file, self.guarded_file]:
                    if handle: handle.close()
                self._write_metadata(True)

    def stop(self):
        with self.lock:
            worker, capture = self.worker, self.capture
            if self.phase in {'starting', 'recording'}:
                self.phase = 'stopping'
                capture.stop.set()
        if worker and worker.is_alive():
            worker.join(timeout=12)
            if worker.is_alive():
                raise ValueError('正在等待串口安全关闭，请稍候')
        return self.state()

    def mark(self, body):
        if not isinstance(body, dict):
            raise ValueError('Invalid mark')
        event_id = body.get('client_event_id')
        if not isinstance(event_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', event_id):
            raise ValueError('Mark requires a unique client_event_id')
        with self.lock:
            if event_id in self.mark_ids:
                old = self.mark_ids[event_id]
                if old['waypoint_id'] != body.get('waypoint_id'):
                    raise ValueError('Duplicate mark ID has a different waypoint')
                return old
            if self.phase != 'recording':
                raise ValueError('请等 A0 身份核验完成并开始采集后再 MARK')
            point = next((p for p in self.route['waypoints'] if p['id'] == body.get('waypoint_id')), None)
            if point is None:
                raise ValueError('请选择已经保存坐标的路线点')
            client_ms = body.get('client_unix_ms')
            if client_ms is not None and not finite(client_ms):
                raise ValueError('Invalid client timestamp')
            now_mono, now_ms = time.monotonic(), time.time()*1000
            mark = dict(seq=len(self.marks)+1, waypoint_id=point['id'], x=point['x'],
                y=point['y'], z=point['z'], t_ms=now_ms, t_s=now_mono-self.origin_mono,
                relative_s=now_mono-self.origin_mono, utc=utc_now(), monotonic_s=now_mono,
                client_unix_ms=client_ms, client_event_id=event_id,
                reference_status='operator_marked_arrival_only; no_continuous_ground_truth')
            self.mark_writer.writerow({k: mark[k] for k in self.mark_writer.fieldnames})
            self.mark_file.flush()
            self.marks.append(mark)
            self.mark_ids[event_id] = mark
            # Only a separate log receives truth. No range/solver state is modified.
            with (self.directory/'mark_events.jsonl').open('a', encoding='utf-8') as fh:
                fh.write(json.dumps(dict(mark=mark, estimates_at_receipt=self.solutions), ensure_ascii=False, allow_nan=False)+'\n')
                fh.flush()
            return dict(mark)

    def _link_snapshot(self, now, observations=None):
        links, usable, times = [], {}, {}
        observations = self.observations if observations is None else observations
        for anchor in self.config['anchors']:
            label = anchor['id']
            rows = observations[label]
            if not rows:
                links.append(dict(id=label, distance_m=None, raw_m=None, age_s=None,
                                  samples_n=0, valid_for_localisation=False))
                continue
            latest = max(rows, key=lambda r: r['monotonic_s'])
            # Median within the most recent burst/window, not an entire trajectory.
            batch = [r['value'] for r in rows if r['monotonic_s'] >= latest['monotonic_s']-self.range_window_s]
            distance = statistics.median(batch)
            age = max(0., now-latest['monotonic_s'])
            valid = age <= self.max_age_s and distance >= 0
            raw = self.raw_latest.get(label, {}).get('value')
            links.append(dict(id=label, distance_m=distance, raw_m=raw, age_s=age,
                              samples_n=len(batch), valid_for_localisation=valid))
            # Fresh negative medians remain in this mapping so both methods apply
            # the identical numerical validity rule in solve_position.
            if age <= self.max_age_s:
                usable[label] = distance
                times[label] = latest['monotonic_s']
        return links, usable, times

    def _compute_loop(self):
        while not self.shutdown_event.wait(.5):
            with self.lock:
                if self.phase != 'recording': continue
                now, run_id = time.monotonic(), self.run_id
                _, distances, times = self._link_snapshot(now)
                _, automatic_distances, automatic_times = self._link_snapshot(now, self.automatic_observations)
                _, automatic_native_distances, automatic_native_times = self._link_snapshot(now, self.automatic_native_observations)
                guarded_links = summarize_completed(self.automatic_observations, now, self.range_window_s)
                offset = self.config['candidate']['offset_to_subtract_m'] if self.config['candidate'] else None
            methods = {'wipro': distances}
            if offset is not None:
                methods['corrected'] = {k: v-offset for k, v in distances.items()}
            methods['automatic'] = automatic_distances
            methods['automatic_native'] = automatic_native_distances
            method_times = {'wipro': times, 'corrected': times,
                            'automatic': automatic_times, 'automatic_native': automatic_native_times}
            solved = {}
            for method, values in methods.items():
                result = solve_position(self.config['anchors'], values, self.config['tag_height_m'])
                used = result.get('anchors_used', [])
                if used:
                    used_times = [method_times[method][k] for k in used]
                    max_age = now-min(used_times)
                    span = max(used_times)-min(used_times)
                else:
                    max_age, span = None, None
                result.update(t=now-self.origin_mono, age_s=max_age,
                              measurement_span_s=span, computed_monotonic_s=now)
                solved[method] = result
            guarded = solve_guarded(self.config['anchors'], guarded_links, self.config['tag_height_m'], now)
            guarded.update(t=now-self.origin_mono, computed_monotonic_s=now)
            solved['guarded'] = guarded
            methods['guarded'] = guarded.get('ranges_used', {})
            with self.lock:
                if self.phase != 'recording' or self.run_id != run_id: continue
                self.solutions = solved
                if self.guarded_file is not None:
                    self.guarded_file.write(json.dumps(dict(relative_s=now-self.origin_mono,
                        monotonic_s=now, result=guarded), ensure_ascii=False, allow_nan=False)+'\n')
                for method, result in solved.items():
                    if not result['ok']: continue
                    point = dict(t=result['t'], x=result['x'], y=result['y'])
                    self.trails[method].append(point)
                    self.track_writer.writerow(dict(t_ms=time.time()*1000, relative_s=result['t'],
                        method=method, x=result['x'], y=result['y'], residual_rms_m=result['residual_rms_m'],
                        anchors_n=result['anchors_n'], max_age_s=result['age_s'],
                        measurement_span_s=result['measurement_span_s'],
                        ranges_json=json.dumps(methods[method]), excluded_json=json.dumps(result.get('anchors_excluded', []))))
                self.track_file.flush()

    def _write_metadata(self, final):
        if self.directory is None: return
        capture = self.capture
        document = dict(schema_version=1, role='live_visualisation_and_operator_waypoint_marks',
            origin_unix_ms=self.origin_unix_ms, origin_monotonic_s=self.origin_mono,
            updated_utc=utc_now(), phase=self.phase, final=final, error=self.error,
            own_base_mac=self.config['owner_base'], expected_own_ap_mac=self.config['owner_ap'],
            port=self.port, tag_height_m=self.config['tag_height_m'], anchors=self.config['anchors'],
            height_reference_status=self.config.get('height_reference_status','configured_height_assumption; uncertainty_unquantified'),
            commands=capture.sent_commands if capture else [],
            runtime_parameters=dict(interval_ms=self.interval_ms, burst=16, wipro_iters=3),
            solver=dict(name='unweighted_2D_least_squares_using_3D_slant_ranges', update_period_s=.5,
                max_age_s=self.max_age_s, recent_per_link_median_window_s=self.range_window_s,
                known_fixed_height=True, route_or_marks_used_for_position=False,
                smoothing=False, clipping=False, online_bias_fitting=False,
                asynchronous_ranges=True, residual_is_not_accuracy=True),
            raw_distances_preserved=True, negative_distances_preserved_in_raw=True,
            raw_chunk_timing=dict(file='raw_chunks.jsonl',
                offsets='zero_based_bytes; start_inclusive_end_exclusive',
                clock='host_serial_read_completion_unix_ms_and_monotonic_s',
                purpose='Reconstruct complete TLV availability without treating RF timestamps as host availability'),
            negative_distances_excluded_from_position_only=True,
            extra_offset_applied_in_firmware=False,
            offset_comparison_m=self.config['candidate']['offset_to_subtract_m'] if self.config['candidate'] else None,
            candidate_sha256=CANDIDATE_SHA if self.config['candidate'] else None,
            automatic_correction=self.automatic.metadata(),
            automatic_comparison=dict(method='automatic_compact_hard',
                correction_in_host=True, frozen_coefficients=True, model_fitted_online=False,
                immediate_native_method='wipro', completion_aligned_native_method='automatic_native',
                freshness_clock='original_RANGE_host_receipt_not_feature_completion',
                availability='same_exact_peer_and_t2_RANGE_and_complete_FTM_CSI_frame',
                negative_outputs='preserved_in_automatic_ranges_csv; excluded_only_by_position_solver',
                per_link_aggregation='same_recent_median_and_age_rule_for_each_method',
                gate_meaning='training_defined_large_positive_bias; not_ground_truth_LOS_probability',
                truth_route_or_marks_used_for_inference=False),
            guarded_localization=dict(method=GUARDED_METHOD,
                config=dict(GUARDED_CONFIG), enabled=True,
                inputs='same_event_native_and_frozen_automatic_latest_completed_burst_medians',
                cadence='per_link_causal_median_of_last_six_completed_gaps; then_global_median_of_eligible_links; startup_6s',
                logs='guarded_diagnostics.jsonl', model_refitted=False,
                fallback_policy='same_event_native_minus_frozen_offset_then_original_native_when_physical; otherwise_exclude; all_decisions_logged',
                route_or_marks_used=False, position_smoothing=False,
                motion_parameter='assumed_cross_time_triangle_tolerance; not_measured_speed_or_position_clipping',
                comparison='original_wipro_automatic_corrected_rules_retained_for_ablation'),
            marks_count=len(self.marks), mark_clock='server_receipt_monotonic_relative_to_origin',
            mark_limitations='Human/key/network delay unquantified. Arrival marks only; planned connecting lines are not measured continuous truth.',
            counts=dict(capture.counts) if capture else {}, dropped_frames=dict(capture.dropped) if capture else {},
            stop_mute_1_acknowledged=capture.stop_ack if capture else False,
            stop_error=capture.stop_error if capture else None)
        if final:
            document['files'] = {str(p.relative_to(self.directory)): dict(bytes=p.stat().st_size,
                sha256=hashlib.sha256(p.read_bytes()).hexdigest()) for p in self.directory.rglob('*')
                if p.is_file() and p.name != 'session.json'}
        write_json(self.directory/'session.json', document)

    def state(self):
        with self.lock:
            now = time.monotonic()
            links, _, _ = self._link_snapshot(now)
            automatic_links, _, _ = self._link_snapshot(now, self.automatic_observations)
            for link, auto_link in zip(links, automatic_links):
                latest = self.automatic_latest.get(link['id'], {})
                status = latest.get('features_status', 'waiting_same_event_FTM')
                if auto_link['distance_m'] is not None:
                    if auto_link['distance_m'] < 0:
                        status = 'negative_prediction_excluded'
                    elif auto_link['age_s'] > self.max_age_s:
                        status = 'stale'
                link.update(automatic_m=auto_link['distance_m'], automatic_age_s=auto_link['age_s'],
                            automatic_branch=latest.get('branch'), automatic_probability=latest.get('p'),
                            automatic_status=status, automatic_valid=auto_link['valid_for_localisation'])
            guard_result = self.solutions.get('guarded', {})
            guard_diagnostics = {r['id']: r for r in guard_result.get('link_diagnostics', [])}
            for link in links:
                diagnostic = guard_diagnostics.get(link['id'], {})
                elapsed = max(0., now-guard_result.get('computed_monotonic_s', now))
                guard_age = diagnostic.get('age_s')
                reasons = diagnostic.get('reasons', [])
                link.update(guarded_m=diagnostic.get('used_m'),
                            guarded_weight=diagnostic.get('weight'),
                            guarded_age_s=guard_age+elapsed if guard_age is not None else None,
                            guarded_reason='; '.join(reasons),
                            guarded_valid=diagnostic.get('valid', False)
                                and self.phase == 'recording'
                                and now < guard_result.get('valid_until_monotonic_s', now))
            ages = [link['age_s'] for link in links if link['age_s'] is not None]
            capture = self.capture
            counts = dict(capture.counts) if capture else {}
            solutions = {k: dict(v) for k, v in self.solutions.items()}
            for method, solution in solutions.items():
                if solution.get('age_s') is not None:
                    solution['age_s'] += max(0., now-solution.get('computed_monotonic_s', now))
                    expired = (now >= solution.get('valid_until_monotonic_s', now)
                               if method == 'guarded' else solution['age_s'] > self.max_age_s)
                    if expired:
                        solution['ok'] = False
                        solution['reason'] = '定位所用测距已过期；等待新测距'
            if self.phase != 'recording':
                for solution in solutions.values():
                    solution['ok'] = False
                    solution['reason'] = '采集已停止；轨迹仅供回看'
            return dict(app_id='wipro_live_route_v1', mode='LIVE', phase=self.phase, session_dir=str(self.directory) if self.directory else None,
                elapsed_s=(now if self.phase in ACTIVE else getattr(self,'ended_mono',now))-self.origin_mono if self.origin_mono else 0,
                port=self.port, identity='A0 · '+self.config['owner_ap'], tag_height_m=self.config['tag_height_m'],
                recorded_tag_height_m=self.recorded_height_m,
                height_reference_status=self.config.get('height_reference_status','configured_height_assumption; uncertainty_unquantified'),
                latest_age_s=min(ages) if ages else None, error=self.error, anchors=self.config['anchors'],
                route={**self.route, 'locked': self.phase in ACTIVE},
                trails={k: list(v) for k,v in self.trails.items()}, solutions=solutions, links=links,
                marks=list(self.marks), counts=dict(ftm=counts.get('FTM',0), range=counts.get('RANGE',0),
                    csi=counts.get('CSI',0), dropped=sum(capture.dropped.values()) if capture else 0),
                candidate=dict(offset_m=self.config['candidate']['offset_to_subtract_m'] if self.config['candidate'] else None,
                    enabled=self.config['candidate'] is not None),
                automatic={**self.automatic.metadata(), 'low_offset_m': self.automatic.metadata().get('los_offset_m')},
                guarded=dict(enabled=True, name=GUARDED_METHOD,
                    config=dict(GUARDED_CONFIG),
                    description='测量时龄连续降权、跨锚点几何保护与 Huber 定位；路线及 MARK 不参与计算'),
                source_note='实时轨迹为多边测距估计；路线和 MARK 不参与定位。到点标记不是连续轨迹真值。',
                stop_ack=capture.stop_ack if capture else None,
                parameters=dict(interval_ms=self.interval_ms, max_age_s=self.max_age_s,
                    range_window_s=self.range_window_s, update_period_s=.5))

    def close(self):
        self.stop()
        self.shutdown_event.set()
        self.compute_worker.join(timeout=3)


class RouteServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, app):
        self.app, self.token = app, secrets.token_urlsafe(24)
        super().__init__(address, RouteHandler)


class RouteHandler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def _respond(self, status, data, content_type='application/json; charset=utf-8'):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def _valid_host(self):
        return self.headers.get('Host') in {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}

    def do_GET(self):
        if not self._valid_host():
            return self._respond(403, dict(error='Local host only'))
        path = urlsplit(self.path).path
        if path == '/api/state':
            return self._respond(200, self.server.app.state())
        if path == '/api/token':
            return self._respond(200, dict(token=self.server.token))
        if path == '/':
            # Same-origin API token is inserted here; hostile pages cannot read it.
            html = (HERE/'live_route.html').read_text(encoding='utf-8')
            injection = '<script>window.ROUTE_API_TOKEN='+json.dumps(self.server.token)+';</script>'
            html = html.replace('<head>', '<head>'+injection, 1)
            return self._respond(200, html.encode('utf-8'), 'text/html; charset=utf-8')
        if path == '/favicon.ico': return self._respond(204,b'')
        return self._respond(404, dict(error='Not found'))

    def do_POST(self):
        allowed_origins = {f'http://127.0.0.1:{self.server.server_port}', f'http://localhost:{self.server.server_port}'}
        if (not self._valid_host() or self.headers.get('Origin') not in allowed_origins
                or self.headers.get('X-Route-Token') != self.server.token):
            return self._respond(403, dict(error='Local same-origin request required'))
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length < 0 or length > 65536: raise ValueError('Request too large')
            body = json.loads(self.rfile.read(length) or b'{}', parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))
            path = urlsplit(self.path).path
            if path == '/api/start': result = self.server.app.start()
            elif path == '/api/stop': result = self.server.app.stop()
            elif path == '/api/shutdown':
                result = self.server.app.stop()
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            elif path == '/api/route': result = self.server.app.set_route(body)
            elif path == '/api/settings': result = self.server.app.set_settings(body)
            elif path == '/api/mark': result = self.server.app.mark(body)
            else: return self._respond(404, dict(error='Not found'))
            return self._respond(200, result)
        except (ValueError, KeyError, TypeError) as exc:
            return self._respond(400, dict(error=str(exc)))
        except Exception as exc:
            return self._respond(500, dict(error=f'{type(exc).__name__}: {exc}'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port')
    parser.add_argument('--http-port', type=int, default=8765)
    parser.add_argument('--registry', type=Path, default=PROJECT/'wipro_nodes.json')
    parser.add_argument('--out', type=Path, default=PROJECT/'captures/live_route')
    parser.add_argument('--candidate', type=Path, default=HERE/'route_global_offset_candidate.json')
    parser.add_argument('--height', type=float)
    parser.add_argument('--interval', type=int, default=200)
    parser.add_argument('--max-age', type=float, default=4.)
    parser.add_argument('--range-window', type=float, default=1.5)
    args = parser.parse_args()
    if args.interval < 100 or not all(math.isfinite(v) and v>0 for v in [args.max_age,args.range_window]):
        parser.error('Require interval >=100 ms and finite positive age/window')
    config = load_configuration(args.registry, args.candidate if args.candidate.exists() else None, args.height)
    app = RouteApp(config,args.out,args.port,args.interval,args.max_age,args.range_window)
    server = RouteServer(('127.0.0.1',args.http_port),app)
    print(json.dumps(dict(url=f'http://127.0.0.1:{server.server_port}/',phase='ready',
                          owner=config['owner_base'],output_root=str(args.out)),ensure_ascii=False),flush=True)
    try: server.serve_forever(poll_interval=.3)
    except KeyboardInterrupt: pass
    finally:
        server.server_close()
        app.close()


if __name__ == '__main__': main()
