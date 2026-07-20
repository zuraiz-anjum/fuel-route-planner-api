"""Cheap, DB-backed "how fresh is the station data" signal.

Used by planner/services/route_planner.py to fold into its cache key, so
that re-running `import_fuel_prices` automatically invalidates every
previously cached route plan -- see DataImportLog's docstring.
"""

from stations.models import DataImportLog


def get_current_data_version() -> str:
    """A string that changes every time `import_fuel_prices` completes, and
    is stable (safe to cache against) between imports. Deliberately a single,
    tiny indexed query -- no caching of this value itself is needed."""
    latest = DataImportLog.objects.only("imported_at").first()
    if latest is None:
        return "no-import-yet"
    return latest.imported_at.isoformat()
