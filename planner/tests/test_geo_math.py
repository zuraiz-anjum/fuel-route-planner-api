import math

from django.test import SimpleTestCase

from planner.services.geo_math import EARTH_RADIUS_MILES, haversine_miles


class HaversineMilesTests(SimpleTestCase):
    def test_zero_distance_for_identical_points(self):
        self.assertAlmostEqual(haversine_miles(40.0, -90.0, 40.0, -90.0), 0.0, places=6)

    def test_one_degree_of_longitude_at_the_equator(self):
        # At the equator, one degree of longitude spans (radius * pi/180) miles exactly.
        expected = EARTH_RADIUS_MILES * math.radians(1.0)
        self.assertAlmostEqual(haversine_miles(0.0, 0.0, 0.0, 1.0), expected, places=6)

    def test_is_symmetric(self):
        a_to_b = haversine_miles(40.0, -90.0, 41.0, -89.0)
        b_to_a = haversine_miles(41.0, -89.0, 40.0, -90.0)
        self.assertAlmostEqual(a_to_b, b_to_a, places=9)

    def test_vectorized_pairwise_matrix_matches_scalar_calls(self):
        import numpy as np

        lats_a = np.array([40.0, 41.0])
        lngs_a = np.array([-90.0, -89.0])
        lats_b = np.array([42.0, 43.0, 44.0])
        lngs_b = np.array([-88.0, -87.0, -86.0])

        matrix = haversine_miles(lats_a[:, None], lngs_a[:, None], lats_b[None, :], lngs_b[None, :])
        self.assertEqual(matrix.shape, (2, 3))
        for i in range(2):
            for j in range(3):
                expected = haversine_miles(lats_a[i], lngs_a[i], lats_b[j], lngs_b[j])
                self.assertAlmostEqual(float(matrix[i, j]), float(expected), places=6)
