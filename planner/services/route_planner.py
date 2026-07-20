"""Top-level orchestration: turns (start, finish) free-text locations into a
full route + fuel-stop plan.

Call budget for a normal request between two US cities:
  - geocode_location(start)  -> 0 network calls (known city, the common case)
  - geocode_location(finish) -> same
  - get_route(...)           -> always exactly 1 OSRM call
So the common case costs a single external call total; worst case (both ends
need the Nominatim fallback) is three, still well within "a couple of calls".

The whole result is cached under compute_plan_key(...) for ROUTE_CACHE_TTL_SECONDS,
so a repeat request for the same trip doesn't hit OSRM/Nominatim again -- unless
the cache expires or the station data changes (the key folds in the current
data-version, so re-running import_fuel_prices invalidates old cached plans).

Concurrency: a plain "check cache, then compute" isn't atomic -- two requests
for the same trip can both miss at the same instant and both call OSRM. cache.add()
gives us an atomic add-if-absent, used here as a short-lived lock: whoever loses
the race waits for the winner instead of computing too. It's best-effort, not a
guarantee (a lock has a token now so a slow/expired holder can't accidentally
release someone else's lock, and a waiter that times out just computes on its
own rather than hanging). The actual guarantee against duplicate *persisted*
plans is the database's unique constraint on RoutePlan.plan_key -- see views.py.
"""

import hashlib
import logging
import time
import uuid
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache

from planner.exceptions import PlannerError, SameLocationError
from planner.services import fuel_optimizer, geocoding, geometry, routing, station_finder
from planner.services.geo_math import haversine_miles
from planner.services.query_normalization import normalize_query
from stations.data_version import get_current_data_version

logger = logging.getLogger(__name__)

# Two geocoded points closer than this count as "the same place" even if
# worded differently ("Chicago, IL" vs "Chicago, Illinois"). Wide enough to
# catch same-city duplicates, narrow enough not to reject genuinely distinct
# nearby towns -- checked against a few real split-by-a-state-line pairs
# (Texarkana TX/AR, Kansas City KS/MO), all well over 4 miles apart.
SAME_LOCATION_THRESHOLD_MILES = 1.0

# Needs to comfortably outlast the slowest realistic computation, or the
# lock expires while its own holder is still working and a second request
# starts a redundant computation alongside it. The old flat 30s was too
# close for comfort: two Nominatim calls at their own timeout plus one slow
# OSRM call already adds up to ~28s before any of our own processing time,
# so a single retry-worthy slow request could trip this. Deriving it from
# the actual configured timeouts keeps it correctly sized if those change.
_COMPUTE_LOCK_TTL_SECONDS = int(
    settings.NOMINATIM_TIMEOUT_SECONDS * 2 + settings.OSRM_TIMEOUT_SECONDS + 15
)
_COMPUTE_LOCK_POLL_INTERVAL_SECONDS = 0.2
_COMPUTE_LOCK_MAX_WAIT_SECONDS = 10

# How long a *failed* computation (bad input, no route, etc.) stays cached.
# Deliberately much shorter than ROUTE_CACHE_TTL_SECONDS: a successful plan
# is worth caching for a while, but an error might be a transient upstream
# hiccup, so we only want it to short-circuit the handful of requests that
# were racing at the same moment, not stick around for an hour. Without
# this, a losing request just sits through the full lock wait and then
# fails anyway on its own -- timed this at ~10s slower than it needed to be
# for no benefit, since the winner had already failed in a fraction of a
# second.
_FAILURE_CACHE_TTL_SECONDS = 10


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
    """Stable identity hash for a (start, finish, mpg, range, corridor,
    current data version) combination. Used both as the compute-cache key
    here and as RoutePlan.plan_key in views.py -- same inputs, same key,
    whether we're deciding what to reuse from cache or what counts as a
    duplicate row in the database.
    """
    raw = (
        f"{get_current_data_version()}|{normalize_query(start_query)}|{normalize_query(finish_query)}|"
        f"{mpg}|{tank_capacity_miles}|{corridor_miles}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _release_lock(lock_key: str, token: str) -> None:
    # Only release if we still own it. Without this check, a lock that
    # outlived its TTL (a slow computation) could get picked up by a second
    # request, and then the first request's cleanup would delete the
    # *second* request's lock instead of its own -- reproduced this with a
    # shortened TTL: it let a third request in too, cascading past a lock
    # that was supposed to only ever have one holder at a time. Not
    # perfectly atomic (get-then-delete has its own tiny race), but it's a
    # best-effort lock to begin with -- this closes the one failure mode
    # that made it actively harmful rather than just occasionally useless.
    if cache.get(lock_key) == token:
        cache.delete(lock_key)


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

    cached = cache.get(result_key)
    if cached is not None:
        if isinstance(cached, PlannerError):
            raise cached
        return cached

    lock_token = uuid.uuid4().hex
    got_lock = cache.add(lock_key, lock_token, _COMPUTE_LOCK_TTL_SECONDS)
    if not got_lock:
        waited = 0.0
        while waited < _COMPUTE_LOCK_MAX_WAIT_SECONDS:
            time.sleep(_COMPUTE_LOCK_POLL_INTERVAL_SECONDS)
            waited += _COMPUTE_LOCK_POLL_INTERVAL_SECONDS
            cached = cache.get(result_key)
            if cached is not None:
                if isinstance(cached, PlannerError):
                    raise cached
                return cached
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
    except PlannerError as exc:
        cache.set(result_key, exc, _FAILURE_CACHE_TTL_SECONDS)
        raise
    finally:
        if got_lock:
            _release_lock(lock_key, lock_token)
