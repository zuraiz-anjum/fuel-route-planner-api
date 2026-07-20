from django.conf import settings
from django.core.cache import cache
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
from planner.services.route_planner import compute_route_plan, plan_cache_key
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

        # The same (start, finish, mpg, range, corridor, station-data-version)
        # combination maps to one cache key (see route_planner.plan_cache_key).
        # A cache *hit* above means this exact plan may already be persisted;
        # reuse that row instead of writing a fresh, identical RoutePlan (and
        # a full set of FuelStops) on every repeat request -- without this,
        # the table grows without bound purely from cache hits, which is
        # exactly what was happening before (see CHANGELOG/README).
        dedup_key = self._dedup_cache_key(data["start"], data["finish"], result)
        existing_plan_id = cache.get(dedup_key)
        if existing_plan_id:
            existing_plan = (
                RoutePlan.objects.prefetch_related("fuel_stops").filter(pk=existing_plan_id).first()
            )
            if existing_plan is not None:
                response_serializer = RoutePlanSerializer(existing_plan, context={"request": request})
                return Response(response_serializer.data, status=200)
            # The cached id pointed at a plan that's since been deleted
            # (e.g. manually, or a future cleanup job) -- fall through and
            # persist a fresh one below.

        route_plan = self._persist(data["start"], data["finish"], result)
        cache.set(dedup_key, str(route_plan.id), settings.ROUTE_CACHE_TTL_SECONDS)

        response_serializer = RoutePlanSerializer(route_plan, context={"request": request})
        return Response(response_serializer.data, status=201)

    @staticmethod
    def _dedup_cache_key(start_query: str, finish_query: str, result) -> str:
        base_key = plan_cache_key(
            start_query, finish_query, result.mpg_used, result.tank_capacity_miles_used, result.corridor_miles_used
        )
        return f"{base_key}:route_plan_id"

    @staticmethod
    def _persist(start_query: str, finish_query: str, result) -> RoutePlan:
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
