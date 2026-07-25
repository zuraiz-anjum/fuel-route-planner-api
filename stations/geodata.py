"""Shared access to the bundled, free US city/state -> lat/lng reference
dataset (data/uscities.csv). Used both by the one-time station import
(stations/management/commands/import_fuel_prices.py) and by the live
geocoding service (planner/services/geocoding.py) as a fast, free first
lookup before ever hitting a network geocoder.
"""

import csv
from functools import lru_cache
from pathlib import Path

CityReference = dict[tuple[str, str], tuple[float, float]]


def load_city_reference(path: Path) -> CityReference:
    reference: CityReference = {}
    with path.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            key = (row["city"].strip().lower(), row["state_id"].strip().upper())
            reference[key] = (float(row["lat"]), float(row["lng"]))
    return reference


@lru_cache(maxsize=1)
def get_city_reference() -> CityReference:
    """Process-wide cached copy, the CSV (~37k rows) is parsed once per
    worker process, not once per request."""
    from django.conf import settings

    return load_city_reference(settings.US_CITIES_REFERENCE_CSV)
