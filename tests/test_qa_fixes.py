"""Regression tests for fixes from the browser QA passes (2026-11-23). No network."""

from __future__ import annotations

import unittest

from meridian import geo
from meridian.modules.hazards.common import clean, geometry_center, repair_mojibake
from meridian.modules.intel.convergence import kinds_phrase


class MojibakeTests(unittest.TestCase):
    def test_double_encoded_text_is_repaired(self):
        self.assertEqual(repair_mojibake("Capeâ€¦"), "Cape…")
        self.assertEqual(clean("Cape Cod â€“ coastal flood"), "Cape Cod – coastal flood")

    def test_correct_text_is_left_alone(self):
        for s in ("Honningsvåg", "São Paulo", "Ângelo", "Kakumäe", "plain ascii", "Ã alone"):
            self.assertEqual(repair_mojibake(s), s)


class InsidePointTests(unittest.TestCase):
    def test_point_lands_inside_a_concave_polygon(self):
        u_shape = {"type": "Polygon", "coordinates": [[[0, 0], [10, 0], [10, 10], [8, 10], [8, 2], [2, 2], [2, 10],
                                                        [0, 10], [0, 0]]]}
        lon, lat = geometry_center(u_shape)
        self.assertTrue(geo.point_in_geometry(lon, lat, u_shape))

    def test_convex_polygon_keeps_its_centroid(self):
        square = {"type": "Polygon", "coordinates": [[[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]]]}
        self.assertEqual(geometry_center(square), (2.0, 2.0))

    def test_multipolygon_uses_the_largest_part(self):
        mp = {"type": "MultiPolygon", "coordinates": [
            [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
            [[[10, 10], [20, 10], [20, 20], [10, 20], [10, 10]]]]}
        lon, lat = geometry_center(mp)
        self.assertTrue(10 < lon < 20 and 10 < lat < 20)


class ConvergenceWordingTests(unittest.TestCase):
    def test_kinds_read_as_plain_words(self):
        phrase = kinds_phrase(["conflict_event", "gps_jam", "earthquake"])
        self.assertEqual(phrase, "conflict events, GPS interference and earthquakes")
        self.assertNotIn("_", kinds_phrase(["sanctioned_vessel", "fire"]))


if __name__ == "__main__":
    unittest.main()
