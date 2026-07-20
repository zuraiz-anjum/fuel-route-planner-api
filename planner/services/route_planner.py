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

The whole result is cached by `compute_plan_key(...)` for a configurable TTL
(ROUTE_CACHE_TTL_SECONDS), so repeated requests for the same trip don't call
OSRM (or Nominatim) again -- until the cache expires *or the station data
changes* (the key folds in the current station-data version, so re-running
`import_fuel_prices` invalidates every previously cached plan automatically).

--- Concurrency -------------------------------------------------------------
A plain "check the cache, then compute" is not atomic: two truly concurrent
identical requests can both miss the cache and both call OSRM -- reproduced
directly (6 concurrent identical requests -> 6 real OSRM calls, not 1).
`cache.add()` (atomic add-if-absent on every backend Django ships) is used
as a short-lived best-effort lock around the compute step: a request that
loses the race waits briefly for the winner to finish and reuses its
result. This *reduces* duplicate calls under concurrency; it is not a hard
guarantee (if the wait times out -- e.g. OSRM itself is unusually slow --
the waiter computes independently rather than hang indefinitely). The hard
guarantee that concurrent identical requests never produce more than one
*persisted* RoutePlan lives in the database (RoutePlan.plan_key's unique
constraint, enforced in planner/views.py), not here -- that's the part that
actually can't be allowed to fail.
------------------------------------------------------------------------------
"""

import hashlib
import time
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache

from planner.exceptions import SameLocationError
from planner.services import fuel_optimizer, geocoding, geometry, routing, station_finder
from planner.services.geo_math import haversine_miles
from planner.services.query_normalization import normalize_query
from stations.data_version import get_current_data_version

# Two geocoded points closer than this are treated as "the same place" even
# if they were worded differently ("Chicago, IL" vs "Chicago, Illinois").
# Wide enough to catch same-city duplicates (which resolve to identical or
# near-identical city centroids), narrow enough to never reject two
# genuinely distinct, if nearby, towns as an invalid trip -- verified against
# several real split-by-a-state-line "twin cities" (Texarkana TX/AR, Kansas
# City KS/MO, Bristol VA/TN), all >4mi apart center-to-center.
SAME_LOCATION_THRESHOLD_MILES = 1.0

_COMPUTE_LOCK_TTL_SECONDS = 30
_COMPUTE_LOCK_POLL_INTERVAL_SECONDS = 0.2
_COMPUTE_LOCK_MAX_WAIT_SECONDS = 10


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
    corridor_miles_used: float


def compute_plan_key(
    start_query: str, finish_query: str, mpg: float, tank_capacity_miles: float, corridor_miles: float
) -> str:
    """The stable identity hash for a (normalized start, normalized finish,
    mpg, range, corridor, current station-data-version) combination.

    Used as both the whole-plan computation cache key (here) and
    RoutePlan.plan_key, the database's unique-constraint dedup mechanism
    (planner/views.py) -- the same inputs that should share a cached
    computation are exactly the inputs that should share one persisted row.
    Public (not `_`-prefixed) so the view layer can compute the identical
    key for its own uniqueness check.
    """
    raw = (
        f"{get_current_data_version()}|{normalize_query(start_query)}|{normalize_query(finish_query)}|"
        f"{mpg}|{tank_capacity_miles}|{corridor_miles}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


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

    plan_key = compute_plan_key(start_query, finish_query, mpg, tank_capacity_miles, corridor_miles)
    result_key = f"route-plan-result:v1:{plan_key}"
    lock_key = f"route-plan-lock:v1:{plan_key}"

    cached_result = cache.get(result_key)
    if cached_result is not None:
        return cached_result

    got_lock = cache.add(lock_key, "1", _COMPUTE_LOCK_TTL_SECONDS)
    if not got_lock:
        waited = 0.0
        while waited < _COMPUTE_LOCK_MAX_WAIT_SECONDS:
            time.sleep(_COMPUTE_LOCK_POLL_INTERVAL_SECONDS)
            waited += _COMPUTE_LOCK_POLL_INTERVAL_SECONDS
            cached_result = cache.get(result_key)
            if cached_result is not None:
                return cached_result
        # Gave up waiting for the in-flight computation -- proceed
        # independently rather than hang or fail.

    try:
        start_coords = geocoding.geocode_location(start_query)
        finish_coords = geocoding.geocode_location(finish_query)

        distance_between_endpoints = haversine_miles(
            start_coords.latitude, start_coords.longitude, finish_coords.latitude, finish_coords.longitude
        )
        if distance_between_endpoints <= SAME_LOCATION_THRESHOLD_MILES:
            raise SameLocationError(
                f"Start ({start_query!r}) and finish ({finish_query!r}) both resolve to "
                f"essentially the same location ({distance_between_endpoints:.2f} mi apart)."
            )

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
            corridor_miles_used=corridor_miles,
        )
        cache.set(result_key, result, settings.ROUTE_CACHE_TTL_SECONDS)
        return result
    finally:
        if got_lock:
            cache.delete(lock_key)
