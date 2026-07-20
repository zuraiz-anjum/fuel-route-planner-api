"""Single-call integration with OSRM's free, public routing API
(router.project-osrm.org -- no API key required).

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
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.exception("OSRM routing request failed")
        raise RoutingError("The routing service is unavailable right now. Please try again shortly.") from exc

    payload = response.json()
    if payload.get("code") != "Ok" or not payload.get("routes"):
        raise RoutingError("No driving route could be found between the given locations.")

    route = payload["routes"][0]
    coordinates = route["geometry"]["coordinates"]  # GeoJSON order: [lng, lat]
    geometry = [(lat, lng) for lng, lat in coordinates]

    return RouteResult(
        distance_miles=route["distance"] / METERS_PER_MILE,
        duration_seconds=route["duration"],
        geometry=geometry,
    )
