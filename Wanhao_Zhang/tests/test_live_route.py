"""Recorder/mark safety regressions. All serial and HTTP IO is mocked."""
from __future__ import annotations

from collections import Counter
import csv
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))
import live_route as route


OWNER = '24:EC:4A:03:47:A1'


def registry_document():
    nodes = {'A0': dict(mac='24:EC:4A:03:47:A0', ap_mac=OWNER,
                        antenna_height_m=1.22,
                        firmware={'application_sha256': route.APP_SHA})}
    coordinates = [(0, 0), (10, 0), (10, 10), (0, 10), (5, 15), (15, 5)]
    for n, (x, y) in enumerate(coordinates, 1):
        nodes[f'A{n}'] = dict(mac=f'AA:BB:CC:DD:{n:02X}:00',
                              ap_mac=f'AA:BB:CC:DD:{n:02X}:01',
                              position_xy_m=[x, y], antenna_height_m=1.22,
                              firmware={'application_sha256': route.APP_SHA})
    return {'nodes': nodes}


class FakeRecorder:
    """A verified recorder that stays alive until stop, without any device IO."""
    def __init__(self, port, directory, owner, targets, interval_ms, on_event,
                 on_ready, acknowledge=True):
        self.port, self.out_dir, self.expected_owner = port, directory, owner
        self.on_event, self.on_ready = on_event, on_ready
        self.stop, self.ready = threading.Event(), threading.Event()
        self.counts, self.dropped = Counter(), Counter()
        self.sent_commands, self.stop_error, self.error = [], None, None
        self.stop_ack, self.acknowledge, self.closed = False, acknowledge, False

    def run(self):
        self.on_ready()
        self.ready.set()
        if not self.stop.wait(4):
            self.error = 'Test did not stop its fake recorder'
        self.sent_commands.append({'command': 'mute 1'})
        self.stop_ack = self.acknowledge
        self.closed = True


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.registry_path = self.root / 'nodes.json'
        self.registry_path.write_text(json.dumps(registry_document()), encoding='utf-8')
        self.config = route.load_configuration(self.registry_path)
        self.apps = []

    def app(self, acknowledge=True):
        app = route.RouteApp(self.config, self.root / 'runs', port='COM_FAKE',
            recorder_factory=lambda *args: FakeRecorder(*args, acknowledge=acknowledge))
        # Unit tests drive compute ticks explicitly, never a live background solver.
        app.shutdown_event.set()
        app.compute_worker.join(1)
        app._select_port = lambda: 'COM_FAKE'
        self.apps.append(app)
        self.addCleanup(app.close)
        return app

    def started(self, acknowledge=True):
        app = self.app(acknowledge)
        app.set_route({'name': 'measured test waypoints', 'waypoints': [
            {'id': 'P1', 'x': 50., 'y': 60.}, {'id': 'P2', 'x': 51., 'y': 60.}]})
        app.start()
        self.assertTrue(app.capture.ready.wait(1))
        self.assertEqual(app.phase, 'recording')
        return app

    def one_compute_tick(self, app, now):
        actual_event = app.shutdown_event
        app.shutdown_event = Mock(wait=Mock(side_effect=[False, True]))
        try:
            with patch.object(route.time, 'monotonic', return_value=now):
                app._compute_loop()
        finally:
            app.shutdown_event = actual_event

    def fill_ranges(self, app, x=2., y=3., received=100.):
        import math
        for anchor in app.config['anchors']:
            value = math.dist([x, y, 1.22], [anchor[k] for k in ('x', 'y', 'z')])
            app.receive_event(dict(kind='range', peer=anchor['ap_mac'], value=value,
                                   unix_ms=123456., monotonic_s=received, timestamp=1))

    def test_registry_rejects_duplicate_mac_instead_of_merging_labels(self):
        document = registry_document()
        document['nodes']['A2']['ap_mac'] = document['nodes']['A1']['ap_mac']
        self.registry_path.write_text(json.dumps(document), encoding='utf-8')
        with self.assertRaises(ValueError):
            route.load_configuration(self.registry_path)

    def test_deployment_can_register_a_new_build_with_one_consistent_digest(self):
        document = registry_document()
        digest = 'a' * 64
        document['expected_firmware_sha256'] = digest
        for node in document['nodes'].values():
            node['firmware']['application_sha256'] = digest
        self.registry_path.write_text(json.dumps(document), encoding='utf-8')
        config = route.load_configuration(self.registry_path)
        self.assertEqual(config['expected_firmware_sha256'], digest)

        document['nodes']['A3']['firmware']['application_sha256'] = route.APP_SHA
        self.registry_path.write_text(json.dumps(document), encoding='utf-8')
        with self.assertRaises(ValueError):
            route.load_configuration(self.registry_path)

    def test_expected_firmware_digest_requires_a_valid_sha256(self):
        for invalid in ['a' * 63, 'G' * 64, True, None]:
            with self.subTest(invalid=invalid):
                document = registry_document()
                document['expected_firmware_sha256'] = invalid
                self.registry_path.write_text(json.dumps(document), encoding='utf-8')
                with self.assertRaises(ValueError):
                    route.load_configuration(self.registry_path)

    def test_capture_rejects_code_changed_since_process_loaded_before_opening_serial(self):
        app = self.app()
        source = next(iter(app.runtime_sources))
        app.runtime_sources[source] = b'older loaded program'
        with self.assertRaisesRegex(ValueError,'程序文件已更新'):
            app.start()
        self.assertIsNone(app.capture)
        self.assertIsNone(app.directory)

    def test_registry_rejects_nan_geometry_wrong_firmware_and_negative_height(self):
        for mutate in [lambda d: d['nodes']['A1'].update(position_xy_m=[float('nan'), 0]),
                       lambda d: d['nodes']['A4']['firmware'].update(application_sha256='modified'),
                       lambda d: d['nodes']['A0'].update(antenna_height_m=-1)]:
            with self.subTest(mutate=mutate):
                document = registry_document()
                mutate(document)
                self.registry_path.write_text(json.dumps(document), encoding='utf-8')
                with self.assertRaises(ValueError):
                    route.load_configuration(self.registry_path)

    def test_modified_candidate_is_rejected_before_a_recorder_is_created(self):
        candidate = self.root / 'candidate.json'
        candidate.write_text('{"offset_to_subtract_m": 100}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'hash'):
            route.load_configuration(self.registry_path, candidate)

    def test_route_rejects_duplicate_or_nonfinite_or_boolean_coordinates(self):
        points = [{'id': 'p', 'x': 0., 'y': 0.}]
        for bad in [points * 2, [{'id': 'p', 'x': float('inf'), 'y': 0}],
                    [{'id': 'p', 'x': True, 'y': 0}],
                    [{'id': '\n', 'x': 0, 'y': 0}]]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                route.validate_route({'waypoints': bad}, 1.22)

    def test_mark_requires_active_verified_recording_and_saved_waypoint(self):
        app = self.app()
        payload = {'waypoint_id': 'P1', 'client_event_id': 'arrival_001'}
        with self.assertRaises(ValueError):
            app.mark(payload)
        app.start()
        self.assertTrue(app.capture.ready.wait(1))
        with self.assertRaises(ValueError):
            app.mark(payload)

    def test_mark_is_idempotent_and_server_receipt_clock_is_authoritative(self):
        app = self.started()
        before = {key: list(value) for key, value in app.observations.items()}
        app.solutions = {'wipro': {'ok': True, 'x': -20., 'y': -30.}}
        payload = dict(waypoint_id='P1', client_event_id='arrival_001',
                       client_unix_ms=1.)
        with patch.object(route.time, 'monotonic', return_value=app.origin_mono+8), \
             patch.object(route.time, 'time', return_value=2000.):
            first = app.mark(payload)
        duplicate = app.mark(payload)
        self.assertEqual(first, duplicate)
        self.assertEqual(first['relative_s'], 8)
        self.assertEqual(first['t_ms'], 2_000_000.)
        self.assertEqual(first['client_unix_ms'], 1.)
        self.assertEqual(len(app.marks), 1)
        self.assertEqual(before, {key: list(value) for key, value in app.observations.items()})
        self.assertEqual(app.solutions['wipro']['x'], -20.)
        with self.assertRaises(ValueError):
            app.mark({**payload, 'waypoint_id': 'P2'})
        app.stop()
        with (app.directory / 'marks.csv').open(encoding='utf-8', newline='') as handle:
            self.assertEqual(len(list(csv.DictReader(handle))), 1)
        logged = json.loads((app.directory / 'mark_events.jsonl').read_text(encoding='utf-8'))
        self.assertEqual(logged['estimates_at_receipt']['wipro']['x'], -20.)

    def test_route_is_locked_during_capture_and_mark_nonfinite_timestamp_rejected(self):
        app = self.started()
        with self.assertRaises(ValueError):
            app.set_route({'waypoints': []})
        with self.assertRaises(ValueError):
            app.mark(dict(waypoint_id='P1', client_event_id='arrival_001',
                          client_unix_ms=float('nan')))
        self.assertEqual(app.marks, [])

    def test_idle_height_change_persists_restart_and_updates_future_waypoint_height(self):
        app = self.app()
        app.set_route({'waypoints': [{'id': 'P1', 'x': 2., 'y': 3.}]})
        registry_bytes = self.registry_path.read_bytes()
        state = app.set_settings({'tag_height_m': 1.4})
        self.assertEqual(state['tag_height_m'], 1.4)
        self.assertIsNone(state['recorded_tag_height_m'])
        self.assertEqual(state['route']['waypoints'][0]['z'], 1.4)
        settings = json.loads((app.output_root / 'settings_current.json').read_text(encoding='utf-8'))
        self.assertEqual(settings['tag_height_m'], 1.4)
        self.assertEqual(self.registry_path.read_bytes(), registry_bytes)
        # A new process constructs fresh config from the registry (still1.22)
        # and must load the saved application setting and route at1.4 instead.
        app.close()
        restarted = self.app()
        self.assertEqual(restarted.state()['tag_height_m'], 1.4)
        self.assertEqual(restarted.route['waypoints'][0]['z'], 1.4)
        restarted.start()
        self.assertTrue(restarted.capture.ready.wait(1))
        mark = restarted.mark({'waypoint_id': 'P1', 'client_event_id': 'arrival_001'})
        self.assertEqual(mark['z'], 1.4)
        restarted.stop()
        self.assertEqual(json.loads((restarted.directory / 'session.json').read_text(encoding='utf-8'))['tag_height_m'], 1.4)
        self.assertEqual(self.registry_path.read_bytes(), registry_bytes)

    def test_recording_height_is_locked_and_stopped_change_only_affects_next_run(self):
        app = self.started()
        self.assertEqual(app.state()['recorded_tag_height_m'], 1.22)
        with self.assertRaises(ValueError):
            app.set_settings({'tag_height_m': 1.4})
        self.assertEqual(app.config['tag_height_m'], 1.22)
        self.assertFalse((app.output_root / 'settings_current.json').exists())
        app.mark({'waypoint_id': 'P1', 'client_event_id': 'arrival_001'})
        app.stop()
        directory = app.directory
        recorded_files = {name: (directory / name).read_bytes()
                          for name in ['session.json', 'route_plan.json', 'marks.csv']}
        state = app.set_settings({'tag_height_m': 1.4})
        self.assertEqual(state['tag_height_m'], 1.4)
        self.assertEqual(state['recorded_tag_height_m'], 1.22)
        self.assertEqual(state['marks'][0]['z'], 1.22)
        self.assertEqual(state['route']['waypoints'][0]['z'], 1.4)
        for name, original in recorded_files.items():
            self.assertEqual((directory / name).read_bytes(), original)
        app.start()
        self.assertTrue(app.capture.ready.wait(1))
        self.assertEqual(app.state()['recorded_tag_height_m'], 1.4)
        self.assertNotEqual(app.directory, directory)
        app.stop()
        self.assertEqual(json.loads((app.directory / 'session.json').read_text(encoding='utf-8'))['tag_height_m'], 1.4)
        self.assertEqual((directory / 'session.json').read_bytes(), recorded_files['session.json'])

    def test_invalid_height_setting_cannot_change_or_persist_configuration(self):
        app = self.app()
        app.set_settings({'tag_height_m': 1.4})
        saved = (app.output_root / 'settings_current.json').read_bytes()
        for body in [{'tag_height_m': float('nan')}, {'tag_height_m': float('inf')},
                     {'tag_height_m': True}, {'tag_height_m': -1.},
                     {'tag_height_m': '1.4'}, {}, None]:
            with self.subTest(body=body), self.assertRaises(ValueError):
                app.set_settings(body)
            self.assertEqual(app.config['tag_height_m'], 1.4)
            self.assertEqual((app.output_root / 'settings_current.json').read_bytes(), saved)

    def test_route_and_marks_never_pull_solution_toward_known_waypoints(self):
        app = self.started()
        self.fill_ranges(app, x=2, y=3)
        self.one_compute_tick(app, 100.)
        first = app.solutions['wipro']
        self.assertTrue(first['ok'])
        self.assertAlmostEqual(first['x'], 2., places=6)
        self.assertAlmostEqual(first['y'], 3., places=6)
        app.mark(dict(waypoint_id='P1', client_event_id='arrival_001'))
        self.one_compute_tick(app, 100.)
        second = app.solutions['wipro']
        self.assertEqual((first['x'], first['y']), (second['x'], second['y']))
        self.assertGreater(abs(second['x']-app.route['waypoints'][0]['x']), 40)

    def test_expired_ranges_produce_no_current_fix_or_new_track_point(self):
        app = self.started()
        self.fill_ranges(app, received=100.)
        self.one_compute_tick(app, 100.)
        self.assertTrue(app.solutions['wipro']['ok'])
        trail_length = len(app.trails['wipro'])
        self.one_compute_tick(app, 105.)
        self.assertFalse(app.solutions['wipro']['ok'])
        self.assertEqual(len(app.trails['wipro']), trail_length)
        with patch.object(route.time, 'monotonic', return_value=105.):
            self.assertTrue(all(not link['valid_for_localisation'] for link in app.state()['links']))

    def test_negative_measurement_is_preserved_and_excluded_only_from_localization(self):
        app = self.started()
        self.fill_ranges(app)
        app.receive_event(dict(kind='range', peer=app.config['anchors'][0]['ap_mac'],
                               value=-4., unix_ms=1, monotonic_s=102., timestamp=2))
        self.one_compute_tick(app, 102.)
        self.assertEqual(app.observations['A1'][-1]['value'], -4.)
        self.assertEqual(app.solutions['wipro']['anchors_n'], 5)
        self.assertIn({'id': 'A1', 'reason': 'negative_range'},
                      app.solutions['wipro']['anchors_excluded'])

    def test_frozen_offset_changes_measurements_without_calibrating_against_marks(self):
        self.config['candidate'] = {'offset_to_subtract_m': 1.5}
        self.config['candidate_bytes'] = b'{"test_fixture_only":true}'
        app = self.started()
        self.fill_ranges(app)
        observed = []
        def solver(anchors, values, height):
            observed.append(dict(values))
            return dict(ok=False, anchors_used=[], reason='test')
        with patch.object(route, 'solve_position', side_effect=solver):
            self.one_compute_tick(app, 100.)
        # Frozen automatic and its aligned native control are also independent
        # LS calls; neither has completed FTM inputs in this fixture.
        self.assertEqual(len(observed), 4)
        self.assertEqual(observed[2:], [{}, {}])
        for key, value in observed[0].items():
            self.assertEqual(observed[1][key], value-1.5)

    def test_guarded_keeps_cycle_old_measurements_with_logged_weights_and_no_mark_input(self):
        import math
        app = self.started()
        self.fill_ranges(app,received=100.)
        for a in app.config['anchors']:
            distance = math.dist([2.,3.,1.22],[a[k] for k in ('x','y','z')])
            common = dict(monotonic_s=100.,available_monotonic_s=100.2,
                          prediction_ready_monotonic_s=100.3)
            app.automatic_observations[a['id']].append(dict(common,value=distance,
                native_value=distance+route.GUARDED_CONFIG['native_offset_m']))
            app.automatic_native_observations[a['id']].append(dict(common,
                value=distance+route.GUARDED_CONFIG['native_offset_m']))
        self.one_compute_tick(app,105.)
        self.assertFalse(app.solutions['wipro']['ok'])
        guarded = app.solutions['guarded']
        self.assertTrue(guarded['ok'])
        self.assertAlmostEqual(guarded['x'],2.,places=5)
        self.assertAlmostEqual(guarded['y'],3.,places=5)
        with patch.object(route.time,'monotonic',return_value=105.):
            state = app.state()
            app.mark(dict(waypoint_id='P1',client_event_id='guarded_arrival_001'))
        self.assertTrue(state['solutions']['guarded']['ok'])
        self.assertTrue(all(r['guarded_valid'] for r in state['links']))
        self.assertTrue(all(0 < r['guarded_weight'] < 1 for r in state['links']))
        self.one_compute_tick(app,105.)
        self.assertEqual(app.solutions['guarded']['x'],guarded['x'])
        self.assertEqual(app.solutions['guarded']['y'],guarded['y'])
        data = [json.loads(line) for line in (app.directory/'guarded_diagnostics.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(data[-1]['result']['ranges_used'],guarded['ranges_used'])
        recorded = list(csv.DictReader((app.directory/'track.csv').open(newline='')))
        self.assertEqual(json.loads(recorded[-1]['ranges_json']),guarded['ranges_used'])
        self.assertTrue((app.directory/'provenance/guarded_localization.py').exists())
        with patch.object(route.time,'monotonic',return_value=120.):
            self.assertFalse(app.state()['solutions']['guarded']['ok'])

    def test_stop_closes_files_and_records_final_ack_and_restart_uses_fresh_directory(self):
        app = self.started()
        directory = app.directory
        app.mark(dict(waypoint_id='P1', client_event_id='arrival_001'))
        state = app.stop()
        self.assertEqual(state['phase'], 'stopped')
        self.assertTrue(app.capture.closed)
        self.assertTrue(app.track_file.closed)
        self.assertTrue(app.mark_file.closed)
        self.assertTrue(app.guarded_file.closed)
        metadata_bytes = (directory / 'session.json').read_bytes()
        metadata = json.loads(metadata_bytes)
        self.assertTrue(metadata['final'])
        self.assertTrue(metadata['stop_mute_1_acknowledged'])
        self.assertFalse(metadata['solver']['route_or_marks_used_for_position'])
        self.assertIn('marks.csv', metadata['files'])
        app.start()
        self.assertTrue(app.capture.ready.wait(1))
        self.assertNotEqual(app.directory, directory)
        self.assertEqual((directory / 'session.json').read_bytes(), metadata_bytes)
        self.assertEqual(app.marks, [])

    def test_missing_stop_ack_is_reported_as_error_without_claiming_safe_stop(self):
        app = self.started(acknowledge=False)
        state = app.stop()
        self.assertEqual(state['phase'], 'error')
        self.assertFalse(state['stop_ack'])
        self.assertIn('mute 1', state['error'])
        self.assertFalse(json.loads((app.directory / 'session.json').read_text(encoding='utf-8'))['stop_mute_1_acknowledged'])

    def test_failed_recorder_construction_closes_files_and_allows_fresh_retry(self):
        app = self.app()
        original_factory = app.recorder_factory
        app.recorder_factory = Mock(side_effect=OSError('injected recorder construction failure'))
        with self.assertRaisesRegex(OSError, 'injected recorder'):
            app.start()
        failed_directory = app.directory
        self.assertEqual(app.phase, 'error')
        self.assertTrue(app.track_file.closed)
        self.assertTrue(app.mark_file.closed)
        self.assertIsNone(app.worker)
        self.assertIsNone(app.capture)
        metadata_bytes = (failed_directory / 'session.json').read_bytes()
        metadata = json.loads(metadata_bytes)
        self.assertFalse(metadata['final'])
        self.assertEqual(metadata['phase'], 'error')
        self.assertFalse(metadata['stop_mute_1_acknowledged'])
        self.assertIn('injected recorder construction failure', metadata['error'])
        app.recorder_factory = original_factory
        app.start()
        self.assertTrue(app.capture.ready.wait(1))
        self.assertEqual(app.phase, 'recording')
        self.assertNotEqual(app.directory, failed_directory)
        self.assertEqual((failed_directory / 'session.json').read_bytes(), metadata_bytes)


class CaptureGuardsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.events = []
        self.capture = route.RouteCapture('COM_FAKE', self.directory, OWNER,
            ['AA:BB:CC:DD:01:01'], 200, self.events.append, lambda: None)
        self.addCleanup(self.capture.close)

    def test_identity_must_be_full_correct_mac_and_preidentity_data_is_not_written(self):
        fields = ['AA:BB:CC:DD:01:01', '1', '2.5']
        self.capture._handle('RANGE', fields)
        self.assertEqual(self.capture.skipped_before_identity, 1)
        self.assertEqual(self.events, [])
        self.assertFalse((self.directory / 'range.csv').exists())
        with self.assertRaises(RuntimeError):
            self.capture._handle('MAC', ['24:EC:4A:03:47:A2'])
        self.assertEqual(self.capture.own_mac, 'UNKNOWN')

    def test_nonfinite_ranges_dropped_negative_finite_kept_and_unknown_target_fails(self):
        self.capture._handle('MAC', [OWNER])
        for value in ['nan', 'inf', '-inf']:
            self.capture._handle('RANGE', ['AA:BB:CC:DD:01:01', '1', value])
        self.assertEqual(self.capture.dropped['RANGE'], 3)
        self.assertEqual(self.events, [])
        self.capture._handle('RANGE', ['AA:BB:CC:DD:01:01', '2', '-2.5'])
        self.capture.close()
        self.assertEqual(self.events[0]['value'], -2.5)
        with (self.directory / 'range.csv').open(encoding='utf-8', newline='') as handle:
            row = list(csv.DictReader(handle))[0]
        self.assertEqual(float(row['range']), -2.5)
        self.assertEqual(row['own_mac'], OWNER)
        with self.assertRaises(RuntimeError):
            self.capture._handle('RANGE', ['AA:BB:CC:DD:02:01', '3', '2.5'])

    def test_stop_ack_must_follow_final_mute_command_and_serial_is_closed(self):
        device = Mock()
        self.capture._open = Mock(return_value=device)
        pumps = 0
        def pump(_):
            nonlocal pumps
            pumps += 1
            if pumps == 2:
                self.capture._handle('MAC', [OWNER])
            if self.capture.sent_commands and self.capture.sent_commands[-1]['command'] == 'channel':
                self.capture.text_tail += 'Current channel: 6 (40MHz)\n'
            if self.capture.sent_commands and self.capture.sent_commands[-1]['command'] == 'mute 1':
                # Only the first mute gets an ACK, never the final one.
                if sum(c['command'] == 'mute 1' for c in self.capture.sent_commands) == 1:
                    self.capture.text_tail += 'Mute: true\n'
        self.capture._pump = pump
        self.capture.on_ready = self.capture.stop.set
        with patch.object(route.time, 'sleep'):
            self.capture.run()
        self.assertIn('mute 0', [c['command'] for c in self.capture.sent_commands])
        self.assertEqual(self.capture.sent_commands[-1]['command'], 'mute 1')
        self.assertFalse(self.capture.stop_ack, 'Earlier ACK must not imply final stop ACK')
        device.close.assert_called_once()
        self.assertTrue(self.capture.raw.closed)

    def test_wrong_owner_never_unmutes_and_always_closes_serial(self):
        device = Mock()
        self.capture._open = Mock(return_value=device)
        def pump(_):
            self.capture._handle('MAC', ['24:EC:4A:04:73:D1'])
        self.capture._pump = pump
        with patch.object(route.time, 'sleep'):
            self.capture.run()
        self.assertNotIn('mute 0', [c['command'] for c in self.capture.sent_commands])
        self.assertIn('identity mismatch', self.capture.error)
        device.close.assert_called_once()
        self.assertTrue(self.capture.raw.closed)

    def test_startup_partial_csi_resynchronises_to_split_mac_without_losing_raw_bytes(self):
        # Actual failure mode: USB opens during CSI, the board reboots, and its
        # valid identity TLV is nested inside the incomplete old CSI frame.
        chunks = [b'\x02CSI\x011f\x019\x01incomplete_payload',
                  b'\r\nESP-ROM boot log\r\n\x02MA',
                  b'C\x01' + OWNER.encode('ascii') + b'\x03']
        self.capture.device = Mock(read=Mock(side_effect=chunks))
        self.capture.raw = io.BytesIO()
        self.capture.raw_chunks = io.StringIO()
        with patch.object(route.time, 'monotonic', side_effect=[0., .1, .2, .3, .4, .5, .6, .7, 1.1]):
            self.capture._pump(1.)
        self.assertEqual(self.capture.own_mac, OWNER)
        self.assertEqual(self.capture.raw.getvalue(), b''.join(chunks))
        logged = [json.loads(line) for line in self.capture.raw_chunks.getvalue().splitlines()]
        self.assertEqual(logged[-1]['end_offset'], len(b''.join(chunks)))
        self.assertEqual(self.capture.dropped['TLV_RESYNC'], 1)
        self.assertEqual(self.capture.counts['MAC'], 1)
        self.assertEqual(self.events, [])
        self.assertEqual(self.capture.buffer, bytearray())

    def test_corrupt_identity_frame_can_be_retried_but_valid_foreign_identity_still_fails(self):
        self.capture._handle('MAC', ['24:EC:4A:broken'])
        self.assertEqual(self.capture.dropped['MAC'], 1)
        self.assertEqual(self.capture.own_mac, 'UNKNOWN')
        self.capture._handle('MAC', [OWNER])
        self.assertEqual(self.capture.own_mac, OWNER)
        with self.assertRaises(RuntimeError):
            self.capture._handle('MAC', ['24:EC:4A:04:73:D1'])


class HttpBoundaryTests(unittest.TestCase):
    """Invoke handlers with fake streams; no HTTP server/socket is opened."""
    def request(self, headers=None, body=b'{}', path='/api/start', method='POST'):
        handler = object.__new__(route.RouteHandler)
        app = Mock()
        app.start.return_value = {'phase': 'starting'}
        app.state.return_value = {'phase': 'ready'}
        handler.server = Mock(app=app, server_port=8765, token='test-token')
        handler.headers = {'Host': '127.0.0.1:8765', 'Origin': 'http://127.0.0.1:8765',
            'X-Route-Token': 'test-token', 'Content-Length': str(len(body))}
        handler.headers.update(headers or {})
        handler.rfile, handler.path = io.BytesIO(body), path
        handler._respond = Mock()
        getattr(handler, 'do_' + method)()
        return app, handler._respond.call_args.args

    def test_same_origin_token_request_succeeds(self):
        app, response = self.request()
        self.assertEqual(response[0], 200)
        app.start.assert_called_once()

    def test_foreign_origin_dns_host_or_missing_token_cannot_start_capture(self):
        for headers in [{'Origin': 'https://example.org'}, {'Host': 'example.org:8765'},
                        {'X-Route-Token': ''}, {'Origin': ''}]:
            with self.subTest(headers=headers):
                app, response = self.request(headers=headers)
                self.assertEqual(response[0], 403)
                app.start.assert_not_called()

    def test_nonfinite_json_oversize_body_or_bad_content_length_never_start(self):
        cases = [({}, b'{"x":NaN}'), ({'Content-Length': '65537'}, b'{}'),
                 ({'Content-Length': '-1'}, b'{}'), ({'Content-Length': 'oops'}, b'{}')]
        for headers, body in cases:
            with self.subTest(headers=headers, body=body):
                app, response = self.request(headers=headers, body=body)
                self.assertEqual(response[0], 400)
                app.start.assert_not_called()

    def test_rebinding_host_cannot_read_state_or_token(self):
        for path in ['/api/state', '/api/token']:
            with self.subTest(path=path):
                app, response = self.request(headers={'Host': 'evil.example:8765'}, path=path, method='GET')
                self.assertEqual(response[0], 403)
                app.state.assert_not_called()


if __name__ == '__main__':
    unittest.main()
