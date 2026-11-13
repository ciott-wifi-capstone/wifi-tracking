from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

MODULE_DIR = Path(__file__).parents[1] / "host_tools" / "multi_anchor_3d"
sys.path.insert(0, str(MODULE_DIR))

import fused_3d_mapper as mapper
from fit_range_correction import fit_anchor
from range_correction import RangeCorrectionModel


class RangeCorrectionTests(unittest.TestCase):
    def test_piecewise_prediction_and_shadow_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            model_path = Path(directory) / "model.json"
            model_path.write_text(json.dumps({
                "trained": True,
                "anchors": {"1": {
                    "raw_knots_m": [2.0, 4.0, 6.0],
                    "delta_knots_m": [-0.2, -0.6, -1.0],
                }},
            }), encoding="utf-8")
            config = {"range_correction": {"mode": "shadow", "model_path": str(model_path)}}
            model = RangeCorrectionModel(config, MODULE_DIR)
            candidate, delta, available = model.predict("1", 5.0)
            self.assertTrue(available)
            self.assertAlmostEqual(candidate, 4.2)
            self.assertAlmostEqual(delta, -0.8)

    def test_online_shadow_does_not_change_solver_distance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model_path = root / "model.json"
            model_path.write_text(json.dumps({
                "trained": True,
                "anchors": {"1": {
                    "raw_knots_m": [1.0, 10.0],
                    "delta_knots_m": [-0.5, -0.5],
                }},
            }), encoding="utf-8")
            config = {
                "range_correction": {"mode": "shadow", "model_path": str(model_path)},
                "room_bounds_m": {"min": [-10, -10, -2], "max": [10, 10, 5]},
                "anchors": {"1": {"x_m": 0, "y_m": 0, "z_m": 0}},
            }
            system = mapper.OnlineSystem(config, root / "output")
            system.ingest({
                "round": 1, "anchor": "1", "status": 0, "rtt_raw_ns": 30,
                "dist_est_cm": 500, "corrected_cm": 500, "filtered_cm": 500,
                "ftm_rssi": -50, "entries": 16, "csi_packets": 8,
                "csi_rssi_mean": -50, "csi_rssi_std": 1, "csi_power_mean": 900,
                "csi_power_std": 10, "csi_roughness": 0.2, "csi_age_ms": 10,
                "bssid": "aa:bb:cc:dd:ee:ff", "pc_monotonic": 10.0,
            })
            measurement = system.links["1"].latest
            self.assertAlmostEqual(measurement.distance_m, 5.0)
            self.assertAlmostEqual(measurement.model_corrected_m, 4.5)
            self.assertFalse(measurement.correction_applied)
            system.close()

    def test_fitter_reduces_consistent_bias(self):
        points = [{"measured": value + 0.7, "truth": value, "weight": 100}
                  for value in [1, 2, 3, 4, 5, 6]]
        result = fit_anchor(points, max_knots=10)
        self.assertLess(result["training_fitted_mae_m"], 0.01)
        self.assertGreater(result["training_raw_mae_m"], 0.69)


if __name__ == "__main__":
    unittest.main()
