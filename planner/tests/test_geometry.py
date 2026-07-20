import numpy as np
from django.test import SimpleTestCase

from planner.services.geo_math import haversine_miles
from planner.services.geometry import build_route_path


class BuildRoutePathTests(SimpleTestCase):
    def test_rejects_geometry_with_fewer_than_two_points(self):
        with self.assertRaises(ValueError):
            build_route_path([(40.0, -90.0)])

    def test_cumulative_distance_is_monotonic_and_starts_at_zero(self):
        geometry = [(40.0, -90.0 + i * 0.05) for i in range(20)]
        path = build_route_path(geometry)
        self.assertEqual(path.cumulative_miles[0], 0.0)
        self.assertTrue(bool(np.all(np.diff(path.cumulative_miles) >= 0)))

    def test_total_matches_sum_of_raw_segments_when_not_downsampled(self):
        geometry = [(40.0, -90.0), (40.0, -89.9), (40.0, -89.7), (41.0, -89.7)]
        path = build_route_path(geometry)
        lats = [p[0] for p in geometry]
        lngs = [p[1] for p in geometry]
        expected_total = sum(
            haversine_miles(lats[i], lngs[i], lats[i + 1], lngs[i + 1]) for i in range(len(geometry) - 1)
        )
        self.assertAlmostEqual(path.total_miles, expected_total, places=6)

    def test_downsampling_keeps_first_and_last_point_exact(self):
        geometry = [(40.0, -90.0 + i * 0.001) for i in range(3000)]
        path = build_route_path(geometry)
        self.assertLess(len(path.cumulative_miles), 3000)
        self.assertEqual(float(path.latitudes[0]), geometry[0][0])
        self.assertEqual(float(path.longitudes[0]), geometry[0][1])
        self.assertAlmostEqual(float(path.latitudes[-1]), geometry[-1][0], places=9)
        self.assertAlmostEqual(float(path.longitudes[-1]), geometry[-1][1], places=9)
