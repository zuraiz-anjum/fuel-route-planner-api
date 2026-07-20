"""Tests for the orchestration layer: whole-plan caching (including that a
reimport invalidates it) and the post-geocode same-location check."""

import threading
import time
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APITransactionTestCase

from planner.exceptions import RoutingError, SameLocationError
from planner.services import route_planner as route_planner_module
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
        # The route-plan cache used to have no relationship at all to the
        # underlying station data, so a price
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
        # The API-level "start != finish" check was a naive string
        # comparison and completely missed this --
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


class FailedComputationCachingTests(TestCase):
    """A failure now gets cached too, briefly -- see route_planner.py's
    _FAILURE_CACHE_TTL_SECONDS. Before this, only successes were cached, so
    a concurrent request that lost the compute-lock race and then found the
    winner had failed would just repeat the whole (also-failing) pipeline
    itself instead of reusing the answer, doing the same wasted work twice."""

    def setUp(self):
        cache.clear()

    def test_a_second_call_reuses_a_recently_cached_failure_instead_of_recomputing(self):
        geocode_patch = patch(
            "planner.services.geocoding.geocode_location",
            side_effect=lambda query: {"Start, IL": START, "Finish, IL": FINISH}[query],
        )
        route_patch = patch(
            "planner.services.routing.get_route", side_effect=RoutingError("simulated OSRM outage")
        )
        with geocode_patch, route_patch as mocked_route:
            with self.assertRaises(RoutingError):
                compute_route_plan("Start, IL", "Finish, IL")
            with self.assertRaises(RoutingError):
                compute_route_plan("Start, IL", "Finish, IL")
        self.assertEqual(mocked_route.call_count, 1, "the second call should reuse the cached failure")


class ComputeLockConcurrencyTests(APITransactionTestCase):
    """Real-thread tests for the compute lock itself (not the HTTP layer --
    see test_api.py's ConcurrentIdenticalRequestsTests for that). Needs
    APITransactionTestCase, same reason as that class: real threads need to
    see each other's committed cache/DB state, which TestCase's per-test
    transaction wrapping doesn't allow."""

    def setUp(self):
        cache.clear()

    def test_concurrent_losers_fail_as_fast_as_the_winner_instead_of_waiting_out_the_full_lock_timeout(self):
        call_count = {"n": 0}
        call_lock = threading.Lock()

        def failing_get_route(*args, **kwargs):
            with call_lock:
                call_count["n"] += 1
            time.sleep(0.1)
            raise RoutingError("simulated OSRM outage")

        geocode_patch = patch(
            "planner.services.geocoding.geocode_location",
            side_effect=lambda query: {"Start, IL": START, "Finish, IL": FINISH}[query],
        )
        route_patch = patch("planner.services.routing.get_route", side_effect=failing_get_route)

        elapsed = {}

        def call(name, delay):
            time.sleep(delay)
            start = time.monotonic()
            try:
                compute_route_plan("Start, IL", "Finish, IL")
            except RoutingError:
                pass
            elapsed[name] = time.monotonic() - start

        with geocode_patch, route_patch:
            t1 = threading.Thread(target=call, args=("winner", 0.0))
            t2 = threading.Thread(target=call, args=("loser", 0.02))
            t1.start(); t2.start()
            t1.join(); t2.join()

        # Before the fix, "loser" would sit through the full ~10s lock-wait
        # timeout before giving up and failing on its own. It should now
        # fail almost as fast as the winner did.
        self.assertLess(elapsed["loser"], 2.0, elapsed)

    def test_a_lock_released_after_its_ttl_expired_does_not_steal_a_later_holders_lock(self):
        # The lock used to release unconditionally (cache.delete(lock_key),
        # no ownership check). If a
        # computation outlived the lock's TTL, a second request could
        # legitimately acquire the (expired) lock and start its own
        # computation -- but then the FIRST request's cleanup would delete
        # the SECOND request's still-in-use lock, letting a THIRD request
        # steal it too, and so on with no bound. The fix (a per-acquisition
        # token, checked before deleting) can't stop the TTL from expiring
        # mid-computation, but it does stop that expiry from cascading past
        # the one request that legitimately raced in.
        original_ttl = route_planner_module._COMPUTE_LOCK_TTL_SECONDS
        route_planner_module._COMPUTE_LOCK_TTL_SECONDS = 1
        try:
            call_times = []
            call_lock = threading.Lock()

            def slow_get_route(*args, **kwargs):
                with call_lock:
                    call_times.append(time.monotonic())
                time.sleep(1.6)
                return RouteResult(distance_miles=400.0, duration_seconds=3600.0, geometry=STRAIGHT_LINE_GEOMETRY)

            geocode_patch = patch(
                "planner.services.geocoding.geocode_location",
                side_effect=lambda query: {"Start, IL": START, "Finish, IL": FINISH}[query],
            )
            route_patch = patch("planner.services.routing.get_route", side_effect=slow_get_route)

            def call(delay):
                time.sleep(delay)
                compute_route_plan("Start, IL", "Finish, IL")

            with geocode_patch, route_patch:
                threads = [
                    threading.Thread(target=call, args=(0.0,)),   # holds the lock past its 1s TTL
                    threading.Thread(target=call, args=(1.2,)),   # TTL already expired -- legitimately steals it
                    threading.Thread(target=call, args=(1.9,)),   # arrives after thread 1 cleans up
                ]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()

            self.assertEqual(
                len(call_times), 2,
                "one legitimate steal after TTL expiry is expected; a third call means the lock got "
                "stolen out from under an in-progress holder a second time",
            )
        finally:
            route_planner_module._COMPUTE_LOCK_TTL_SECONDS = original_ttl
