"""Turns a free-text location ("Chicago, IL", "Los Angeles, California",
"350 5th Ave, New York, NY") into coordinates.

Resolution order, cheapest/fastest first:
  1. Django cache (any query we've already resolved, local or remote).
  2. Local lookup against the bundled US cities reference -- instant, free,
     no rate limits. Covers the overwhelming majority of "City, ST" style
     input, which is what a route-planning API realistically receives.
  3. Nominatim (OpenStreetMap), as a fallback for specific street addresses
     or spellings the local dataset doesn't have. This is a *different*
     external service than the routing call and is only hit when the fast
     path misses, so it doesn't count against the "minimize routing API
     calls" budget for the actual route computation.

Every result (from either path) is cached, so a given input string never
triggers more than one network call across the life of the cache entry.
"""

import hashlib
import logging
from dataclasses import dataclass

import requests
from django.conf import settings
from django.core.cache import cache

from planner.exceptions import GeocodingError
from stations.constants import US_STATE_CODES, US_STATE_NAME_TO_CODE
from stations.geodata import get_city_reference

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Coordinates:
    latitude: float
    longitude: float


def _cache_key(query: str) -> str:
    # Hashed (rather than the raw query) so the key is safe for every cache
    # backend, including memcached-style backends that reject spaces/colons.
    digest = hashlib.sha256(query.strip().lower().encode("utf-8")).hexdigest()
    return f"geocode:v1:{digest}"


def _resolve_state_token(token: str) -> str | None:
    token = token.strip()
    if len(token) == 2 and token.upper() in US_STATE_CODES:
        return token.upper()
    return US_STATE_NAME_TO_CODE.get(token.lower())


def _try_local_city_lookup(query: str) -> Coordinates | None:
    """Handles the common 'City, State' / 'City, ST' / 'City, ST, USA' shape
    without any network access."""
    parts = [p.strip() for p in query.split(",") if p.strip()]
    if len(parts) < 2:
        return None

    city = parts[0]
    # Try each remaining comma-separated token as the state, in case of a
    # trailing "USA"/"United States".
    for token in parts[1:]:
        state = _resolve_state_token(token)
        if state:
            coords = get_city_reference().get((city.lower(), state))
            if coords:
                return Coordinates(latitude=coords[0], longitude=coords[1])
    return None


def _geocode_via_nominatim(query: str) -> Coordinates | None:
    try:
        response = requests.get(
            f"{settings.NOMINATIM_BASE_URL}/search",
            params={
                "q": query,
                "format": "jsonv2",
                "limit": 1,
                "countrycodes": "us",
            },
            headers={"User-Agent": settings.NOMINATIM_USER_AGENT},
            timeout=settings.NOMINATIM_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.RequestException:
        logger.exception("Nominatim geocoding request failed for query=%r", query)
        return None

    results = response.json()
    if not results:
        return None

    best = results[0]
    try:
        return Coordinates(latitude=float(best["lat"]), longitude=float(best["lon"]))
    except (KeyError, TypeError, ValueError):
        return None


def geocode_city_state(city: str, state: str) -> Coordinates | None:
    """Direct Nominatim lookup for a city/state pair, bypassing the local
    reference (used by the one-off `geocode_remaining_stations` command to
    enrich the handful of stations the bundled dataset doesn't cover)."""
    return _geocode_via_nominatim(f"{city}, {state}, USA")


def geocode_location(query: str) -> Coordinates:
    """Resolve free-text `query` to coordinates, raising GeocodingError if it
    can't be resolved locally or via the fallback geocoder."""
    query = (query or "").strip()
    if not query:
        raise GeocodingError("A location must be provided.")

    key = _cache_key(query)
    cached = cache.get(key)
    if cached is not None:
        return Coordinates(**cached)

    coords = _try_local_city_lookup(query)
    if coords is None:
        coords = _geocode_via_nominatim(query)

    if coords is None:
        raise GeocodingError(f"Could not resolve location: {query!r}")

    cache.set(key, {"latitude": coords.latitude, "longitude": coords.longitude}, settings.GEOCODE_CACHE_TTL_SECONDS)
    return coords
