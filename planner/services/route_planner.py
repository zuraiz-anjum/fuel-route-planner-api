"""Top-level orchestration: turns (start, finish) free-text locations into a
full route + fuel-stop plan.

This is the only place that matters for the assignment's "don't hammer the
free routing API" requirement:

  - geocode_location(start)   -> 0 network calls if it's a known US city
                                  (the common case), 1 Nominatim call otherwise
  - geocode_location(finish)  -> same
  - get_route(...)            -> always exactly 1 OSRM call

So a request between two ordinary US cities costs a **single** external
call total; the worst case (both endpoints needing the geocoding fallback)
is three -- matching "one ideal, two or three acceptable" for the whole
pipeline, not just the routing leg.

The whole result is also cached by its inputs for a configurable TTL
(ROUTE_CACHE_TTL_SECONDS), so repeated requests for the same trip don't
call OSRM (or Nominatim) again at all until the cache expires.
"""

import hashlib
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache

from planner.services import fuel_optimizer, geocoding, geometry, routing, station_finder


@dataclass(frozen=True)
class RoutePlanResult:
    start_coordinates: geocoding.Coordinates
    finish_coordinates: geocoding.Coordinates
    distance_miles: float
    duration_seconds: float
    route_path: geometry.RoutePath
    fuel_plan: fuel_optimizer.FuelPlan
    mpg_used: float
    tank_capacity_miles_used: float


def _cache_key(
    start_query: str, finish_query: str, mpg: float, tank_capacity_miles: float, corridor_miles: float
) -> str:
    raw = f"{start_query.strip().lower()}|{finish_query.strip().lower()}|{mpg}|{tank_capacity_miles}|{corridor_miles}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"route-plan:v1:{digest}"


def compute_route_plan(
    start_query: str,
    finish_query: str,
    mpg: float | None = None,
    tank_capacity_miles: float | None = None,
    corridor_miles: float | None = None,
) -> RoutePlanResult:
    mpg = mpg if mpg is not None else settings.VEHICLE_MPG
    tank_capacity_miles = (
        tank_capacity_miles if tank_capacity_miles is not None else settings.VEHICLE_RANGE_MILES
    )
    corridor_miles = corridor_miles if corridor_miles is not None else settings.ROUTE_SEARCH_CORRIDOR_MILES

    key = _cache_key(start_query, finish_query, mpg, tank_capacity_miles, corridor_miles)
    cached_result = cache.get(key)
    if cached_result is not None:
        return cached_result

    start_coords = geocoding.geocode_location(start_query)
    finish_coords = geocoding.geocode_location(finish_query)

    route = routing.get_route(start_coords, finish_coords)
    route_path = geometry.build_route_path(route.geometry)

    nearby_stations = station_finder.find_stations_near_route(route_path, corridor_miles=corridor_miles)

    fuel_plan = fuel_optimizer.plan_fuel_stops(
        nearby_stations,
        total_miles=route.distance_miles,
        mpg=mpg,
        tank_capacity_miles=tank_capacity_miles,
    )

    result = RoutePlanResult(
        start_coordinates=start_coords,
        finish_coordinates=finish_coords,
        distance_miles=route.distance_miles,
        duration_seconds=route.duration_seconds,
        route_path=route_path,
        fuel_plan=fuel_plan,
        mpg_used=mpg,
        tank_capacity_miles_used=tank_capacity_miles,
    )
    cache.set(key, result, settings.ROUTE_CACHE_TTL_SECONDS)
    return result
