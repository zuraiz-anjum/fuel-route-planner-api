import tempfile
from decimal import Decimal
from pathlib import Path

from django.core.management import call_command
from django.test import TestCase

from stations.models import DataImportLog, Station

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

    def _run_import(self, rows: list[str], prune_missing: bool = False) -> None:
        _write_csv(self.fuel_csv, FUEL_CSV_HEADER, rows)
        call_command(
            "import_fuel_prices",
            csv_path=str(self.fuel_csv),
            cities_path=str(self.cities_csv),
            prune_missing=prune_missing,
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

    def test_each_import_creates_a_data_import_log_row(self):
        self.assertEqual(DataImportLog.objects.count(), 0)
        self._run_import(['10,STOP,"I-1",Springfield,IL,100,3.000'])
        self.assertEqual(DataImportLog.objects.count(), 1)
        self._run_import(['10,STOP,"I-1",Springfield,IL,100,3.100'])
        self.assertEqual(DataImportLog.objects.count(), 2, "every run should log a new import, not upsert")

    def test_stations_missing_from_a_reimport_are_reported_but_kept_by_default(self):
        # Regression coverage for a real data-hygiene gap: a station that's
        # removed from the source file (closed/decommissioned truck stop)
        # used to live in the DB forever with no way to detect it, let
        # alone remove it.
        self._run_import(['11,WILL DISAPPEAR,"I-1",Springfield,IL,100,3.000'])
        self._run_import(['12,STILL HERE,"I-1",Springfield,IL,100,3.000'])  # opis_id 11 absent this time

        self.assertTrue(Station.objects.filter(opis_id=11).exists(), "left untouched without --prune-missing")
        self.assertTrue(Station.objects.filter(opis_id=12).exists())

    def test_prune_missing_deletes_stations_absent_from_the_current_import(self):
        self._run_import(['13,WILL BE PRUNED,"I-1",Springfield,IL,100,3.000'])
        self._run_import(['14,SURVIVOR,"I-1",Springfield,IL,100,3.000'], prune_missing=True)

        self.assertFalse(Station.objects.filter(opis_id=13).exists(), "should be pruned: absent + flag set")
        self.assertTrue(Station.objects.filter(opis_id=14).exists())

    def test_reimport_does_not_wipe_out_previous_nominatim_geocoding(self):
        # This one actually happened: a station outside the bundled city
        # reference stays ungeocoded after import (see
        # test_unresolved_city_left_ungeocoded above) until
        # geocode_remaining_stations fills it in via Nominatim. Re-running
        # this command afterwards, a completely normal thing to do, e.g.
        # to pick up a price update, rebuilt every Station row from
        # scratch and overwrote latitude/longitude/geocode_source with the
        # (still-empty) city-reference lookup, silently erasing that
        # enrichment every time.
        self._run_import(['15,MYSTERY STOP,"Nowhere Rd",Nowheresville,IL,100,3.900'])
        Station.objects.filter(opis_id=15).update(
            latitude=39.5, longitude=-89.1, geocode_source="nominatim"
        )

        self._run_import(['15,MYSTERY STOP,"Nowhere Rd",Nowheresville,IL,100,3.500'])  # e.g. a price refresh

        station = Station.objects.get(opis_id=15)
        self.assertEqual(station.latitude, 39.5)
        self.assertEqual(station.longitude, -89.1)
        self.assertEqual(station.geocode_source, "nominatim")
        self.assertEqual(station.price_per_gallon, Decimal("3.500"), "the price should still update normally")
