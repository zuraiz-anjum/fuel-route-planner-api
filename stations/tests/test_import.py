import tempfile
from decimal import Decimal
from pathlib import Path

from django.core.management import call_command
from django.test import TestCase

from stations.models import Station

FUEL_CSV_HEADER = "OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price\n"
CITY_REFERENCE_HEADER = "city,state_id,lat,lng,population\n"


def _write_csv(path: Path, header: str, rows: list[str]) -> None:
    path.write_text(header + "\n".join(rows) + "\n", encoding="utf-8")


class ImportFuelPricesTests(TestCase):
    def setUp(self):
        tmp_dir = Path(tempfile.mkdtemp())
        self.fuel_csv = tmp_dir / "fuel.csv"
        self.cities_csv = tmp_dir / "cities.csv"

        _write_csv(
            self.cities_csv,
            CITY_REFERENCE_HEADER,
            [
                "Springfield,IL,39.7817,-89.6501,100000",
                "Springfield,MO,37.2090,-93.2923,100000",
            ],
        )

    def _run_import(self, rows: list[str]) -> None:
        _write_csv(self.fuel_csv, FUEL_CSV_HEADER, rows)
        call_command(
            "import_fuel_prices",
            csv_path=str(self.fuel_csv),
            cities_path=str(self.cities_csv),
        )

    def test_dedupes_by_opis_id_keeping_cheapest_price(self):
        self._run_import(
            [
                '1,PILOT #1,"I-1",Springfield,IL,100,3.500',
                '1,PILOT TRAVEL CENTER #1,"I-1",Springfield,IL,100,3.200',
            ]
        )
        self.assertEqual(Station.objects.count(), 1)
        station = Station.objects.get(opis_id=1)
        self.assertEqual(station.price_per_gallon, Decimal("3.200"))

    def test_excludes_non_us_rows(self):
        self._run_import(
            [
                '2,CANADIAN STOP,"HWY 1",Toronto,ON,200,4.000',
                '3,US STOP,"I-55",Springfield,IL,100,3.100',
            ]
        )
        self.assertEqual(Station.objects.count(), 1)
        self.assertTrue(Station.objects.filter(opis_id=3).exists())
        self.assertFalse(Station.objects.filter(opis_id=2).exists())

    def test_geocodes_from_city_reference(self):
        self._run_import(['4,SOME STOP,"US-50",Springfield,MO,100,3.300'])
        station = Station.objects.get(opis_id=4)
        self.assertAlmostEqual(station.latitude, 37.2090)
        self.assertAlmostEqual(station.longitude, -93.2923)
        self.assertEqual(station.geocode_source, "city_reference")

    def test_unresolved_city_left_ungeocoded(self):
        self._run_import(['5,MYSTERY STOP,"Nowhere Rd",Nowheresville,IL,100,3.900'])
        station = Station.objects.get(opis_id=5)
        self.assertIsNone(station.latitude)
        self.assertIsNone(station.longitude)
        self.assertEqual(station.geocode_source, "")

    def test_reimport_upserts_rather_than_duplicating(self):
        self._run_import(['6,STOP,"I-1",Springfield,IL,100,3.500'])
        self._run_import(['6,STOP,"I-1",Springfield,IL,100,2.999'])
        self.assertEqual(Station.objects.count(), 1)
        self.assertEqual(Station.objects.get(opis_id=6).price_per_gallon, Decimal("2.999"))

    def test_skips_malformed_rows_without_aborting_import(self):
        self._run_import(
            [
                '7,BAD PRICE,"I-1",Springfield,IL,100,not-a-price',
                '8,GOOD ROW,"I-1",Springfield,IL,100,3.000',
            ]
        )
        self.assertEqual(Station.objects.count(), 1)
        self.assertTrue(Station.objects.filter(opis_id=8).exists())

    def test_whitespace_in_source_fields_is_normalized(self):
        self._run_import(['9,  PADDED   NAME  ,"  I-1  ",  Springfield  ,IL,100,3.000'])
        station = Station.objects.get(opis_id=9)
        self.assertEqual(station.name, "PADDED NAME")
        self.assertEqual(station.city, "Springfield")
