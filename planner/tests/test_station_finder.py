import numpy as np
from django.test import TestCase

from planner.services.geometry import RoutePath
from planner.services.station_finder import find_stations_near_route
from stations.models import Station


class FindStationsNearRouteTests(TestCase):
    def setUp(self):
        # A straight route along latitude 40, from lng -90.0 to -89.0.
        n = 50
        lats = np.full(n, 40.0)
        lngs = np.linspace(-90.0, -89.0, n)
        # Cumulative distance recomputed properly rather than assumed, so
        # this fixture stays correct if anything above changes.
        from planner.services.geo_math import haversine_miles

        segment_miles = haversine_miles(lats[:-1], lngs[:-1], lats[1:], lngs[1:])
        cumulative = np.concatenate(([0.0], np.cumsum(segment_miles)))
        self.route_path = RoutePath(latitudes=lats, longitudes=lngs, cumulative_miles=cumulative)

        self.near_station = Station.objects.create(
            opis_id=1, name="Near", city="X", state="IL",
            price_per_gallon="3.00", latitude=40.0, longitude=-89.5,
        )
        self.far_station = Station.objects.create(
            opis_id=2, name="Far", city="Y", state="IL",
            price_per_gallon="3.00", latitude=45.0, longitude=-89.5,
        )
        self.earlier_station = Station.objects.create(
            opis_id=3, name="Earlier", city="X", state="IL",
            price_per_gallon="3.00", latitude=40.0, longitude=-89.9,
        )
        # No coordinates at all -- must never show up as a candidate.
        Station.objects.create(
            opis_id=4, name="Ungeocoded", city="Z", state="IL", price_per_gallon="3.00",
        )

    def test_only_returns_stations_within_the_corridor(self):
        results = find_stations_near_route(self.route_path, corridor_miles=8)
        ids = {r.station.opis_id for r in results}
        self.assertIn(1, ids)
        self.assertIn(3, ids)
        self.assertNotIn(2, ids)
        self.assertNotIn(4, ids)

    def test_results_are_sorted_by_distance_along_route(self):
        results = find_stations_near_route(self.route_path, corridor_miles=8)
        positions = [r.distance_along_route_miles for r in results]
        self.assertEqual(positions, sorted(positions))
        # The station further west (closer to lng -90) should come first.
        self.assertEqual(results[0].station.opis_id, self.earlier_station.opis_id)

    def test_narrowing_the_corridor_excludes_more_distant_candidates(self):
        # Move a station just outside a very tight corridor.
        Station.objects.create(
            opis_id=5, name="Borderline", city="X", state="IL",
            price_per_gallon="3.00", latitude=40.2, longitude=-89.5,
        )
        wide = {r.station.opis_id for r in find_stations_near_route(self.route_path, corridor_miles=20)}
        narrow = {r.station.opis_id for r in find_stations_near_route(self.route_path, corridor_miles=1)}
        self.assertIn(5, wide)
        self.assertNotIn(5, narrow)
