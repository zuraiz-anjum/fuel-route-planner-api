"""Turns a raw OSRM route polyline into a downsampled path annotated with
cumulative distance from the origin, the representation the corridor
search and fuel optimizer actually need (position along the route matters
far more than every individual GPS vertex).
"""

from dataclasses import dataclass

import numpy as np

from planner.services.geo_math import haversine_miles

# A coast-to-coast OSRM route can return several thousand raw vertices.
# The station search corridor is a handful of miles wide, and station
# coordinates are themselves city-level centroids (see stations/geodata.py),
# already a coarser source of error than a few miles of polyline sampling.
# ~5 mile spacing is plenty of resolution for that corridor check and keeps
# the O(stations x route_points) distance matrix in station_finder.py fast
# even for coast-to-coast trips (measured ~100ms at this density on the full
# ~6.2k geocoded station set, vs ~700ms at 2-mile spacing).
TARGET_WAYPOINT_SPACING_MILES = 5.0
MAX_WAYPOINTS = 700


@dataclass(frozen=True)
class RoutePath:
    latitudes: np.ndarray
    longitudes: np.ndarray
    cumulative_miles: np.ndarray  # monotonically increasing, starts at 0.0

    @property
    def total_miles(self) -> float:
        return float(self.cumulative_miles[-1]) if len(self.cumulative_miles) else 0.0


def build_route_path(geometry: list[tuple[float, float]]) -> RoutePath:
    if len(geometry) < 2:
        raise ValueError("Route geometry must contain at least two points.")

    lats = np.array([point[0] for point in geometry], dtype=np.float64)
    lngs = np.array([point[1] for point in geometry], dtype=np.float64)

    segment_miles = haversine_miles(lats[:-1], lngs[:-1], lats[1:], lngs[1:])
    cumulative = np.concatenate(([0.0], np.cumsum(segment_miles)))

    return _downsample(RoutePath(latitudes=lats, longitudes=lngs, cumulative_miles=cumulative))


def _downsample(path: RoutePath) -> RoutePath:
    total = path.total_miles
    if total <= 0 or len(path.cumulative_miles) <= 2:
        return path

    target_points = min(MAX_WAYPOINTS, max(2, int(total / TARGET_WAYPOINT_SPACING_MILES) + 1))
    if len(path.cumulative_miles) <= target_points:
        return path

    sample_targets = np.linspace(0.0, total, target_points)
    indices = np.searchsorted(path.cumulative_miles, sample_targets)
    indices = np.clip(indices, 0, len(path.cumulative_miles) - 1)
    indices = np.unique(indices)
    indices[0] = 0
    indices[-1] = len(path.cumulative_miles) - 1

    return RoutePath(
        latitudes=path.latitudes[indices],
        longitudes=path.longitudes[indices],
        cumulative_miles=path.cumulative_miles[indices],
    )
