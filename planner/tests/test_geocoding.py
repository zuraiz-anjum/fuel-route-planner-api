from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase

from planner.exceptions import GeocodingError
from planner.services.geocoding import _cache_key, geocode_location


class GeocodeLocationTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_resolves_known_city_state_abbreviation_without_any_network_call(self):
        with patch("planner.services.geocoding.requests.get") as mocked_get:
            coords = geocode_location("Chicago, IL")
        mocked_get.assert_not_called()
        # Exact value from the bundled reference dataset (data/uscities.csv),
        # not the textbook city-hall coordinate -- this is a real city-level
        # centroid, which is all the corridor-matching downstream needs.
        self.assertAlmostEqual(coords.latitude, 41.8373, places=3)
        self.assertAlmostEqual(coords.longitude, -87.6861, places=3)

    def test_resolves_known_city_full_state_name_without_any_network_call(self):
        with patch("planner.services.geocoding.requests.get") as mocked_get:
            coords = geocode_location("Chicago, Illinois")
        mocked_get.assert_not_called()
        self.assertAlmostEqual(coords.latitude, 41.8373, places=3)

    def test_empty_query_raises_without_any_network_call(self):
        with patch("planner.services.geocoding.requests.get") as mocked_get:
            with self.assertRaises(GeocodingError):
                geocode_location("   ")
        mocked_get.assert_not_called()

    def test_falls_back_to_nominatim_when_not_in_local_reference(self):
        fake_response_json = [{"lat": "12.3456", "lon": "-65.4321"}]
        with patch("planner.services.geocoding.requests.get") as mocked_get:
            mocked_get.return_value.raise_for_status.return_value = None
            mocked_get.return_value.json.return_value = fake_response_json

            coords = geocode_location("123 Some Street, Nowhere, IL")

        mocked_get.assert_called_once()
        self.assertAlmostEqual(coords.latitude, 12.3456)
        self.assertAlmostEqual(coords.longitude, -65.4321)

    def test_raises_when_neither_local_reference_nor_nominatim_resolve_it(self):
        with patch("planner.services.geocoding.requests.get") as mocked_get:
            mocked_get.return_value.raise_for_status.return_value = None
            mocked_get.return_value.json.return_value = []
            with self.assertRaises(GeocodingError):
                geocode_location("Nowhere at all, ZZ")

    def test_result_is_cached_so_a_repeat_query_never_calls_nominatim_again(self):
        fake_response_json = [{"lat": "1.0", "lon": "2.0"}]
        with patch("planner.services.geocoding.requests.get") as mocked_get:
            mocked_get.return_value.raise_for_status.return_value = None
            mocked_get.return_value.json.return_value = fake_response_json

            first = geocode_location("123 Some Street, Nowhere, IL")
            second = geocode_location("123 Some Street, Nowhere, IL")

        mocked_get.assert_called_once()
        self.assertEqual(first, second)

    def test_malformed_cache_entry_is_treated_as_a_miss_not_a_crash(self):
        # Regression test for a real bug: a schema change to the cached
        # payload shape (e.g. a field rename during a future refactor, with
        # an old entry still alive under the 30-day geocode cache TTL, or
        # simply a rolling deploy where two code versions briefly share one
        # Redis instance) used to raise a raw, unhandled TypeError instead
        # of gracefully recomputing -- a 500 for something that should be
        # invisible to the caller.
        query = "Chicago, IL"
        cache.set(_cache_key(query), {"unexpected_field": "x", "another": "y"}, 3600)

        with patch("planner.services.geocoding.requests.get") as mocked_get:
            coords = geocode_location(query)  # must NOT raise

        # Recovered via the local reference (no network call needed), and
        # the cache should now hold a well-formed entry.
        mocked_get.assert_not_called()
        self.assertAlmostEqual(coords.latitude, 41.8373, places=3)

    def test_self_heals_a_malformed_cache_entry_for_subsequent_calls(self):
        query = "123 Some Street, Nowhere, IL"  # not resolvable locally -- forces the Nominatim path
        cache.set(_cache_key(query), {"lat": 1.0, "lng": 2.0}, 3600)  # wrong field names

        fake_response_json = [{"lat": "9.0", "lon": "8.0"}]
        with patch("planner.services.geocoding.requests.get") as mocked_get:
            mocked_get.return_value.raise_for_status.return_value = None
            mocked_get.return_value.json.return_value = fake_response_json
            coords = geocode_location(query)

        mocked_get.assert_called_once()  # recomputed instead of trusting the malformed entry
        self.assertEqual((coords.latitude, coords.longitude), (9.0, 8.0))
