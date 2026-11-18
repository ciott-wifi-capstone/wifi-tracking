"""Meaningful checks for preprocessing, label isolation and live/offline parity.

Run from project root: python -m unittest discover -s tools -p test_cir_pipeline.py -v
"""
import json
import contextlib
import csv
import io
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
import zipfile
from pathlib import Path
import numpy as np

from cir_core import (ACTIVE, CIR_FEATURES, FTM_FEATURES, UnsupportedCSI, decode_lltf20,
                      dense_cir, load_model, make_round_features, predict, reconstruct_missing)
from cir_io import SerialRoundAdapter, extract_trial, read_trials

ROOT = Path(__file__).resolve().parents[1]


class CIRTests(unittest.TestCase):
    def test_dense_ifft_preserves_frequency_order_and_delay(self):
        k = np.fft.fftfreq(64) * 64
        h = np.exp(-2j * np.pi * k * 3 / 64)
        response = dense_cir(h)
        self.assertEqual(int(np.argmax(np.abs(response))), 48)
        self.assertAlmostEqual(float(np.max(np.abs(response))), 1.0, places=12)

    def test_sparse_reconstruction_preserves_measured_bins(self):
        k = np.fft.fftfreq(64) * 64
        original = np.exp(-2j * np.pi * k * 0.3 / 64) + 0.35 * np.exp(-2j * np.pi * k * 2.4 / 64)
        measured = original.copy()
        measured[~ACTIVE] = 0
        recovered = reconstruct_missing(measured, ACTIVE)
        np.testing.assert_array_equal(recovered[ACTIVE], measured[ACTIVE])
        loss = lambda h: np.sum(np.sqrt(np.abs(np.fft.ifft(h, norm='ortho')) ** 2 + 0.02 ** 2))
        self.assertLess(float(loss(recovered)), float(loss(measured)))

    def test_iq_order_and_invalid_word(self):
        iq = np.zeros(128, dtype=np.int8)
        iq[0::2] = 30
        iq[1::2] = 40
        h, known = decode_lltf20(iq.tobytes().hex(), cwb=0, sig_mode=0, invalid_first=1)
        self.assertFalse(known[1])
        self.assertAlmostEqual(float(h[2].real), 0.8)
        self.assertAlmostEqual(float(h[2].imag), 0.6)
        with self.assertRaises(UnsupportedCSI):
            decode_lltf20(iq.tobytes().hex(), cwb=1, sig_mode=1)

    def test_live_offline_parity_and_no_truth_input(self):
        source = ROOT / 'example_data/V5_TIMESTAMP_01.zip'
        if not source.is_file() or not all((ROOT / 'models' / (kind + '.json')).is_file()
                                           for kind in ('cir_direct', 'cir_residual')):
            self.skipTest('Requires the original private replay and fitted models.')
        trial = next(read_trials([source]))
        offline, audit = extract_trial(trial)
        adapter = SerialRoundAdapter()
        online = []
        with zipfile.ZipFile(source) as archive:
            member = next(n for n in archive.namelist() if n.endswith('serial_raw.log'))
            for line in archive.read(member).decode('utf-8', errors='replace').splitlines():
                row = adapter.feed(line)
                if row:
                    online.append(row)
        last = adapter.finish()
        if last:
            online.append(last)
        self.assertEqual(len(online), len(offline))
        self.assertEqual(len(online), 15)
        reference = {r['round_id']: r for r in offline}
        for row in online:
            for key in FTM_FEATURES + CIR_FEATURES:
                self.assertAlmostEqual(row[key], reference[row['round_id']][key], places=10)
            self.assertNotIn('true_distance_m', row)
        for kind in ['cir_direct', 'cir_residual']:
            model = load_model(ROOT / 'models' / (kind + '.json'))
            self.assertNotIn('V5_TIMESTAMP_01', model['training_trials'])
            self.assertNotIn(audit['capture_fingerprint'], model['training_fingerprints'])
            altered = dict(offline[0], true_distance_m=9999, trial='unrelated', round_id=-1)
            a = predict(model, offline[0])['distance_m']
            b = predict(model, altered)['distance_m']
            self.assertEqual(a, b)

    def test_missing_cir_does_not_create_a_fitted_distance(self):
        adapter = SerialRoundAdapter()
        adapter.feed('ROUND_START,1,100')
        adapter.feed('FTM,1,200,0,20,20,300,0,-40')
        self.assertIsNone(adapter.feed('ROUND_END,1,250'))
        self.assertEqual(adapter.last_raw_ftm, 3.0)
        self.assertIn('Insufficient', adapter.last_error)

    def test_live_loop_saves_inference_without_truth_labels(self):
        from cir_distance import live
        if not (ROOT / 'example_data/V5_TIMESTAMP_01.zip').is_file() or not all(
            (ROOT / 'models' / (kind + '.json')).is_file()
            for kind in ('ftm_affine', 'cir_direct', 'cir_residual')
        ):
            self.skipTest('Requires the original private replay and fitted models.')
        with zipfile.ZipFile(ROOT / 'example_data/V5_TIMESTAMP_01.zip') as archive:
            member = next(n for n in archive.namelist() if n.endswith('serial_raw.log'))
            lines = archive.read(member).splitlines(keepends=True)

        class FakeSerial:
            def __init__(self, **kwargs):
                self.lines = iter(lines)
            def open(self):
                pass
            def close(self):
                pass
            def readline(self):
                try:
                    return next(self.lines)
                except StopIteration:
                    raise KeyboardInterrupt

        with tempfile.TemporaryDirectory() as folder:
            args = types.SimpleNamespace(models=str(ROOT / 'models'), out=str(Path(folder) / 'live'),
                                         port='VIRTUAL', baud=115200, seconds=0, primary='cir_direct')
            with patch.dict(sys.modules, {'serial': types.SimpleNamespace(Serial=FakeSerial)}), \
                    contextlib.redirect_stdout(io.StringIO()):
                live(args)
            with (Path(args.out) / 'predictions.csv').open(encoding='utf-8-sig') as file:
                rows = list(csv.DictReader(file))
            self.assertEqual(len(rows), 15)
            self.assertNotIn('true_distance_m', rows[0])
            self.assertEqual(rows[0]['primary_model'], 'cir_direct')
            self.assertAlmostEqual(float(rows[0]['raw_ftm_m']), 3.3)
            self.assertTrue(1.9 < float(rows[0]['distance_m']) < 2.2)


if __name__ == '__main__':
    unittest.main()
