"""Cheap "how fresh is the station data" signal, used by
planner/services/route_planner.py to fold into its cache key so re-running
`import_fuel_prices` automatically invalidates every previously cached route
plan -- see DataImportLog's docstring.

The version itself is cached for a short TTL rather than queried on every
single request: an earlier version of this queried DataImportLog directly
on every call, which meant even a pure cache *hit* for a route plan cost a
real DB round-trip just to compute which cache key to look up. Since the
version only ever changes when an import completes -- not something that
happens more than a handful of times a day -- a few seconds of staleness on
that specific value is a fine trade for not hitting the DB on every request.
`import_fuel_prices` also proactively clears this cache entry the moment an
import completes, so with a shared cache backend (Redis) the invalidation
is instant; the short TTL is purely a safety net for the LocMemCache case,
where the import command's own process can't reach into the web server
process's in-memory cache to clear it directly.
"""

from django.core.cache import cache

from stations.models import DataImportLog

_CACHE_KEY = "stations:data-version:v1"
_CACHE_TTL_SECONDS = 5


def get_current_data_version() -> str:
    cached = cache.get(_CACHE_KEY)
    if cached is not None:
        return cached

    latest = DataImportLog.objects.only("imported_at").first()
    version = latest.imported_at.isoformat() if latest else "no-import-yet"
    cache.set(_CACHE_KEY, version, _CACHE_TTL_SECONDS)
    return version


def invalidate_data_version_cache() -> None:
    """Called by import_fuel_prices right after logging a new import."""
    cache.delete(_CACHE_KEY)
