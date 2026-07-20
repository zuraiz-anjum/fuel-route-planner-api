"""One-time enrichment pass: geocode the small number of stations the
bundled US-cities reference couldn't resolve (typically small/unincorporated
places), using Nominatim as a fallback.

This is deliberately a *separate*, explicitly-invoked command rather than
something the API calls at request time: it's a slow, rate-limited, one-off
data-quality step (Nominatim's usage policy caps free use at ~1 req/sec),
not something that should ever run as part of serving a route-plan request.
"""

import time

from django.core.management.base import BaseCommand

from planner.services.geocoding import geocode_city_state
from stations.models import Station


class Command(BaseCommand):
    help = "Geocode stations left unresolved by the local city reference, via Nominatim (rate-limited, one-time)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--delay",
            type=float,
            default=1.1,
            help="Seconds to wait between Nominatim requests (policy minimum is 1/sec).",
        )

    def handle(self, *args, **options):
        delay = options["delay"]
        unresolved = Station.objects.filter(latitude__isnull=True).order_by("state", "city")
        city_state_pairs = sorted({(s.city, s.state) for s in unresolved})

        if not city_state_pairs:
            self.stdout.write(self.style.SUCCESS("No unresolved stations -- nothing to do."))
            return

        self.stdout.write(f"Geocoding {len(city_state_pairs)} unresolved city/state pairs via Nominatim ...")

        resolved_count = 0
        failed: list[tuple[str, str]] = []
        for index, (city, state) in enumerate(city_state_pairs, start=1):
            coords = geocode_city_state(city, state)
            if coords is None:
                failed.append((city, state))
                self.stdout.write(f"  [{index}/{len(city_state_pairs)}] FAILED  {city}, {state}")
            else:
                updated = Station.objects.filter(city=city, state=state, latitude__isnull=True).update(
                    latitude=coords.latitude,
                    longitude=coords.longitude,
                    geocode_source="nominatim",
                )
                resolved_count += updated
                self.stdout.write(f"  [{index}/{len(city_state_pairs)}] OK ({updated} station(s))  {city}, {state}")

            if index < len(city_state_pairs):
                time.sleep(delay)

        self.stdout.write(
            self.style.SUCCESS(f"Done. Resolved {resolved_count} station(s); {len(failed)} city/state pair(s) still unresolved.")
        )
        if failed:
            self.stdout.write(self.style.WARNING("Still unresolved: " + ", ".join(f"{c}, {s}" for c, s in failed[:20])))
