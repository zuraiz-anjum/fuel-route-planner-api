import csv
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from stations.constants import US_STATE_CODES
from stations.geodata import load_city_reference
from stations.models import DataImportLog, Station


class Command(BaseCommand):
    help = (
        "Import/refresh Station records from the OPIS fuel price CSV. Rows are "
        "deduplicated by truckstop id (keeping the lowest observed price), "
        "restricted to US states/DC, and geocoded at the city level using the "
        "bundled US cities reference dataset -- no external API calls needed. "
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
                "(i.e. genuinely removed from the source file -- closed/decommissioned "
                "truck stops). Off by default: only use this with a complete, "
                "authoritative source file -- running it against a partial/test CSV "
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

        stations: list[Station] = []
        unmatched_cities: set[tuple[str, str]] = set()
        for data in grouped.values():
            key = (data["city"].lower(), data["state"])
            coords = city_reference.get(key)
            if coords:
                lat, lng, source = coords[0], coords[1], "city_reference"
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
            # in *this* file no longer exists in the source data -- report it
            # always, and only actually delete it when explicitly asked to
            # (see --prune-missing help text for why this isn't the default).
            stale_qs = Station.objects.exclude(opis_id__in=grouped.keys())
            stale_count = stale_qs.count()
            if stale_count:
                if options["prune_missing"]:
                    stale_qs.delete()
                    self.stdout.write(
                        self.style.WARNING(
                            f"Pruned {stale_count:,} station(s) no longer present in this import "
                            "(--prune-missing was set)."
                        )
                    )
                else:
                    self.stdout.write(
                        self.style.WARNING(
                            f"{stale_count:,} previously-imported station(s) are not present in this "
                            "file (possibly decommissioned). Left untouched -- re-run with "
                            "--prune-missing to remove them."
                        )
                    )

            DataImportLog.objects.create(station_count=len(stations))

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
