from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404, render
from rest_framework import mixins, viewsets
from rest_framework.response import Response

from planner.models import FuelStop, RoutePlan
from planner.serializers import (
    RoutePlanListSerializer,
    RoutePlanRequestSerializer,
    RoutePlanSerializer,
)
from planner.services.route_planner import compute_plan_key, compute_route_plan
from stations.models import Station


class RoutePlanViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """
    POST   /api/v1/route-plans/        compute a new route + fuel plan (or
                                        return an existing one -- see below)
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

        plan_key = compute_plan_key(
            data["start"], data["finish"], result.mpg_used, result.tank_capacity_miles_used, result.corridor_miles_used
        )
        route_plan, created = self._get_or_persist(plan_key, data["start"], data["finish"], result)

        response_serializer = RoutePlanSerializer(route_plan, context={"request": request})
        return Response(response_serializer.data, status=201 if created else 200)

    @staticmethod
    def _get_or_persist(plan_key: str, start_query: str, finish_query: str, result) -> tuple[RoutePlan, bool]:
        """Returns (route_plan, created). Two concurrent requests can both
        pass the "does it exist yet" check below at the same time (checked
        this directly -- 8 concurrent requests produced 2 rows before this
        was fixed), so the real guarantee isn't this method's ordering, it's
        RoutePlan.plan_key's unique constraint: only one of them wins the
        INSERT, and the loser's IntegrityError gets caught here and turned
        into "go fetch the winner's row" instead of a crash or a duplicate.
        """
        existing = RoutePlan.objects.prefetch_related("fuel_stops").filter(plan_key=plan_key).first()
        if existing is not None:
            return existing, False

        try:
            return RoutePlanViewSet._persist(plan_key, start_query, finish_query, result), True
        except IntegrityError:
            return RoutePlan.objects.prefetch_related("fuel_stops").get(plan_key=plan_key), False

    @staticmethod
    def _persist(plan_key: str, start_query: str, finish_query: str, result) -> RoutePlan:
        geometry_points = [
            [round(lat, 5), round(lng, 5)]
            for lat, lng in zip(
                result.route_path.latitudes.tolist(), result.route_path.longitudes.tolist()
            )
        ]

        # Round each stop's figures *first*, then derive the plan-level
        # totals as the sum of those already-rounded figures -- guarantees
        # total_cost/total_gallons always exactly equal the sum of the
        # individual fuel_stops the API also returns, rather than being two
        # independently-rounded numbers that can disagree by a cent.
        stops = result.fuel_plan.stops
        rounded_stops = [
            {
                "station": stop.station,
                "gallons_purchased": round(stop.gallons_purchased, 3),
                "cost": round(stop.cost, 2),
                "distance_into_trip_miles": round(stop.distance_into_trip_miles, 2),
                "distance_from_route_miles": round(stop.distance_from_route_miles, 2),
                "price_per_gallon": float(stop.price_per_gallon),
            }
            for stop in stops
        ]

        if rounded_stops:
            total_gallons = round(sum(s["gallons_purchased"] for s in rounded_stops), 3)
            total_cost = round(sum(s["cost"] for s in rounded_stops), 2)
        else:
            # No stops at all: either a trivial trip (0 gallons) or the
            # "no priced stations found nearby" warning case, where there's
            # nothing to sum but the trip still needs total_gallons of fuel.
            total_gallons = round(result.fuel_plan.total_gallons, 3)
            total_cost = (
                round(result.fuel_plan.total_cost, 2) if result.fuel_plan.total_cost is not None else None
            )

        # Defend against a cached RoutePlanResult referencing a Station that
        # has since been deleted (by a reimport, or manually). Without this
        # check, bulk_create below raises a raw IntegrityError (FK
        # constraint) instead of degrading gracefully -- on_delete=SET_NULL
        # on FuelStop.station doesn't help here, since it only fires when an
        # *existing* referencing row's target is deleted, not when a brand
        # new FuelStop is being created against an id that's already gone.
        referenced_ids = [s["station"].pk for s in rounded_stops]
        existing_ids = set(Station.objects.filter(pk__in=referenced_ids).values_list("pk", flat=True))

        with transaction.atomic():
            route_plan = RoutePlan.objects.create(
                plan_key=plan_key,
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
                total_gallons=total_gallons,
                total_cost=total_cost,
                warning=result.fuel_plan.warning or "",
            )

            FuelStop.objects.bulk_create(
                [
                    FuelStop(
                        route_plan=route_plan,
                        order=index,
                        station=s["station"] if s["station"].pk in existing_ids else None,
                        station_name=s["station"].name,
                        station_address=s["station"].address,
                        station_city=s["station"].city,
                        station_state=s["station"].state,
                        station_latitude=s["station"].latitude,
                        station_longitude=s["station"].longitude,
                        distance_into_trip_miles=s["distance_into_trip_miles"],
                        distance_from_route_miles=s["distance_from_route_miles"],
                        price_per_gallon=s["price_per_gallon"],
                        gallons_purchased=s["gallons_purchased"],
                        cost=s["cost"],
                    )
                    for index, s in enumerate(rounded_stops, start=1)
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
