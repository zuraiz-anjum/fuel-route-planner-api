from django.test import SimpleTestCase

from planner.exceptions import InfeasibleTripError
from planner.services.fuel_optimizer import plan_fuel_stops
from planner.services.station_finder import RouteStation
from stations.models import Station


def _station(opis_id: int, price: str) -> Station:
    # Unsaved model instances are fine here: plan_fuel_stops only reads
    # attributes off them, it never touches the database.
    return Station(opis_id=opis_id, name=f"Station {opis_id}", city="X", state="IL", price_per_gallon=price)


def _route_station(opis_id: int, price: str, position_miles: float) -> RouteStation:
    return RouteStation(
        station=_station(opis_id, price),
        distance_along_route_miles=position_miles,
        distance_from_route_miles=0.5,
    )


class PlanFuelStopsTests(SimpleTestCase):
    def test_trip_shorter_than_range_buys_only_what_the_trip_needs(self):
        # Single station at mile 50 of a 100 mile trip; nothing cheaper
        # ahead, trip ends within range, so it should buy exactly enough to
        # finish (50 for the unbilled leg already driven + 50 to the
        # destination) -- 10 gallons total at 10mpg, matching total_miles/mpg.
        stations = [_route_station(1, "3.00", 50)]
        plan = plan_fuel_stops(stations, total_miles=100, mpg=10, tank_capacity_miles=500)

        self.assertEqual(len(plan.stops), 1)
        self.assertAlmostEqual(plan.total_gallons, 10.0, places=6)
        self.assertAlmostEqual(plan.total_cost, 30.0, places=6)
        self.assertAlmostEqual(plan.stops[0].gallons_purchased, 10.0, places=6)

    def test_buys_just_enough_to_reach_a_strictly_cheaper_station(self):
        # A (mile 50, $4.00) -> B (mile 120, $2.00), tank 150, trip 200mi.
        # Optimal: buy only enough at A to reach B, then fill exactly enough
        # at B to finish. Total gallons must always equal total_miles/mpg.
        stations = [_route_station(1, "4.00", 50), _route_station(2, "2.00", 120)]
        plan = plan_fuel_stops(stations, total_miles=200, mpg=10, tank_capacity_miles=150)

        self.assertEqual(len(plan.stops), 2)
        self.assertAlmostEqual(plan.total_gallons, 20.0, places=6)  # 200 miles / 10 mpg
        self.assertAlmostEqual(plan.total_cost, 64.0, places=6)  # 12gal*$4 + 8gal*$2
        self.assertAlmostEqual(plan.stops[0].gallons_purchased, 12.0, places=6)
        self.assertAlmostEqual(plan.stops[1].gallons_purchased, 8.0, places=6)

    def test_fills_to_capacity_when_nothing_cheaper_is_reachable(self):
        # A (100, $3.00) -> B (250, $3.50) -> C (500, $4.00); nothing ever
        # gets cheaper, tank=300, trip=700. Each stop should fill up to
        # exactly tank capacity (never more -- this exercises the fix for an
        # overfill bug found while deriving this very test), and the final
        # stop buys only what's left to finish.
        stations = [
            _route_station(1, "3.00", 100),
            _route_station(2, "3.50", 250),
            _route_station(3, "4.00", 500),
        ]
        plan = plan_fuel_stops(stations, total_miles=700, mpg=10, tank_capacity_miles=300)

        self.assertEqual(len(plan.stops), 3)
        self.assertAlmostEqual(plan.total_gallons, 70.0, places=6)  # 700 / 10
        self.assertAlmostEqual(plan.stops[0].gallons_purchased, 40.0, places=6)  # 300mi forward + 100mi retroactive
        self.assertAlmostEqual(plan.stops[1].gallons_purchased, 15.0, places=6)  # tops back up to 300mi range
        self.assertAlmostEqual(plan.stops[2].gallons_purchased, 15.0, places=6)  # only 150mi needed to finish
        self.assertAlmostEqual(
            plan.total_cost, 40 * 3.00 + 15 * 3.50 + 15 * 4.00, places=6
        )

    def test_never_carries_more_than_a_full_tank_forward_from_any_stop(self):
        # The physical-capacity invariant applies to fuel carried *forward*
        # into the tank. The very first stop also settles a one-time "debt"
        # for the distance already driven from the empty-tank start (billed
        # here since there's no earlier station to have bought it from), so
        # its gallons_purchased legitimately includes that on top of a full
        # forward fill -- see the module docstring. Strip that known
        # retroactive amount back out before checking the capacity bound.
        stations = [
            _route_station(1, "3.00", 100),
            _route_station(2, "3.50", 250),
            _route_station(3, "4.00", 500),
        ]
        mpg, tank_capacity_miles = 10, 300
        plan = plan_fuel_stops(stations, total_miles=700, mpg=mpg, tank_capacity_miles=tank_capacity_miles)

        for index, stop in enumerate(plan.stops):
            retroactive_miles = stop.distance_into_trip_miles if index == 0 else 0.0
            forward_miles = stop.gallons_purchased * mpg - retroactive_miles
            self.assertLessEqual(forward_miles, tank_capacity_miles + 1e-6)

    def test_raises_when_a_gap_exceeds_vehicle_range(self):
        stations = [_route_station(1, "3.00", 50), _route_station(2, "3.00", 600)]
        with self.assertRaises(InfeasibleTripError):
            plan_fuel_stops(stations, total_miles=700, mpg=10, tank_capacity_miles=500)

    def test_raises_when_final_leg_exceeds_vehicle_range(self):
        stations = [_route_station(1, "3.00", 50)]
        with self.assertRaises(InfeasibleTripError):
            plan_fuel_stops(stations, total_miles=600, mpg=10, tank_capacity_miles=500)

    def test_short_trip_with_no_stations_returns_gallons_with_null_cost_and_warning(self):
        plan = plan_fuel_stops([], total_miles=100, mpg=10, tank_capacity_miles=500)
        self.assertEqual(plan.stops, [])
        self.assertAlmostEqual(plan.total_gallons, 10.0, places=6)
        self.assertIsNone(plan.total_cost)
        self.assertIsNotNone(plan.warning)

    def test_zero_length_trip_is_a_trivial_empty_plan(self):
        plan = plan_fuel_stops([], total_miles=0, mpg=10, tank_capacity_miles=500)
        self.assertEqual(plan.stops, [])
        self.assertEqual(plan.total_gallons, 0.0)
        self.assertEqual(plan.total_cost, 0.0)

    def test_ignores_stations_outside_the_trip_bounds(self):
        stations = [
            _route_station(1, "1.00", -10),  # behind the start, shouldn't happen but must be ignored
            _route_station(2, "3.00", 50),
            _route_station(3, "1.00", 150),  # past the destination
        ]
        plan = plan_fuel_stops(stations, total_miles=100, mpg=10, tank_capacity_miles=500)
        used_ids = {stop.station.opis_id for stop in plan.stops}
        self.assertEqual(used_ids, {2})
