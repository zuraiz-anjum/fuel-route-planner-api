"""Lightweight sanity checks on the actual bundled data files (not the full
import pipeline -- that's covered, with synthetic fixtures, in
test_import.py). Guards against someone accidentally committing a
truncated/corrupt data file."""

import csv

from django.conf import settings
from django.test import SimpleTestCase

from stations.constants import US_STATE_CODES

FUEL_PRICES_CSV = settings.BASE_DIR / "data" / "fuel_prices.csv"


class BundledFuelPricesCsvTests(SimpleTestCase):
    def test_file_exists_with_expected_header(self):
        self.assertTrue(FUEL_PRICES_CSV.exists(), f"Expected {FUEL_PRICES_CSV} to exist")
        with FUEL_PRICES_CSV.open(encoding="utf-8") as fh:
            header = next(csv.reader(fh))
        self.assertEqual(
            header,
            ["OPIS Truckstop ID", "Truckstop Name", "Address", "City", "State", "Rack ID", "Retail Price"],
        )

    def test_contains_a_substantial_number_of_us_rows(self):
        with FUEL_PRICES_CSV.open(encoding="utf-8") as fh:
            us_rows = sum(1 for row in csv.DictReader(fh) if row["State"].strip().upper() in US_STATE_CODES)
        self.assertGreater(us_rows, 1000)


class BundledCityReferenceCsvTests(SimpleTestCase):
    def test_file_exists_with_expected_header(self):
        self.assertTrue(settings.US_CITIES_REFERENCE_CSV.exists())
        with settings.US_CITIES_REFERENCE_CSV.open(encoding="utf-8") as fh:
            header = next(csv.reader(fh))
        self.assertEqual(header, ["city", "state_id", "lat", "lng", "population"])
