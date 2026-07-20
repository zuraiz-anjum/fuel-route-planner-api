import uuid

from django.db import models

from stations.models import Station


class RoutePlan(models.Model):
    """A computed route + fuel-stop plan, persisted so it can be fetched
    again (GET by id) or rendered on the map view without recomputing --
    and so the API has a request history for free."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Hash of (normalized start, normalized finish, mpg, range, corridor,
    # station-data-version) -- see planner.services.route_planner.plan_cache_key.
    # The UNIQUE constraint is what actually prevents duplicate rows for the
    # same logical request under concurrency: a cache-based "check, then
    # create" (what this used to rely on exclusively) is not atomic and does
    # NOT prevent two concurrent identical requests from both creating a row
    # -- reproduced directly (8 concurrent identical requests -> 2 rows).
    # The database's unique index is enforced atomically regardless of
    # process/thread count; the cache is now just a fast-path optimization
    # on top of that guarantee, not the guarantee itself.
    plan_key = models.CharField(max_length=64, unique=True, db_index=True)

    start_query = models.CharField(max_length=255)
    finish_query = models.CharField(max_length=255)

    start_latitude = models.FloatField()
    start_longitude = models.FloatField()
    finish_latitude = models.FloatField()
    finish_longitude = models.FloatField()

    distance_miles = models.FloatField()
    duration_seconds = models.FloatField()
    # Downsampled [[lat, lng], ...] polyline used to render the route on a map.
    geometry = models.JSONField()

    vehicle_mpg = models.FloatField()
    vehicle_range_miles = models.FloatField()

    total_gallons = models.FloatField()
    total_cost = models.FloatField(null=True, blank=True)
    warning = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.start_query} -> {self.finish_query} ({self.distance_miles:.0f} mi)"


class FuelStop(models.Model):
    """One recommended fuel purchase within a RoutePlan. Station details are
    denormalized (copied) at plan-creation time so a historical plan keeps
    showing exactly what was true when it was computed, even if the
    station's price is refreshed by a later import."""

    route_plan = models.ForeignKey(RoutePlan, related_name="fuel_stops", on_delete=models.CASCADE)
    order = models.PositiveIntegerField()
    station = models.ForeignKey(Station, null=True, blank=True, on_delete=models.SET_NULL)

    station_name = models.CharField(max_length=255)
    station_address = models.CharField(max_length=255, blank=True)
    station_city = models.CharField(max_length=120)
    station_state = models.CharField(max_length=2)
    station_latitude = models.FloatField()
    station_longitude = models.FloatField()

    distance_into_trip_miles = models.FloatField()
    distance_from_route_miles = models.FloatField()
    price_per_gallon = models.FloatField()
    gallons_purchased = models.FloatField()
    cost = models.FloatField()

    class Meta:
        ordering = ["route_plan", "order"]
        constraints = [
            models.UniqueConstraint(fields=["route_plan", "order"], name="unique_stop_order_per_plan")
        ]

    def __str__(self) -> str:
        return f"Stop {self.order}: {self.station_name} ({self.station_city}, {self.station_state})"
