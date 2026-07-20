"""Minimum-cost fuel purchasing plan for a route.

Given the trip's total distance, a vehicle's tank capacity (expressed as
miles of range) and fuel economy, and a list of candidate stations (each
with a price and a position along the route), decide which stations to stop
at and how many gallons to buy at each, minimizing total spend while never
letting the tank run empty or exceed capacity.

--- Assumption -------------------------------------------------------------
The vehicle departs the origin with an EMPTY tank. This means:
  * the very first stop must be reachable within one tank of range from the
    start, and
  * the fuel burned reaching that first stop is billed at *that* stop's
    price (there's no earlier station to have bought it from), so the
    reported total cost always reflects the entire trip.
  * (A "starts full" assumption was considered and rejected: it would report
    $0 for any trip shorter than the vehicle's range, which isn't a useful
    answer to "how much will fuel cost for this trip".)
This is a documented, deliberate modeling choice -- see README.md.

--- Algorithm ---------------------------------------------------------------
This is the classical, provably-optimal greedy solution to "minimum cost to
travel with a fuel-capacity constraint" (an exchange-argument optimal
strategy, sometimes seen as an interview problem under names like "gas
station refueling cost"), extended with one bookkeeping detail for the
empty-tank assumption above:

  Process candidate stations in route order. At each one, look ahead (within
  one tank of range) for the next station with a STRICTLY lower price.
    - If one exists, buy only enough fuel to just reach it -- there's no
      reason to buy more now at a worse price than what's coming.
    - If none exists (nothing cheaper is reachable on a full tank), fill the
      tank completely -- this is the best price available for as far as the
      vehicle can currently see, so maximize how much of it gets carried
      forward.
  At the very first station only, the distance already driven to reach it
  (with no prior purchase) is *also* billed at that station's price, in
  addition to (not constrained by the capacity of) whatever is bought to
  carry forward -- it reflects fuel already consumed, not fuel that needs to
  fit in the tank going forward, so it never causes an over-capacity fill.

  Repeat until the destination is reached. The trip is infeasible if any gap
  between consecutive usable stations (including start->first-stop and
  last-stop->destination) exceeds the tank's range -- no purchase strategy
  can fix a genuine coverage gap.
------------------------------------------------------------------------------
"""

from dataclasses import dataclass, field

from django.conf import settings

from planner.exceptions import InfeasibleTripError
from planner.services.station_finder import RouteStation
from stations.models import Station

FLOAT_TOLERANCE = 1e-6


@dataclass(frozen=True)
class FuelStop:
    station: Station
    distance_into_trip_miles: float
    distance_from_route_miles: float
    price_per_gallon: float
    gallons_purchased: float
    cost: float


@dataclass(frozen=True)
class FuelPlan:
    stops: list[FuelStop] = field(default_factory=list)
    total_gallons: float = 0.0
    total_cost: float | None = 0.0
    warning: str | None = None


def _check_feasibility(positions: list[float], total_miles: float, tank_capacity_miles: float) -> None:
    previous = 0.0
    for position in positions:
        gap = position - previous
        if gap > tank_capacity_miles + FLOAT_TOLERANCE:
            raise InfeasibleTripError(
                f"No fuel station is available between mile {previous:.1f} and mile {position:.1f} of the "
                f"trip -- that {gap:.1f} mile gap exceeds the vehicle's {tank_capacity_miles:.1f} mile range."
            )
        previous = position

    final_gap = total_miles - previous
    if final_gap > tank_capacity_miles + FLOAT_TOLERANCE:
        raise InfeasibleTripError(
            f"No fuel station is available in the final {final_gap:.1f} miles of the trip, which exceeds "
            f"the vehicle's {tank_capacity_miles:.1f} mile range."
        )


def plan_fuel_stops(
    route_stations: list[RouteStation],
    total_miles: float,
    mpg: float | None = None,
    tank_capacity_miles: float | None = None,
) -> FuelPlan:
    mpg = mpg if mpg is not None else settings.VEHICLE_MPG
    tank_capacity_miles = (
        tank_capacity_miles if tank_capacity_miles is not None else settings.VEHICLE_RANGE_MILES
    )

    if total_miles <= FLOAT_TOLERANCE:
        return FuelPlan()

    # Sort by (position, price) -- NOT position alone. Multiple physically
    # distinct stations can legitimately tie on distance_along_route_miles
    # (this got MORE likely once the route polyline was coarsened to ~5mi
    # sampling for performance -- see geometry.py), and ties were previously
    # broken by whatever arbitrary DB row order they arrived in (Station's
    # default ordering is alphabetical by state/city/name, nothing to do
    # with price). That silently let a more expensive tied station "win" the
    # empty-tank retroactive billing at index 0, changing the reported total
    # cost by tens of percent for the exact same trip depending on row
    # order. Breaking ties by price makes the plan's cost a pure function of
    # the available stations and the trip, as it always should have been --
    # see test_fuel_optimizer.py::test_tied_position_stations_do_not_change_cost_based_on_input_order.
    in_range = sorted(
        (rs for rs in route_stations if 0.0 <= rs.distance_along_route_miles <= total_miles),
        key=lambda rs: (rs.distance_along_route_miles, float(rs.station.price_per_gallon)),
    )

    _check_feasibility([rs.distance_along_route_miles for rs in in_range], total_miles, tank_capacity_miles)

    if not in_range:
        # Physically possible (short trip, within one tank) but we have no
        # pricing data anywhere near the route -- be honest about that
        # rather than silently reporting $0.
        return FuelPlan(
            total_gallons=total_miles / mpg,
            total_cost=None,
            warning="No priced fuel stations were found near this route, so a cost estimate isn't available.",
        )

    stops: list[FuelStop] = []
    fuel_range_remaining = 0.0  # miles of range physically in the tank; never negative, never > capacity
    position = 0.0
    total_cost = 0.0
    total_gallons = 0.0

    for index, rs in enumerate(in_range):
        travelled = rs.distance_along_route_miles - position
        fuel_range_remaining = max(0.0, fuel_range_remaining - travelled)
        position = rs.distance_along_route_miles
        price = float(rs.station.price_per_gallon)

        # Distance to the next strictly cheaper stop, or to the destination
        # if no cheaper station lies ahead.
        target_distance = total_miles - position
        for later in in_range[index + 1 :]:
            if float(later.station.price_per_gallon) < price:
                target_distance = later.distance_along_route_miles - position
                break

        if target_distance <= tank_capacity_miles + FLOAT_TOLERANCE:
            forward_miles = max(0.0, target_distance - fuel_range_remaining)
        else:
            forward_miles = max(0.0, tank_capacity_miles - fuel_range_remaining)

        # The very first stop also settles the "debt" for the empty-tank
        # departure leg. It's billed here but deliberately NOT added to
        # fuel_range_remaining below (it covers ground already covered, not
        # fuel that needs to fit in the tank), so it can never push the tank
        # past capacity.
        retroactive_miles = rs.distance_along_route_miles if index == 0 else 0.0
        buy_miles = forward_miles + retroactive_miles

        if buy_miles > FLOAT_TOLERANCE:
            gallons = buy_miles / mpg
            cost = gallons * price
            stops.append(
                FuelStop(
                    station=rs.station,
                    distance_into_trip_miles=rs.distance_along_route_miles,
                    distance_from_route_miles=rs.distance_from_route_miles,
                    price_per_gallon=price,
                    gallons_purchased=gallons,
                    cost=cost,
                )
            )
            total_gallons += gallons
            total_cost += cost
            fuel_range_remaining += forward_miles

    return FuelPlan(stops=stops, total_gallons=total_gallons, total_cost=total_cost)
