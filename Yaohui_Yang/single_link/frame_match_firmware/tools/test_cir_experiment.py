"""Tests for plan import, data separation, freezing and controlled final testing.

Synthetic features below test software control flow, not ranging performance.
They are never included in the supplied measurements or evaluation reports.
"""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import cir_experiment as experiment
from cir_core import CIR_FEATURES


def toy_partition(split):
    rows = []
    for distance in [1.0, 2.0, 3.0]:
        for i in range(12):
            row = {'trial': f'{split}_{distance}', 'round_id': i,
                   'true_distance_m': distance, 'nominal_distance_m': distance,
                   'capture_fingerprint': f'synthetic_{split}_{distance}',
                   'ftm_distance_m': 2 * distance + 3 + 0.01 * i,
                   'ftm_frame_median_m': 2 * distance + 3.1,
                   'ftm_mad_m': 0.1, 'ftm_rssi_dbm': -20 - 5 * distance,
                   'paired_cir_frames': 10}
            row.update({name: distance * (j + 1) / 20 + i * 0.0001
                        for j, name in enumerate(CIR_FEATURES)})
            rows.append(row)
    return rows, [{'trial': split, 'true_distance_m': 2.0, 'usable_sessions': len(rows),
                   'sessions': len(rows), 'test_fixture_only': True}]


class ExperimentTests(unittest.TestCase):
    def test_real_import_reuses_only_training_and_resume_preserves_plan(self):
        if not all((experiment.PROJECT / 'example_data' / f'V5_LOS_{i}m_A_R1.zip').is_file()
                   for i in (1, 2, 3)):
            self.skipTest('Requires the three original private training captures.')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'experiment'
            args = SimpleNamespace(root=str(root), distances=[1, 2, 3, 4, 6, 9],
                                   extra_test_distances=[1.5, 2.5, 5], seconds=120,
                                   reuse_existing=True, notes='unit-test import')
            with contextlib.redirect_stdout(io.StringIO()):
                experiment.initialize(args)
            plan = experiment.load_plan(root)
            self.assertEqual(len(plan['slots']), 21)
            complete = [s for s in plan['slots'] if s['status'] == 'complete']
            self.assertEqual(len(complete), 3)
            self.assertEqual({s['split'] for s in complete}, {'train'})
            self.assertEqual({s['nominal_distance_m'] for s in complete}, {1.0, 2.0, 3.0})
            before = (root / 'experiment.json').read_bytes()
            with self.assertRaisesRegex(ValueError, 'already exists'):
                experiment.initialize(args)
            self.assertEqual(before, (root / 'experiment.json').read_bytes())
            with self.assertRaisesRegex(ValueError, 'freeze'):
                experiment.collect_partition(SimpleNamespace(root=str(root), split='test',
                                                             port='VIRTUAL', baud=115200))

    def test_fit_never_reads_test_and_evaluate_never_refits(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            experiment.save_plan(root, {'schema': 'qar_multi_capture_v1', 'slots': []})
            calls = []

            def mocked_partition(root, plan, split):
                calls.append(split)
                return toy_partition(split)

            args = SimpleNamespace(root=str(root))
            with patch.object(experiment, 'load_partition', side_effect=mocked_partition), \
                    contextlib.redirect_stdout(io.StringIO()):
                experiment.fit(args)
            self.assertEqual(calls, ['train', 'validation'])
            selection = json.loads((root / 'fit/selection.json').read_text())
            self.assertFalse(selection['test_used_for_selection'])
            self.assertFalse(selection['refit_on_validation'])
            calls.clear()
            with patch.object(experiment, 'load_partition', side_effect=mocked_partition), \
                    patch.object(experiment, 'fit_model', side_effect=AssertionError('No refitting on test')), \
                    contextlib.redirect_stdout(io.StringIO()):
                experiment.evaluate(args)
            self.assertEqual(calls, ['test'])
            self.assertTrue((root / 'fit/test_by_distance.csv').exists())
            model = root / 'fit/models/cir_direct.json'
            model.write_text(model.read_text() + ' ', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'changed'):
                experiment.verify_frozen(root)
            with self.assertRaisesRegex(ValueError, 'changed'):
                experiment.collect_partition(SimpleNamespace(root=str(root), split='test',
                                                             port='VIRTUAL', baud=115200))

    def test_relabelled_copy_does_not_become_an_independent_capture(self):
        a, _ = toy_partition('train')
        b, _ = toy_partition('validation')
        b[0]['capture_fingerprint'] = a[0]['capture_fingerprint']
        with self.assertRaisesRegex(ValueError, 'Identical capture bytes'):
            experiment.assert_disjoint(a, b)

    def test_incomplete_partitions_cannot_be_fitted_as_complete(self):
        plan = {'slots': [{'id': 'train_A_1m_R1', 'split': 'train', 'status': 'pending'}]}
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            experiment.load_partition(Path('.'), plan, 'train')


if __name__ == '__main__':
    unittest.main()
