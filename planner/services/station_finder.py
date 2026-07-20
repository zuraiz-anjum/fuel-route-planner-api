"""Finds candidate fuel stations near a computed route.

Deliberately a two-stage filter:

  1. A cheap DB query using a lat/lon bounding box over indexed columns to
     cut the station table down before any heavy geometry math runs. For
     typical regional trips this shrinks thousands of stations down to a
     few dozen/hundred; even in the worst case (a diagonal coast-to-coast
     route, where the bounding box barely helps) the station table here is
     small enough (~6-7k rows) that stage 2 is still fast.
  2. Precise, vectorized (numpy) distance-to-route computation on that
     candidate set: for every candidate, its distance to the *nearest*
     sampled route point, and that point's cumulative mileage into the
     trip. This is what tells the optimizer both "is this station usable"
     (within the corridor) and "where does it sit on the trip" (mile N).
"""

from dataclasses import dataclass

import numpy as np
from django.conf import settings

from planner.services.geo_math import haversine_miles
from planner.services.geometry import RoutePath
from stations.models import Station

MILES_PER_DEGREE_LATITUDE = 69.0


@dataclass(frozen=True)
class RouteStation:
    station: Station
    distance_along_route_miles: float
    distance_from_route_miles: float


def find_stations_near_route(route_path: RoutePath, corridor_miles: float | None = None) -> list[RouteStation]:
    corridor_miles = corridor_miles if corridor_miles is not None else settings.ROUTE_SEARCH_CORRIDOR_MILES

    min_lat = float(route_path.latitudes.min())
    max_lat = float(route_path.latitudes.max())
    min_lng = float(route_path.longitudes.min())
    max_lng = float(route_path.longitudes.max())
    mean_lat = (min_lat + max_lat) / 2.0

    lat_pad = corridor_miles / MILES_PER_DEGREE_LATITUDE
    lng_pad = corridor_miles / (MILES_PER_DEGREE_LATITUDE * max(np.cos(np.radians(mean_lat)), 0.15))

    candidates = list(
        Station.objects.only(
            "id", "opis_id", "name", "address", "city", "state", "price_per_gallon", "latitude", "longitude"
        ).filter(
            latitude__isnull=False,
            longitude__isnull=False,
            latitude__gte=min_lat - lat_pad,
            latitude__lte=max_lat + lat_pad,
            longitude__gte=min_lng - lng_pad,
            longitude__lte=max_lng + lng_pad,
        )
    )
    if not candidates:
        return []

    station_lats = np.array([c.latitude for c in candidates], dtype=np.float64)
    station_lngs = np.array([c.longitude for c in candidates], dtype=np.float64)

    # (n_stations, n_route_points) pairwise distance matrix in one shot.
    distance_matrix = haversine_miles(
        station_lats[:, np.newaxis],
        station_lngs[:, np.newaxis],
        route_path.latitudes[np.newaxis, :],
        route_path.longitudes[np.newaxis, :],
    )

    nearest_point_index = np.argmin(distance_matrix, axis=1)
    nearest_distance = distance_matrix[np.arange(len(candidates)), nearest_point_index]
    nearest_cumulative_miles = route_path.cumulative_miles[nearest_point_index]

    results = [
        RouteStation(
            station=candidates[i],
            distance_along_route_miles=float(nearest_cumulative_miles[i]),
            distance_from_route_miles=float(nearest_distance[i]),
        )
        for i in range(len(candidates))
        if nearest_distance[i] <= corridor_miles
    ]
    results.sort(key=lambda rs: rs.distance_along_route_miles)
    return results
