"""Vectorized great-circle distance math shared by route geometry processing
and station-corridor matching. Built on numpy so both a ~3,000-point route
polyline and a multi-thousand-station candidate set can be compared in a
handful of array operations instead of a nested Python loop.
"""

import numpy as np

EARTH_RADIUS_MILES = 3958.7613


def haversine_miles(lat1, lon1, lat2, lon2):
    """Great-circle distance in miles between (lat1, lon1) and (lat2, lon2).

    Every argument may be a Python float or a numpy array; arrays are
    broadcast against each other in the usual numpy way, e.g. passing
    column vectors for one pair and row vectors for the other yields the
    full pairwise distance matrix in one call.
    """
    lat1r = np.radians(lat1)
    lon1r = np.radians(lon1)
    lat2r = np.radians(lat2)
    lon2r = np.radians(lon2)

    dlat = lat2r - lat1r
    dlon = lon2r - lon1r

    a = np.sin(dlat / 2.0) ** 2 + np.cos(lat1r) * np.cos(lat2r) * np.sin(dlon / 2.0) ** 2
    c = 2.0 * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))
    return EARTH_RADIUS_MILES * c
