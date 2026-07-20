"""End-to-end API tests. External services (geocoding, routing) are mocked
so the whole suite runs offline and deterministically -- exact algorithm
correctness is covered in detail by test_fuel_optimizer.py; these tests
cover request/response wiring, persistence, and HTTP-level error handling.
"""

import threading
import time
from unittest.mock import patch

import numpy as np
from django.core.cache import cache
from django.db import IntegrityError
from rest_framework.test import APITestCase, APITransactionTestCase

from planner.exceptions import GeocodingError
from planner.models import RoutePlan
from planner.services.geocoding import Coordinates
from planner.services.routing import RouteResult
from stations.models import Station


def _straight_line_geometry(num_points=200, lat=40.0, lng_start=-95.0, lng_end=-85.0):
    lngs = np.linspace(lng_start, lng_end, num_points)
    return [(lat, float(l)) for l in lngs]


START_COORDS = Coordinates(latitude=40.0, longitude=-95.0)
FINISH_COORDS = Coordinates(latitude=40.0, longitude=-85.0)


class RoutePlanApiTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.geometry = _straight_line_geometry()

        cheap_point = self.geometry[50]
        pricey_point = self.geometry[150]
        self.cheap_station = Station.objects.create(
            opis_id=101, name="Cheap Stop", city="Testville", state="IL",
            price_per_gallon="2.500", latitude=cheap_point[0], longitude=cheap_point[1],
        )
        self.pricey_station = Station.objects.create(
            opis_id=102, name="Pricey Stop", city="Testburg", state="IL",
            price_per_gallon="4.500", latitude=pricey_point[0], longitude=pricey_point[1],
        )

    def _patched_pipeline(self, distance_miles, duration_seconds=3600.0):
        geocode_patch = patch(
            "planner.services.geocoding.geocode_location",
            side_effect=lambda query: {
                "Start City, IL": START_COORDS,
                "Finish City, IL": FINISH_COORDS,
            }[query],
        )
        route_patch = patch(
            "planner.services.routing.get_route",
            return_value=RouteResult(
                distance_miles=distance_miles, duration_seconds=duration_seconds, geometry=self.geometry
            ),
        )
        return geocode_patch, route_patch

    def _create_plan(self, distance_miles=600.0, extra_payload=None):
        geocode_patch, route_patch = self._patched_pipeline(distance_miles=distance_miles)
        payload = {"start": "Start City, IL", "finish": "Finish City, IL"}
        payload.update(extra_payload or {})
        with geocode_patch, route_patch:
            return self.client.post("/api/v1/route-plans/", payload, format="json")

    def test_create_route_plan_returns_full_plan_with_fuel_stops(self):
        response = self._create_plan(distance_miles=600.0)

        self.assertEqual(response.status_code, 201, response.data)
        body = response.data
        self.assertEqual(body["distance_miles"], 600.0)
        self.assertAlmostEqual(body["total_gallons"], 60.0, places=3)  # 600mi / 10mpg default
        self.assertIsNotNone(body["total_cost"])
        self.assertGreaterEqual(len(body["fuel_stops"]), 1)
        self.assertTrue(body["map_url"].endswith(f"/api/v1/route-plans/{body['id']}/map/"))

        self.assertEqual(RoutePlan.objects.count(), 1)
        plan = RoutePlan.objects.get()
        self.assertEqual(plan.fuel_stops.count(), len(body["fuel_stops"]))

    def test_total_gallons_always_equals_distance_over_mpg(self):
        response = self._create_plan(distance_miles=437.0, extra_payload={"mpg": 12.5})
        self.assertEqual(response.status_code, 201, response.data)
        self.assertAlmostEqual(response.data["total_gallons"], 437.0 / 12.5, places=3)

    def test_cheaper_station_is_used_for_more_fuel_than_the_pricier_one(self):
        response = self._create_plan(distance_miles=600.0)
        stops_by_name = {s["station_name"]: s for s in response.data["fuel_stops"]}
        self.assertIn("Cheap Stop", stops_by_name)
        if "Pricey Stop" in stops_by_name:
            self.assertGreater(
                stops_by_name["Cheap Stop"]["gallons_purchased"],
                stops_by_name["Pricey Stop"]["gallons_purchased"],
            )

    def test_rejects_identical_start_and_finish(self):
        response = self.client.post(
            "/api/v1/route-plans/",
            {"start": "Same Place, IL", "finish": "Same Place, IL"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_missing_fields_returns_400(self):
        response = self.client.post("/api/v1/route-plans/", {"start": "Only Start, IL"}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_infeasible_trip_returns_400_with_clear_message(self):
        response = self._create_plan(distance_miles=600.0, extra_payload={"vehicle_range_miles": 5})
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.data)

    def test_geocoding_failure_returns_400_not_500(self):
        with patch("planner.services.geocoding.geocode_location", side_effect=GeocodingError("nope")):
            response = self.client.post(
                "/api/v1/route-plans/",
                {"start": "Unresolvable Place", "finish": "Also Unresolvable"},
                format="json",
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.data)

    def test_identical_repeat_request_reuses_the_same_persisted_plan(self):
        # Regression test for a real bug: this used to assert
        # RoutePlan.objects.count() == 2 here and present that as *correct*
        # behavior -- every cache hit was still writing a brand new
        # RoutePlan + full set of FuelStops, meaning the table grew without
        # bound purely from repeat/duplicate requests, cache or no cache.
        # The fix: a cache hit reuses the already-persisted plan (200), and
        # only a genuinely new computation creates a new one (201).
        geocode_patch, route_patch = self._patched_pipeline(distance_miles=600.0)
        payload = {"start": "Start City, IL", "finish": "Finish City, IL"}
        with geocode_patch, route_patch as mocked_route:
            first = self.client.post("/api/v1/route-plans/", payload, format="json")
            second = self.client.post("/api/v1/route-plans/", payload, format="json")

        self.assertEqual(mocked_route.call_count, 1)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.data["id"], second.data["id"])
        self.assertEqual(RoutePlan.objects.count(), 1)
        self.assertEqual(RoutePlan.objects.get().fuel_stops.count(), len(first.data["fuel_stops"]))

    def test_semantically_duplicate_start_and_finish_is_rejected(self):
        # Regression test for a real bug: differently-worded queries for the
        # same place ("Chicago, IL" vs "Chicago, Illinois") used to sail
        # past the naive string-equality check and return a nonsensical
        # 201 with 0 miles, 0 stops, $0 cost, as if that were a real trip.
        same_point = Coordinates(latitude=41.8373, longitude=-87.6861)
        with patch("planner.services.geocoding.geocode_location", side_effect=lambda q: same_point):
            response = self.client.post(
                "/api/v1/route-plans/",
                {"start": "Chicago, IL", "finish": "Chicago, Illinois"},
                format="json",
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.data)
        self.assertEqual(RoutePlan.objects.count(), 0)

    def test_total_cost_and_gallons_exactly_equal_the_sum_of_the_line_items(self):
        # Regression test for a real bug: total_cost/total_gallons were
        # rounded independently from each fuel_stop's cost/gallons_purchased
        # (both derived from the same unrounded float sum, rounded
        # separately) -- mathematically guaranteed to disagree by a cent
        # for *some* inputs (round(30.015, 2) == 30.02, but
        # round(10.005,2)*3 == 30.03). Now the total is computed as the sum
        # of the already-rounded per-stop values, so they can never diverge.
        response = self._create_plan(distance_miles=600.0)
        self.assertEqual(response.status_code, 201, response.data)
        body = response.data

        stop_cost_sum = round(sum(s["cost"] for s in body["fuel_stops"]), 2)
        stop_gallons_sum = round(sum(s["gallons_purchased"] for s in body["fuel_stops"]), 3)
        self.assertEqual(body["total_cost"], stop_cost_sum)
        self.assertEqual(body["total_gallons"], stop_gallons_sum)

    def test_list_and_retrieve_and_map_endpoints(self):
        created = self._create_plan(distance_miles=600.0)
        plan_id = created.data["id"]

        list_response = self.client.get("/api/v1/route-plans/")
        self.assertEqual(list_response.status_code, 200)

        detail_response = self.client.get(f"/api/v1/route-plans/{plan_id}/")
        self.assertEqual(detail_response.status_code, 200)
        self.assertEqual(detail_response.data["id"], plan_id)

        map_response = self.client.get(f"/api/v1/route-plans/{plan_id}/map/")
        self.assertEqual(map_response.status_code, 200)
        self.assertIn(b"leaflet", map_response.content.lower())

    def test_retrieve_missing_plan_returns_404(self):
        response = self.client.get("/api/v1/route-plans/00000000-0000-0000-0000-000000000000/")
        self.assertEqual(response.status_code, 404)

    def test_persist_survives_a_station_deleted_after_being_computed_but_before_being_persisted(self):
        # Regression test for a real bug: a route plan is computed (and
        # cached) referencing a Station; if that Station is deleted before
        # the cached result is later replayed into _persist (e.g. a
        # reimport, or an admin deleting a bad row, landing inside the
        # cache TTL window), creating the FuelStop used to raise a raw
        # IntegrityError (FOREIGN KEY constraint failed) -- on_delete=SET_NULL
        # on FuelStop.station doesn't help here, because it only fires when
        # an *already-referencing* row's target is deleted, not when a new
        # FuelStop is being created against an id that's already gone.
        from planner.services.route_planner import compute_plan_key, compute_route_plan
        from planner.views import RoutePlanViewSet

        geocode_patch, route_patch = self._patched_pipeline(distance_miles=600.0)
        with geocode_patch, route_patch:
            result = compute_route_plan("Start City, IL", "Finish City, IL")

        self.assertTrue(result.fuel_plan.stops, "test setup expects at least one stop")
        deleted_station_id = result.fuel_plan.stops[0].station.pk
        Station.objects.filter(pk=deleted_station_id).delete()

        plan_key = compute_plan_key(
            "Start City, IL", "Finish City, IL",
            result.mpg_used, result.tank_capacity_miles_used, result.corridor_miles_used,
        )
        route_plan = RoutePlanViewSet._persist(plan_key, "Start City, IL", "Finish City, IL", result)  # must not raise

        first_stop = route_plan.fuel_stops.get(order=1)
        self.assertIsNone(first_stop.station_id)  # gracefully degraded FK
        self.assertEqual(first_stop.station_name, "Cheap Stop")  # denormalized snapshot preserved

    def test_explicit_null_mpg_is_treated_the_same_as_omitting_it(self):
        # Regression test for a real bug: omitting "mpg" entirely worked
        # (used the default), but a client that always sends every key and
        # uses JSON null for "unset" -- an extremely common pattern with
        # typed form libraries / generated API clients -- got a confusing
        # 400 "This field may not be null" for the identical intent.
        response = self._create_plan(distance_miles=100.0, extra_payload={"mpg": None})
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["vehicle_mpg"], 10.0)

    def test_database_unique_constraint_rejects_a_second_row_for_the_same_plan_key(self):
        # Proves the actual hard guarantee directly, independent of any
        # application-level "check, then act" logic: the database itself
        # will not allow two RoutePlan rows with the same plan_key to
        # exist, no matter how _get_or_persist is called. See the
        # concurrency test below for proof this holds under real threads.
        from planner.services.route_planner import compute_plan_key, compute_route_plan
        from planner.views import RoutePlanViewSet

        geocode_patch, route_patch = self._patched_pipeline(distance_miles=600.0)
        with geocode_patch, route_patch:
            result = compute_route_plan("Start City, IL", "Finish City, IL")

        plan_key = compute_plan_key(
            "Start City, IL", "Finish City, IL",
            result.mpg_used, result.tank_capacity_miles_used, result.corridor_miles_used,
        )

        RoutePlanViewSet._persist(plan_key, "Start City, IL", "Finish City, IL", result)
        with self.assertRaises(IntegrityError):
            RoutePlanViewSet._persist(plan_key, "Start City, IL", "Finish City, IL", result)

        self.assertEqual(RoutePlan.objects.filter(plan_key=plan_key).count(), 1)


