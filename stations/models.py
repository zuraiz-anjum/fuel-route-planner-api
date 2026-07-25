from django.db import models


class Station(models.Model):
    """A single fuel station (truck stop), deduplicated from the OPIS price
    sheet by truckstop id, with the cheapest observed retail price and a
    geocoded (city-level) position.

    Only US stations are imported, see stations.constants.US_STATE_CODES,
    since the planner only routes within the USA.
    """

    opis_id = models.PositiveIntegerField(
        unique=True,
        db_index=True,
        help_text="OPIS Truckstop ID from the source price sheet; stable identity for a physical station.",
    )
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=120, db_index=True)
    state = models.CharField(max_length=2, db_index=True)
    rack_id = models.PositiveIntegerField(null=True, blank=True)
    price_per_gallon = models.DecimalField(max_digits=7, decimal_places=3)

    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    geocode_source = models.CharField(
        max_length=20,
        blank=True,
        help_text="'city_reference' if resolved from the bundled US cities dataset, else blank if unresolved.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["state", "city", "name"]
        indexes = [
            models.Index(fields=["latitude", "longitude"], name="station_lat_lng_idx"),
            models.Index(fields=["state", "city"], name="station_state_city_idx"),
            models.Index(fields=["price_per_gallon"], name="station_price_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.city}, {self.state}) - ${self.price_per_gallon}/gal"

    @property
    def is_geocoded(self) -> bool:
        return self.latitude is not None and self.longitude is not None


class DataImportLog(models.Model):
    """One row per successful `import_fuel_prices` run.

    Its sole purpose is to give downstream code (route_planner.py's whole
    plan cache) a cheap, DB-backed "data version" signal: the cache key for
    a computed route plan includes the timestamp of the latest import, so a
    reimport (prices changing) automatically invalidates every previously
    cached plan, old cache entries just become unreachable under a new
    key, with no need to enumerate or explicitly clear them, and this works
    identically whether the cache backend is per-process LocMemCache or a
    shared Redis instance (unlike bumping an in-memory counter, which
    wouldn't be visible across processes/workers).
    """

    imported_at = models.DateTimeField(auto_now_add=True, db_index=True)
    station_count = models.PositiveIntegerField()

    class Meta:
        ordering = ["-imported_at"]

    def __str__(self) -> str:
        return f"Import at {self.imported_at:%Y-%m-%d %H:%M:%S} ({self.station_count} stations)"
