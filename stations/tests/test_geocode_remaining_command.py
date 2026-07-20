from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from planner.services.geocoding import Coordinates
from stations.models import Station


class GeocodeRemainingStationsCommandTests(TestCase):
    def setUp(self):
        Station.objects.create(
            opis_id=1, name="Resolved Already", city="Chicago", state="IL",
            price_per_gallon="3.00", latitude=41.8, longitude=-87.6,
        )
        Station.objects.create(
            opis_id=2, name="Needs Geocoding A", city="Nowheresville", state="IL", price_per_gallon="3.00",
        )
        Station.objects.create(
            opis_id=3, name="Needs Geocoding B", city="Nowheresville", state="IL", price_per_gallon="3.10",
        )

    def test_resolves_unresolved_stations_grouped_by_city_state(self):
        with patch(
            "stations.management.commands.geocode_remaining_stations.geocode_city_state",
            return_value=Coordinates(latitude=10.0, longitude=20.0),
        ) as mocked_geocode:
            call_command("geocode_remaining_stations", delay=0)

        # Only one lookup for the shared (Nowheresville, IL) pair, even
        # though two stations needed it.
        mocked_geocode.assert_called_once_with("Nowheresville", "IL")

        for opis_id in (2, 3):
            station = Station.objects.get(opis_id=opis_id)
            self.assertEqual(station.latitude, 10.0)
            self.assertEqual(station.longitude, 20.0)
            self.assertEqual(station.geocode_source, "nominatim")

        # The already-resolved station is untouched.
        already = Station.objects.get(opis_id=1)
        self.assertEqual(already.latitude, 41.8)

    def test_leaves_stations_unresolved_when_geocoder_fails(self):
        with patch(
            "stations.management.commands.geocode_remaining_stations.geocode_city_state",
            return_value=None,
        ):
            call_command("geocode_remaining_stations", delay=0)

        self.assertIsNone(Station.objects.get(opis_id=2).latitude)

    def test_no_op_when_nothing_is_unresolved(self):
        Station.objects.filter(latitude__isnull=True).delete()
        with patch(
            "stations.management.commands.geocode_remaining_stations.geocode_city_state"
        ) as mocked_geocode:
            call_command("geocode_remaining_stations", delay=0)
        mocked_geocode.assert_not_called()