class ConcurrentIdenticalRequestsTests(APITransactionTestCase):
    """Regression test for a real race condition: N clients requesting the
    identical trip at the same moment used to each pass the "does this
    already exist?" check before any of them had written a row, so every
    one of them called OSRM and every one of them inserted a RoutePlan --
    duplicate rows and wasted upstream calls. A plain APITestCase can't
    catch this: it wraps each test in a single DB transaction, so other
    threads' Django ORM calls don't see uncommitted work the way they
    would against a real multi-connection Postgres/production setup.
    APITransactionTestCase disables that wrapping, so this test exercises
    real concurrent commits, same as the live-server manual verification.
    """

    def setUp(self):
        cache.clear()
        self.geometry = _straight_line_geometry()
        cheap_point = self.geometry[50]
        self.cheap_station = Station.objects.create(
            opis_id=201, name="Cheap Stop", city="Testville", state="IL",
            price_per_gallon="2.500", latitude=cheap_point[0], longitude=cheap_point[1],
        )

    def test_concurrent_identical_requests_produce_exactly_one_route_plan_row(self):
        num_threads = 8
        route_call_count = {"n": 0}
        route_call_lock = threading.Lock()

        def counting_get_route(*args, **kwargs):
            with route_call_lock:
                route_call_count["n"] += 1
            time.sleep(0.05)  # widen the race window so a real bug reliably shows up
            return RouteResult(distance_miles=600.0, duration_seconds=3600.0, geometry=self.geometry)

        geocode_patch = patch(
            "planner.services.geocoding.geocode_location",
            side_effect=lambda query: {
                "Start City, IL": START_COORDS,
                "Finish City, IL": FINISH_COORDS,
            }[query],
        )
        route_patch = patch("planner.services.routing.get_route", side_effect=counting_get_route)

        results = [None] * num_threads

        def worker(index):
            from rest_framework.test import APIClient

            client = APIClient()
            payload = {"start": "Start City, IL", "finish": "Finish City, IL"}
            response = client.post("/api/v1/route-plans/", payload, format="json")
            results[index] = response.status_code

        with geocode_patch, route_patch:
            threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        for status_code in results:
            self.assertIn(status_code, (200, 201), results)
        self.assertEqual(
            results.count(201), 1, f"expected exactly one 201 (creator), got statuses: {results}"
        )
        self.assertEqual(
            RoutePlan.objects.count(), 1, "concurrent identical requests must not create duplicate rows"
        )
        self.assertEqual(
            route_call_count["n"], 1, "the best-effort lock should prevent redundant upstream routing calls"
        )
