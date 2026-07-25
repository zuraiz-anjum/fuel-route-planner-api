import csv
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from stations.constants import US_STATE_CODES
from stations.data_version import invalidate_data_version_cache
from stations.geodata import load_city_reference
from stations.models import DataImportLog, Station

_DELETE_CHUNK_SIZE = 500


def _delete_in_chunks(opis_ids: set[int], chunk_size: int = _DELETE_CHUNK_SIZE) -> None:
    """Delete stations by opis_id in bounded-size batches rather than one
    query with a huge IN(...) list, see the call site for why."""
    id_list = list(opis_ids)
    for start in range(0, len(id_list), chunk_size):
        chunk = id_list[start : start + chunk_size]
        Station.objects.filter(opis_id__in=chunk).delete()


class Command(BaseCommand):
    help = (
        "Import/refresh Station records from the OPIS fuel price CSV. Rows are "
        "deduplicated by truckstop id (keeping the lowest observed price), "
        "restricted to US states/DC, and geocoded at the city level using the "
        "bundled US cities reference dataset, no external API calls needed. "
        "Safe to re-run; existing stations are upserted by opis_id."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--csv-path",
            default=str(settings.BASE_DIR / "data" / "fuel_prices.csv"),
            help="Path to the OPIS fuel price CSV.",
        )
        parser.add_argument(
            "--cities-path",
            default=str(settings.US_CITIES_REFERENCE_CSV),
            help="Path to the bundled city/state -> lat/lng reference CSV.",
        )
        parser.add_argument(
            "--prune-missing",
            action="store_true",
            help=(
                "Delete existing stations whose opis_id is NOT present in this import "
                "(i.e. genuinely removed from the source file, closed/decommissioned "
                "truck stops). Off by default: only use this with a complete, "
                "authoritative source file, running it against a partial/test CSV "
                "will delete most of your station table. Without this flag, stations "
                "missing from the current file are left untouched but reported."
            ),
        )

    def handle(self, *args, **options):
        csv_path = Path(options["csv_path"])
        cities_path = Path(options["cities_path"])

        if not csv_path.exists():
            raise CommandError(f"Fuel price CSV not found at {csv_path}")
        if not cities_path.exists():
            raise CommandError(f"US cities reference CSV not found at {cities_path}")

        self.stdout.write(f"Loading city reference data from {cities_path} ...")
        city_reference = load_city_reference(cities_path)
        self.stdout.write(f"  {len(city_reference):,} unique city/state coordinates loaded.")

        self.stdout.write(f"Reading fuel prices from {csv_path} ...")
        grouped: dict[int, dict] = {}
        skipped_non_us = 0
        skipped_bad_row = 0
        total_rows = 0

        with csv_path.open(encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                total_rows += 1
                try:
                    opis_id = int(row["OPIS Truckstop ID"].strip())
                    price = Decimal(row["Retail Price"].strip())
                except (KeyError, InvalidOperation, ValueError, AttributeError):
                    skipped_bad_row += 1
                    continue

                state = (row.get("State") or "").strip().upper()
                if state not in US_STATE_CODES:
                    skipped_non_us += 1
                    continue

                name = " ".join(row["Truckstop Name"].split())
                address = " ".join(row["Address"].split())
                city = " ".join(row["City"].split())
                rack_id_raw = (row.get("Rack ID") or "").strip()
                rack_id = int(rack_id_raw) if rack_id_raw.isdigit() else None

                # A single physical truck stop can appear multiple times in the
                # sheet (different fuel grades / listings). We keep the
                # cheapest observed price as "the" price used for optimization.
                existing = grouped.get(opis_id)
                if existing is None or price < existing["price"]:
                    grouped[opis_id] = {
                        "opis_id": opis_id,
                        "name": name,
                        "address": address,
                        "city": city,
                        "state": state,
                        "rack_id": rack_id,
                        "price": price,
                    }

        self.stdout.write(
            f"  {total_rows:,} rows read -> {len(grouped):,} unique US stations "
            f"(skipped {skipped_non_us:,} non-US rows, {skipped_bad_row:,} malformed rows)."
        )

        # Stations already geocoded via the one-off Nominatim pass
        # (geocode_remaining_stations) won't be in city_reference either,
        # that's exactly why they needed that pass. Without this, re-running
        # this command (e.g. a routine price refresh) would blow away that
        # work: bulk_create's update_conflicts overwrites latitude/longitude/
        # geocode_source unconditionally, and the fresh lookup below would
        # come back empty for those same rows every time, resetting them to
        # ungeocoded. So: only treat a station as "still unresolved" if we
        # don't already have coordinates for it from a previous run.
        previously_geocoded = {
            opis_id: (lat, lng, source)
            for opis_id, lat, lng, source in Station.objects.exclude(latitude__isnull=True).values_list(
                "opis_id", "latitude", "longitude", "geocode_source"
            )
        }

        stations: list[Station] = []
        unmatched_cities: set[tuple[str, str]] = set()
        for data in grouped.values():
            key = (data["city"].lower(), data["state"])
            coords = city_reference.get(key)
            if coords:
                lat, lng, source = coords[0], coords[1], "city_reference"
            elif data["opis_id"] in previously_geocoded:
                lat, lng, source = previously_geocoded[data["opis_id"]]
            else:
                lat, lng, source = None, None, ""
                unmatched_cities.add(key)

            stations.append(
                Station(
                    opis_id=data["opis_id"],
                    name=data["name"],
                    address=data["address"],
                    city=data["city"],
                    state=data["state"],
                    rack_id=data["rack_id"],
                    price_per_gallon=data["price"],
                    latitude=lat,
                    longitude=lng,
                    geocode_source=source,
                )
            )

        with transaction.atomic():
            Station.objects.bulk_create(
                stations,
                batch_size=1000,
                update_conflicts=True,
                update_fields=[
                    "name",
                    "address",
                    "city",
                    "state",
                    "rack_id",
                    "price_per_gallon",
                    "latitude",
                    "longitude",
                    "geocode_source",
                ],
                unique_fields=["opis_id"],
            )

            # Anything already in the DB whose opis_id doesn't appear anywhere
            # in *this* file no longer exists in the source data, report it
            # always, and only actually delete it when explicitly asked to
            # (see --prune-missing help text for why this isn't the default).
            #
            # Computed as a set difference in Python rather than
            # .exclude(opis_id__in=grouped.keys()) / one large NOT IN query:
            # with ~6-7k stations that can approach or exceed some SQLite
            # builds' compiled SQLITE_MAX_VARIABLE_NUMBER (historically 999
            # on some system packages, even though this project's bundled
            # SQLite comfortably allows far more), chunking avoids
            # depending on that limit at all, on any backend.
            existing_ids = set(Station.objects.values_list("opis_id", flat=True))
            stale_ids = existing_ids - set(grouped.keys())
            if stale_ids:
                if options["prune_missing"]:
                    _delete_in_chunks(stale_ids)
                    self.stdout.write(
                        self.style.WARNING(
                            f"Pruned {len(stale_ids):,} station(s) no longer present in this import "
                            "(--prune-missing was set)."
                        )
                    )
                else:
                    self.stdout.write(
                        self.style.WARNING(
                            f"{len(stale_ids):,} previously-imported station(s) are not present in this "
                            "file (possibly decommissioned). Left untouched, re-run with "
                            "--prune-missing to remove them."
                        )
                    )

            DataImportLog.objects.create(station_count=len(stations))

        invalidate_data_version_cache()

        geocoded = sum(1 for s in stations if s.latitude is not None)
        self.stdout.write(
            self.style.SUCCESS(
                f"Imported {len(stations):,} stations ({geocoded:,} geocoded, "
                f"{len(stations) - geocoded:,} unresolved across "
                f"{len(unmatched_cities)} distinct city/state pairs)."
            )
        )
        if unmatched_cities:
            sample = ", ".join(f"{c.title()}, {s}" for c, s in sorted(unmatched_cities)[:15])
            more = " ..." if len(unmatched_cities) > 15 else ""
            self.stdout.write(
                self.style.WARNING(
                    "Unresolved city/state examples (excluded from route matching "
                    f"until geocoded): {sample}{more}"
                )
            )
