"""Single-call integration with OSRM's free, public routing API
(router.project-osrm.org, no API key required).

We ask OSRM for full route geometry in the same request that gets us
distance/duration (`overview=full&geometries=geojson`), so computing an
entire route plan needs exactly **one** external routing call, regardless of
trip length.
"""

import logging
from dataclasses import dataclass

import requests
from django.conf import settings

from planner.exceptions import RoutingError
from planner.services.geocoding import Coordinates

logger = logging.getLogger(__name__)

METERS_PER_MILE = 1609.344

# The only two OSRM response codes that actually mean "no road connects
# these two points" (e.g. Hawaii to the mainland). Everything else non-Ok
# (TooBig, InvalidQuery, InvalidValue, ...) is a request/service problem,
# not a geographic fact, lumping those in under "no route exists" was
# itself a bug: it hid a real integration issue behind a message that
# sounds like a permanent, nothing-to-be-done answer.
_NO_ROUTE_CODES = {"NoRoute", "NoSegment"}


@dataclass(frozen=True)
class RouteResult:
    distance_miles: float
    duration_seconds: float
    geometry: list[tuple[float, float]]  # (lat, lng) in travel order


def get_route(origin: Coordinates, destination: Coordinates) -> RouteResult:
    """Fetch the driving route between two points. Raises RoutingError on
    any failure (network, timeout, no route found) so callers get a single,
    predictable exception type."""
    url = (
        f"{settings.OSRM_BASE_URL}/route/v1/driving/"
        f"{origin.longitude},{origin.latitude};{destination.longitude},{destination.latitude}"
    )

    try:
        response = requests.get(
            url,
            params={
                "overview": "full",
                "geometries": "geojson",
                "steps": "false",
                "alternatives": "false",
            },
            timeout=settings.OSRM_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        logger.exception("OSRM routing request failed")
        raise RoutingError("The routing service is unavailable right now. Please try again shortly.") from exc

    # OSRM's public server responds with a non-2xx status (observed: 400)
    # even for a well-formed "no route exists between these points" answer
    # (e.g. Hawaii to the mainland, no road connects them), not just for
    # genuine service failures. Try to read the structured error out of the
    # body first, in either case, so that permanent "no route" answers get
    # an accurate message instead of being lumped in with transient
    # "service unavailable, try again" failures.
    try:
        payload = response.json()
    except ValueError:
        payload = None

    if payload is None or not isinstance(payload, dict):
        try:
            response.raise_for_status()
        except requests.RequestException as exc:
            logger.exception("OSRM routing request failed")
            raise RoutingError(
                "The routing service is unavailable right now. Please try again shortly."
            ) from exc
        raise RoutingError("The routing service returned an unexpected response.")

    if payload.get("code") != "Ok" or not payload.get("routes"):
        code = payload.get("code")
        detail = payload.get("message") or code or "no route was found"
        if code in _NO_ROUTE_CODES:
            raise RoutingError(f"No driving route exists between these locations ({detail}).")
        raise RoutingError(f"The routing service could not process this request ({detail}).")

    route = payload["routes"][0]
    coordinates = route["geometry"]["coordinates"]  # GeoJSON order: [lng, lat]
    geometry = [(lat, lng) for lng, lat in coordinates]

    return RouteResult(
        distance_miles=route["distance"] / METERS_PER_MILE,
        duration_seconds=route["duration"],
        geometry=geometry,
    )
