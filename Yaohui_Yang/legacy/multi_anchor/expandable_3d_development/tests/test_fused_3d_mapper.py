from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

MODULE_DIR = Path(__file__).parents[1] / "host_tools" / "multi_anchor_3d"
sys.path.insert(0, str(MODULE_DIR))
import fused_3d_mapper as mapper


class MapperTests(unittest.TestCase):
    def test_parse_schema_one(self):
        line = ("MFUSED,1,7,123456,2,0,22,315,320,318,-55,16,9,-52.2,1.4,"
                "901.0,12.0,0.34,1,18,10,1,aa:bb:cc:dd:ee:ff,-54")
        parsed = mapper.parse_mfused(line, now=10.0)
        self.assertEqual(parsed["anchor"], "2")
        self.assertEqual(parsed["filtered_cm"], 318)
        self.assertEqual(parsed["csi_packets"], 9)

    def test_clean_3d_solution(self):
        anchors = {
            "1": {"x_m": 0, "y_m": 0, "z_m": 0},
            "2": {"x_m": 6, "y_m": 0, "z_m": 2},
            "3": {"x_m": 6, "y_m": 5, "z_m": 0},
            "4": {"x_m": 0, "y_m": 5, "z_m": 2},
            "5": {"x_m": 3, "y_m": 2.5, "z_m": 3},
            "6": {"x_m": 1, "y_m": 4, "z_m": 1},
            "7": {"x_m": 5, "y_m": 1, "z_m": 2.5},
        }
        truth = np.asarray([2.1, 1.7, 1.2])
        links = {}
        for aid, cfg in anchors.items():
            distance = float(np.linalg.norm(truth - mapper.anchor_vector(cfg)))
            measurement = mapper.Measurement(1, 10.0, distance, -50, 16, 8,
                                             -50, 1, 900, 10, 0.2, 10, aid)
            links[aid] = mapper.LinkState()
            links[aid].update(measurement)
        result = mapper.solve_position(
            anchors, links, 3.0, (np.zeros(3), np.asarray([6, 5, 3])), None, now=10.0)
        self.assertIsNotNone(result)
        self.assertLess(float(np.linalg.norm(result["position"] - truth)), 0.02)

    def test_fixed_height_2d_solution(self):
        anchors = {
            "1": {"x_m": 0, "y_m": 0, "z_m": 0.4},
            "2": {"x_m": 6, "y_m": 0, "z_m": 2.4},
            "3": {"x_m": 6, "y_m": 5, "z_m": 0.4},
            "5": {"x_m": 0, "y_m": 5, "z_m": 2.4},
        }
        truth = np.asarray([2.1, 1.7, 1.15])
        links = {}
        for aid, cfg in anchors.items():
            distance = float(np.linalg.norm(truth - mapper.anchor_vector(cfg)))
            measurement = mapper.Measurement(1, 10.0, distance, -50, 16, 8,
                                             -50, 1, 900, 10, 0.2, 10, aid)
            links[aid] = mapper.LinkState()
            links[aid].update(measurement)
        result = mapper.solve_position(
            anchors, links, 3.0, (np.zeros(3), np.asarray([6, 5, 3])), None,
            now=10.0, fixed_z_m=truth[2])
        self.assertIsNotNone(result)
        self.assertEqual(result["positioning_mode"], "2d_fixed_z")
        self.assertAlmostEqual(result["position"][2], truth[2], places=9)
        self.assertLess(float(np.linalg.norm(result["position"][:2] - truth[:2])), 0.02)

    def test_simulation_writes_state(self):
        config = {
            "max_measurement_age_s": 3, "voxel_size_m": 0.5,
            "room_bounds_m": {"min": [0, 0, 0], "max": [6, 5, 3]},
            "anchors": {
                "1": {"x_m": 0, "y_m": 0, "z_m": .4},
                "2": {"x_m": 6, "y_m": 0, "z_m": 2.4},
                "3": {"x_m": 6, "y_m": 5, "z_m": .4},
                "4": {"x_m": 0, "y_m": 5, "z_m": 2.4},
                "5": {"x_m": 3, "y_m": 2.5, "z_m": 2.9},
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            system = mapper.OnlineSystem(config, Path(directory))
            mapper.simulate(system, rounds=5)
            self.assertTrue((Path(directory) / "latest_state.json").exists())
            self.assertTrue((Path(directory) / "radio_voxels.csv").exists())
            system.close()


if __name__ == "__main__":
    unittest.main()
