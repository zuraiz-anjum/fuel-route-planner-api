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

    within_corridor = distance_matrix <= corridor_miles  # (n_stations, n_route_points)

    results: list[RouteStation] = []
    for i in range(len(candidates)):
        point_indices = np.nonzero(within_corridor[i])[0]
        if point_indices.size == 0:
            continue

        # Usually a station is near just one stretch of the route, and this
        # loop does one pass. But if the route genuinely comes near the same
        # station twice -- a spur, a cloverleaf, two legs running close
        # together -- picking only the single closest point (the old
        # np.argmin-only approach) silently drops the other encounter. If the
        # vehicle only needs fuel on the *second* pass, that station looked
        # unavailable even though it physically wasn't. So: split the
        # in-corridor route points into separate encounters wherever there's
        # a real gap between them, and report each encounter as its own
        # candidate at its closest point.
        cluster_start = 0
        for cluster_end in range(1, point_indices.size + 1):
            is_last = cluster_end == point_indices.size
            gap_miles = (
                0.0
                if is_last
                else route_path.cumulative_miles[point_indices[cluster_end]]
                - route_path.cumulative_miles[point_indices[cluster_end - 1]]
            )
            if is_last or gap_miles > corridor_miles * 2:
                cluster = point_indices[cluster_start:cluster_end]
                best = cluster[np.argmin(distance_matrix[i, cluster])]
                results.append(
                    RouteStation(
                        station=candidates[i],
                        distance_along_route_miles=float(route_path.cumulative_miles[best]),
                        distance_from_route_miles=float(distance_matrix[i, best]),
                    )
                )
                cluster_start = cluster_end

    results.sort(key=lambda rs: rs.distance_along_route_miles)
    return results
