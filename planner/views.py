from django.db import transaction
from django.shortcuts import get_object_or_404, render
from rest_framework import mixins, viewsets
from rest_framework.response import Response

from planner.models import FuelStop, RoutePlan
from planner.serializers import (
    RoutePlanListSerializer,
    RoutePlanRequestSerializer,
    RoutePlanSerializer,
)
from planner.services.route_planner import compute_route_plan


class RoutePlanViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """
    POST   /api/v1/route-plans/        compute a new route + fuel plan
    GET    /api/v1/route-plans/        list recently computed plans
    GET    /api/v1/route-plans/{id}/   retrieve a previously computed plan
    """

    queryset = RoutePlan.objects.prefetch_related("fuel_stops")
    lookup_field = "pk"

    def get_serializer_class(self):
        if self.action == "list":
            return RoutePlanListSerializer
        return RoutePlanSerializer

    def create(self, request, *args, **kwargs):
        request_serializer = RoutePlanRequestSerializer(data=request.data)
        request_serializer.is_valid(raise_exception=True)
        data = request_serializer.validated_data

        result = compute_route_plan(
            start_query=data["start"],
            finish_query=data["finish"],
            mpg=data.get("mpg"),
            tank_capacity_miles=data.get("vehicle_range_miles"),
            corridor_miles=data.get("corridor_miles"),
        )

        route_plan = self._persist(data["start"], data["finish"], result)

        response_serializer = RoutePlanSerializer(route_plan, context={"request": request})
        return Response(response_serializer.data, status=201)

    @staticmethod
    def _persist(start_query: str, finish_query: str, result) -> RoutePlan:
        geometry_points = [
            [round(lat, 5), round(lng, 5)]
            for lat, lng in zip(
                result.route_path.latitudes.tolist(), result.route_path.longitudes.tolist()
            )
        ]

        with transaction.atomic():
            route_plan = RoutePlan.objects.create(
                start_query=start_query,
                finish_query=finish_query,
                start_latitude=result.start_coordinates.latitude,
                start_longitude=result.start_coordinates.longitude,
                finish_latitude=result.finish_coordinates.latitude,
                finish_longitude=result.finish_coordinates.longitude,
                distance_miles=round(result.distance_miles, 2),
                duration_seconds=round(result.duration_seconds, 1),
                geometry=geometry_points,
                vehicle_mpg=result.mpg_used,
                vehicle_range_miles=result.tank_capacity_miles_used,
                total_gallons=round(result.fuel_plan.total_gallons, 3),
                total_cost=(
                    round(result.fuel_plan.total_cost, 2)
                    if result.fuel_plan.total_cost is not None
                    else None
                ),
                warning=result.fuel_plan.warning or "",
            )

            FuelStop.objects.bulk_create(
                [
                    FuelStop(
                        route_plan=route_plan,
                        order=index,
                        station=stop.station,
                        station_name=stop.station.name,
                        station_address=stop.station.address,
                        station_city=stop.station.city,
                        station_state=stop.station.state,
                        station_latitude=stop.station.latitude,
                        station_longitude=stop.station.longitude,
                        distance_into_trip_miles=round(stop.distance_into_trip_miles, 2),
                        distance_from_route_miles=round(stop.distance_from_route_miles, 2),
                        price_per_gallon=float(stop.price_per_gallon),
                        gallons_purchased=round(stop.gallons_purchased, 3),
                        cost=round(stop.cost, 2),
                    )
                    for index, stop in enumerate(result.fuel_plan.stops, start=1)
                ]
            )

        return route_plan


def route_plan_map(request, pk):
    """A minimal, dependency-free (Leaflet + OpenStreetMap tiles, no API key)
    HTML view of a computed route plan -- useful for visually sanity-checking
    a plan in a browser alongside the JSON API."""
    route_plan = get_object_or_404(RoutePlan.objects.prefetch_related("fuel_stops"), pk=pk)

    fuel_stops = [
        {
            "order": stop.order,
            "name": stop.station_name,
            "city": stop.station_city,
            "state": stop.station_state,
            "lat": stop.station_latitude,
            "lng": stop.station_longitude,
            "price_per_gallon": stop.price_per_gallon,
            "gallons_purchased": stop.gallons_purchased,
            "cost": stop.cost,
            "distance_into_trip_miles": stop.distance_into_trip_miles,
        }
        for stop in route_plan.fuel_stops.all()
    ]

    context = {
        "route_plan": route_plan,
        "route_geometry_json": route_plan.geometry,
        "fuel_stops_json": fuel_stops,
        "start_point_json": [route_plan.start_latitude, route_plan.start_longitude],
        "finish_point_json": [route_plan.finish_latitude, route_plan.finish_longitude],
    }
    return render(request, "planner/map.html", context)
