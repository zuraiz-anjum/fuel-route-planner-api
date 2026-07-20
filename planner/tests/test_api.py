"""End-to-end API tests. External services (geocoding, routing) are mocked
so the whole suite runs offline and deterministically -- exact algorithm
correctness is covered in detail by test_fuel_optimizer.py; these tests
cover request/response wiring, persistence, and HTTP-level error handling.
"""

from unittest.mock import patch

import numpy as np
from django.core.cache import cache
from rest_framework.test import APITestCase

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

    def test_identical_repeat_request_does_not_call_routing_again(self):
        geocode_patch, route_patch = self._patched_pipeline(distance_miles=600.0)
        payload = {"start": "Start City, IL", "finish": "Finish City, IL"}
        with geocode_patch, route_patch as mocked_route:
            self.client.post("/api/v1/route-plans/", payload, format="json")
            self.client.post("/api/v1/route-plans/", payload, format="json")
        self.assertEqual(mocked_route.call_count, 1)
        self.assertEqual(RoutePlan.objects.count(), 2)  # still creates a plan record each time

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
