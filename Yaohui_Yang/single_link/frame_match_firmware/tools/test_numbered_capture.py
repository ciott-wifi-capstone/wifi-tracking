"""Check that per-round Anchor identities survive recording without changing FTM."""
import argparse
import csv
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from record_serial_2min import parse_measurement, run_capture
from flash_numbered import commands


class NumberedCaptureTests(unittest.TestCase):
    def test_source_recorded_with_original_ftm_fields(self):
        lines = iter([
            b'ROUND_START,17,1000\n',
            b'LINK_ID,17,1010,A6,02:10:5b:f5:6c:ee,6\n',
            b'FTM,17,1300,0,17,17,250,32,-45.0\n',
            b'ROUND_END,17,1400\n',
        ])
        class Serial:
            def __init__(self, *args, **kwargs): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def readline(self): return next(lines)
        with tempfile.TemporaryDirectory() as temp:
            args = argparse.Namespace(output=temp, trial='A6_2m_R1', condition='LOS', distance_m=2.,
                                      blocker='none', notes='', baud=115200, port='TEST', seconds=0, rounds=1)
            with patch.dict('sys.modules', {'serial': types.SimpleNamespace(Serial=Serial)}):
                run_capture(args)
            root = Path(temp) / args.trial
            def read(name):
                with (root / name).open(encoding='utf-8-sig', newline='') as f:
                    return list(csv.DictReader(f))
            link, ftm = read('round_links.csv')[0], read('ftm.csv')[0]
            self.assertEqual(link['anchor_id'], 'A6')
            self.assertEqual(link['bssid'], '02:10:5b:f5:6c:ee')
            self.assertEqual(link['round_id'], ftm['round_id'])
            self.assertEqual(float(ftm['estimated_distance_m']), 2.5)
            self.assertEqual(float(ftm['absolute_error_m']), .5)

    def test_malformed_source_is_not_accepted(self):
        for line in ['LINK_ID,1,10,A9,02:10:5b:f5:6c:ee,6',
                     'LINK_ID,1,10,A1,zz:ec:4a:03:47:a1,6',
                     'LINK_ID,1,10,A1,24:ec:4a,6']:
            with self.assertRaises(ValueError):
                parse_measurement(line, 2.)

    def test_roles_and_ids_use_independent_builds_and_uart_defaults(self):
        builds = set()
        configs = set()
        for role in ['anchor', 'tag']:
            for number in range(1, 7):
                cwd, calls = commands(Path('/project'), role, f'A{number}', 'COM11', '/idf')
                self.assertEqual(cwd, Path('/project') / role)
                build = calls[0][calls[0].index('-B') + 1]
                config = next(x for x in calls[0] if x.startswith('-DSDKCONFIG='))
                self.assertNotIn(build, builds)
                self.assertNotIn(config, configs)
                builds.add(build)
                configs.add(config)
                self.assertIn(f'-DLAB_ANCHOR_NUMBER={number}', calls[0])
                self.assertTrue(any(x.endswith('sdkconfig.numbered_uart.defaults') for x in calls[0]))
                self.assertEqual(calls[1][-4:], ['-p', 'COM11', 'flash', 'monitor'])


if __name__ == '__main__':
    unittest.main()
