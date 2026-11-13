from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

MODULE_DIR = Path(__file__).parents[1] / "host_tools" / "ftm_csi_3d"
sys.path.insert(0, str(MODULE_DIR))
import fast_dashboard as dashboard


class FastDashboardGeometryTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "display_reference": "lowest_z_anchor",
            "anchors": {
                "1": {"x_m": 1.3, "y_m": 0.1, "z_m": 1.35},
                "2": {"x_m": 0.64, "y_m": -0.96, "z_m": 2.14},
                "3": {"x_m": 0.0, "y_m": 0.0, "z_m": 0.0},
                "4": {"x_m": -1.32, "y_m": 0.77, "z_m": 1.32},
                "5": {"x_m": -1.1, "y_m": -2.54, "z_m": 0.52},
            },
        }

    def test_lowest_anchor_is_display_origin(self):
        reference, origin, positions = dashboard.display_frame(self.config)
        self.assertEqual(reference, "3")
        np.testing.assert_allclose(origin, [0.0, 0.0, 0.0])
        np.testing.assert_allclose(positions["3"], [0.0, 0.0, 0.0])

    def test_view_contains_all_anchors_with_padding(self):
        _, _, positions = dashboard.display_frame(self.config)
        lower, upper, _center, span = dashboard.anchor_view_bounds(positions)
        points = np.vstack(list(positions.values()))
        self.assertTrue(np.all(points > lower))
        self.assertTrue(np.all(points < upper))
        self.assertTrue(np.all(span > 0.0))


if __name__ == "__main__":
    unittest.main()
