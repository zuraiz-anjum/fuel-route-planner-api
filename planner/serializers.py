from django.conf import settings
from django.urls import reverse
from rest_framework import serializers

from planner.models import FuelStop, RoutePlan


class RoutePlanRequestSerializer(serializers.Serializer):
    start = serializers.CharField(
        max_length=255, trim_whitespace=True, help_text="Trip origin, e.g. 'Chicago, IL'."
    )
    finish = serializers.CharField(
        max_length=255, trim_whitespace=True, help_text="Trip destination, e.g. 'Denver, CO'."
    )
    # allow_null=True alongside default=None: a client that always sends
    # every key (common with typed form libraries / generated clients) and
    # uses JSON null for "unset" should get the same "use the default"
    # behavior as a client that omits the key entirely, previously,
    # omitting the field worked but explicitly sending `"mpg": null` was
    # rejected with "This field may not be null", for identical intent.
    mpg = serializers.FloatField(
        required=False, allow_null=True, min_value=1, max_value=200, default=None,
        help_text=f"Vehicle fuel economy in miles/gallon. Defaults to {settings.VEHICLE_MPG}.",
    )
    vehicle_range_miles = serializers.FloatField(
        required=False, allow_null=True, min_value=1, max_value=5000, default=None,
        help_text=f"Max miles the vehicle can drive on a full tank. Defaults to {settings.VEHICLE_RANGE_MILES}.",
    )
    corridor_miles = serializers.FloatField(
        required=False, allow_null=True, min_value=0.5, max_value=50, default=None,
        help_text=(
            "How far off the route (in miles) a station may be and still be considered. "
            f"Defaults to {settings.ROUTE_SEARCH_CORRIDOR_MILES}."
        ),
    )

    def validate(self, attrs):
        if attrs["start"].strip().lower() == attrs["finish"].strip().lower():
            raise serializers.ValidationError("Start and finish must be different locations.")
        return attrs


class FuelStopSerializer(serializers.ModelSerializer):
    class Meta:
        model = FuelStop
        fields = [
            "order",
            "station_name",
            "station_address",
            "station_city",
            "station_state",
            "station_latitude",
            "station_longitude",
            "distance_into_trip_miles",
            "distance_from_route_miles",
            "price_per_gallon",
            "gallons_purchased",
            "cost",
        ]


class RoutePlanSerializer(serializers.ModelSerializer):
    fuel_stops = FuelStopSerializer(many=True, read_only=True)
    duration_hours = serializers.SerializerMethodField()
    map_url = serializers.SerializerMethodField()

    class Meta:
        model = RoutePlan
        fields = [
            "id",
            "start_query",
            "finish_query",
            "start_latitude",
            "start_longitude",
            "finish_latitude",
            "finish_longitude",
            "distance_miles",
            "duration_seconds",
            "duration_hours",
            "geometry",
            "vehicle_mpg",
            "vehicle_range_miles",
            "total_gallons",
            "total_cost",
            "warning",
            "fuel_stops",
            "map_url",
            "created_at",
        ]

    def get_duration_hours(self, obj: RoutePlan) -> float:
        return round(obj.duration_seconds / 3600.0, 2)

    def get_map_url(self, obj: RoutePlan) -> str:
        request = self.context.get("request")
        url = reverse("planner:route-plan-map", kwargs={"pk": obj.pk})
        return request.build_absolute_uri(url) if request else url


class RoutePlanListSerializer(serializers.ModelSerializer):
    """Lighter-weight representation for the list endpoint (no geometry/stops)."""

    class Meta:
        model = RoutePlan
        fields = [
            "id",
            "start_query",
            "finish_query",
            "distance_miles",
            "total_gallons",
            "total_cost",
            "created_at",
        ]
