"""Tests for the orchestration layer: whole-plan caching (including that a
reimport invalidates it) and the post-geocode same-location check."""

from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase

from planner.exceptions import SameLocationError
from planner.services.geocoding import Coordinates
from planner.services.route_planner import compute_route_plan
from planner.services.routing import RouteResult
from stations.data_version import invalidate_data_version_cache
from stations.models import DataImportLog

START = Coordinates(latitude=40.0, longitude=-95.0)
FINISH = Coordinates(latitude=40.0, longitude=-85.0)
STRAIGHT_LINE_GEOMETRY = [(40.0, -95.0 + i * 0.1) for i in range(101)]  # ~530mi of raw polyline


def _patched_pipeline():
    geocode_patch = patch(
        "planner.services.geocoding.geocode_location",
        side_effect=lambda query: {"Start, IL": START, "Finish, IL": FINISH}[query],
    )
    route_patch = patch(
        "planner.services.routing.get_route",
        # distance_miles is what fuel_optimizer treats as the trip length,
        # independent of the raw geometry's length -- kept under the
        # default 500mi vehicle range so these caching-focused tests don't
        # need any Station rows in the DB to be feasible.
        return_value=RouteResult(distance_miles=400.0, duration_seconds=3600.0, geometry=STRAIGHT_LINE_GEOMETRY),
    )
    return geocode_patch, route_patch


class ComputeRoutePlanCachingTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_repeat_request_is_served_from_cache_without_calling_routing_again(self):
        geocode_patch, route_patch = _patched_pipeline()
        with geocode_patch, route_patch as mocked_route:
            compute_route_plan("Start, IL", "Finish, IL")
            compute_route_plan("Start, IL", "Finish, IL")
        self.assertEqual(mocked_route.call_count, 1)

    def test_reimporting_fuel_prices_invalidates_the_cache_for_the_same_trip(self):
        # Regression test for a real bug: the route-plan cache used to have
        # no relationship at all to the underlying station data, so a price
        # update from re-running import_fuel_prices was silently invisible
        # to anyone requesting an already-cached trip for up to
        # ROUTE_CACHE_TTL_SECONDS (an hour, by default). The cache key now
        # folds in the latest DataImportLog timestamp, so a reimport must
        # result in a fresh computation (a second routing call), not a
        # stale cache hit.
        geocode_patch, route_patch = _patched_pipeline()
        with geocode_patch, route_patch as mocked_route:
            compute_route_plan("Start, IL", "Finish, IL")
            self.assertEqual(mocked_route.call_count, 1)

            # Simulates a reimport completing: import_fuel_prices creates
            # the log row AND proactively clears the cached data-version
            # (data_version.py caches that lookup for a few seconds so a
            # plain cache *hit* doesn't cost a DB query on every request --
            # see its docstring). A test that only does the first half
            # would still see the stale cached version for a few seconds,
            # which is exactly why the real command does both.
            DataImportLog.objects.create(station_count=1)
            invalidate_data_version_cache()

            compute_route_plan("Start, IL", "Finish, IL")
            self.assertEqual(mocked_route.call_count, 2, "a reimport must invalidate the old cache entry")

    def test_different_vehicle_params_are_cached_independently(self):
        geocode_patch, route_patch = _patched_pipeline()
        with geocode_patch, route_patch as mocked_route:
            compute_route_plan("Start, IL", "Finish, IL", mpg=10)
            compute_route_plan("Start, IL", "Finish, IL", mpg=12)
        self.assertEqual(mocked_route.call_count, 2)


class SameLocationValidationTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_rejects_start_and_finish_that_geocode_to_the_same_point_even_if_worded_differently(self):
        # Regression test for a real bug: the API-level "start != finish"
        # check was a naive string comparison and completely missed this --
        # "Chicago, IL" and "Chicago, Illinois" sailed straight through as a
        # "valid" 201-with-$0-cost trip. This checks resolved *coordinates*.
        same_point = Coordinates(latitude=41.8373, longitude=-87.6861)
        with patch(
            "planner.services.geocoding.geocode_location",
            side_effect=lambda query: same_point,
        ):
            with self.assertRaises(SameLocationError):
                compute_route_plan("Chicago, IL", "Chicago, Illinois")

    def test_allows_two_distinct_nearby_but_different_towns(self):
        nearby_but_distinct = {
            "Town A": Coordinates(latitude=40.0, longitude=-95.0),
            "Town B": Coordinates(latitude=40.05, longitude=-95.05),  # a few miles away, not the same place
        }
        geocode_patch = patch(
            "planner.services.geocoding.geocode_location", side_effect=lambda q: nearby_but_distinct[q]
        )
        route_patch = patch(
            "planner.services.routing.get_route",
            return_value=RouteResult(distance_miles=5.0, duration_seconds=600.0, geometry=STRAIGHT_LINE_GEOMETRY),
        )
        with geocode_patch, route_patch:
            result = compute_route_plan("Town A", "Town B")
        self.assertEqual(result.distance_miles, 5.0)
