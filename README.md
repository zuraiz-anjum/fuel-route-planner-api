# Fuel Route Planner API

A Django REST API that, given a start and finish location in the USA, returns:

- the driving route (distance, duration, and a full polyline for mapping),
- the **cost-optimal** sequence of fuel stops along that route (a vehicle
  with a 500-mile range needs to refuel more than once on a long trip), and
- the **total fuel cost** for the whole trip, assuming 10 mpg.

Built with Django 5.2 (current LTS) + Django REST Framework, a free/keyless
routing API (OSRM), a free/keyless geocoding strategy, and a from-scratch
greedy optimization algorithm for the fuel-purchasing decision.

```
POST /api/v1/route-plans/
{ "start": "Chicago, IL", "finish": "Los Angeles, CA" }
```
returns distance, a map polyline, ~18 recommended fuel stops with exact
gallons/cost per stop, and the trip's total fuel cost — computed with a
**single** external routing API call.

---

## Contents

- [Quick start](#quick-start)
- [API](#api)
- [Architecture](#architecture)
- [The optimization algorithm](#the-optimization-algorithm)
- [Data pipeline & geocoding strategy](#data-pipeline--geocoding-strategy)
- [External API call budget](#external-api-call-budget)
- [Performance](#performance)
- [Testing](#testing)
- [Design decisions & known trade-offs](#design-decisions--known-trade-offs)
- [What I'd do next](#what-id-do-next-with-more-time)
- [Project layout](#project-layout)

---

## Quick start

### Option A — zero setup (SQLite)

```bash
python -m venv .venv
source .venv/bin/activate        # .venv\Scripts\activate on Windows
pip install -r requirements.txt

python manage.py migrate
python manage.py import_fuel_prices   # loads data/fuel_prices.csv (~8k rows -> 6,626 unique US stations)
python manage.py runserver
```

That's it — no Postgres, no Redis, no API keys. Everything defaults to
SQLite + Django's in-memory cache.

Optional, one-time data-quality pass (see [Data pipeline](#data-pipeline--geocoding-strategy)):
```bash
python manage.py geocode_remaining_stations   # ~5 min, rate-limited Nominatim fallback
                                                # for the ~6% of stations the free local
                                                # dataset alone can't place
```

### Option B — Docker Compose (Postgres + Redis)

```bash
docker compose up --build
```
This runs migrations, imports the fuel price data, and serves the API on
`http://localhost:8000` backed by Postgres (the DB this team uses) and Redis
(for caching) — same code, just different `DATABASE_URL`/`REDIS_URL` env vars.

### Running the tests

```bash
python manage.py test          # 49 tests, all offline/mocked, ~0.5-1s
```

---

## API

### `POST /api/v1/route-plans/` — compute a route + fuel plan

Request body:
```json
{
  "start": "Chicago, IL",
  "finish": "Los Angeles, CA",
  "mpg": 10,                  // optional, default 10
  "vehicle_range_miles": 500, // optional, default 500
  "corridor_miles": 8         // optional: how far off-route a station may be, default 8
}
```

Response (trimmed):
```json
{
  "id": "6495c0b6-...",
  "distance_miles": 2029.09,
  "duration_hours": 35.69,
  "geometry": [[41.878, -87.63], ...],
  "vehicle_mpg": 10.0,
  "vehicle_range_miles": 500.0,
  "total_gallons": 202.909,
  "total_cost": 615.88,
  "warning": "",
  "fuel_stops": [
    {
      "order": 1, "station_name": "Gas N Wash", "station_city": "Chicago", "station_state": "IL",
      "station_latitude": 41.68, "station_longitude": -87.76,
      "distance_into_trip_miles": 0.0, "price_per_gallon": 3.399,
      "gallons_purchased": 1.4, "cost": 4.76
    },
    ...
  ],
  "map_url": "http://localhost:8000/api/v1/route-plans/6495c0b6-.../map/"
}
```

If the trip can't be completed (a gap between usable stations wider than the
vehicle's range) or a location can't be resolved, the API returns `400` with
`{"error": "..."}` instead of a partial/misleading plan.

### `GET /api/v1/route-plans/` — list recently computed plans (paginated)
### `GET /api/v1/route-plans/{id}/` — re-fetch a previously computed plan (no recomputation)
### `GET /api/v1/route-plans/{id}/map/` — a Leaflet/OpenStreetMap HTML view of the plan

### `GET /healthz/`

---

## Architecture

```
stations/            Station data: model, admin, import + geocoding management commands
  models.py             Station (opis_id, name, address, city, state, price_per_gallon, lat/lng)
  geodata.py            Shared city/state -> lat/lng reference loader (cached, process-wide)
  management/commands/
    import_fuel_prices.py        one-time/idempotent CSV import + geocode
    geocode_remaining_stations.py one-time Nominatim fallback for the ~6% the free
                                   reference dataset doesn't cover

planner/             Route planning: models, API, and the actual domain logic
  services/
    geocoding.py        free-text location -> Coordinates (local lookup, then Nominatim fallback)
    routing.py          Coordinates x2 -> OSRM route (1 external call)
    geometry.py          raw OSRM polyline -> downsampled, distance-annotated RoutePath
    station_finder.py    RoutePath -> candidate stations near the route (bbox + vectorized)
    fuel_optimizer.py     candidates + trip length -> minimum-cost stop plan (the core algorithm)
    route_planner.py     orchestrates the above, with whole-result caching
  models.py            RoutePlan / FuelStop (persisted history, not just request/response)
  serializers.py, views.py, urls.py    DRF API
  templates/planner/map.html          Leaflet map view
```

The services layer has no Django-request awareness and no DB writes (aside
from reading `Station` rows) — it's plain, unit-testable Python. Persistence
(`RoutePlan`/`FuelStop`) is the view layer's job. This is what lets
`fuel_optimizer.py` be tested with 9 hand-verified numeric scenarios and zero
database or network dependency.

---

## The optimization algorithm

Fuel price varies by station, and a vehicle can carry at most one tank
(500 miles of range = 50 gallons at 10 mpg). Given the total gallons needed
for a trip is fixed (`distance / mpg`, regardless of where you stop),
minimizing total cost means: **buy as much as possible at the cheapest
stations, and as little as possible at expensive ones**, without ever
running out of range or exceeding tank capacity.

`planner/services/fuel_optimizer.py` implements the classical, provably
optimal greedy solution to this ("gas station problem with capacity"),
processing candidate stations in route order:

- **If a strictly cheaper station is reachable within one tank of range**,
  buy only enough fuel to just reach it — there's no reason to pay more now
  for fuel you could get cheaper shortly.
- **If nothing cheaper is reachable**, fill the tank completely — this is
  the best price available for as far as the vehicle can currently see.

The trip is reported as infeasible (`400`, not a wrong answer) if any gap
between usable stations — including start→first-stop and last-stop→destination
— exceeds the vehicle's range; no purchasing strategy can fix a genuine
coverage gap.

**Assumption: the vehicle departs with an empty tank.** This is a deliberate
choice, not an oversight — it's what makes "total money spent on fuel"
reflect the *entire* trip. (The alternative, starting full, would report
`$0` for any trip shorter than the vehicle's range, which isn't a useful
answer to the question being asked.) Because there's no earlier station to
have bought fuel from, the distance driven to reach the first stop is billed
at that first stop's price — this is handled as a one-time adjustment that
never causes a stop to exceed physical tank capacity (see the module
docstring for the exact mechanics; it's also directly tested).

Every numeric example in `planner/tests/test_fuel_optimizer.py` is hand
computed and asserted exactly (down to the cent/gallon) — including the
scenario that caught a real bug during development, see below.

> **A bug I found and fixed while writing tests for this:** an early version
> tracked "fuel remaining" as a signed value that could go negative before
> the first purchase (to represent the empty-tank assumption above). That's
> fine for *totals*, but in the "fill up completely" branch it let a single
> stop compute a purchase *larger than the tank's physical capacity* when
> the first reachable station wasn't at mile 0. The fix splits each
> purchase into a capacity-bounded "forward" portion (what has to physically
> fit in the tank) and a one-time, capacity-exempt "retroactive" portion
> (billing for ground already covered) — see
> `test_never_carries_more_than_a_full_tank_forward_from_any_stop`, which
> specifically guards against regressing this.

---

## Data pipeline & geocoding strategy

The provided `data/fuel_prices.csv` has ~8,151 rows, including duplicates
(the same physical truck stop listed multiple times) and ~620 Canadian rows
(this planner is USA-only, per the assignment). `import_fuel_prices`:

1. Groups rows by **OPIS Truckstop ID** (a stable per-station identifier),
   keeping the cheapest observed price per station.
2. Drops non-US rows (Canadian province codes).
3. Geocodes each station **at the city level**, by joining against a
   **free, bundled** reference dataset (`data/uscities.csv`, ~37k US
   city/state centroids, derived from simplemaps' free "Basic" US Cities
   database, CC BY 4.0) — a **local, offline join, zero network calls**.

Result: **6,626 unique US stations, 6,223 (94%) geocoded for free**, with
zero API calls, in under a second.

The remaining ~400 stations (small/unincorporated places not in the free
city dataset) can optionally be filled in with a **one-time, rate-limited**
Nominatim (OpenStreetMap) pass:
```bash
python manage.py geocode_remaining_stations   # ~1 req/sec per Nominatim's usage policy
```
This is a deliberate, explicit, one-time data-quality command — never
something the live API calls per-request.

**Known limitation:** station coordinates are city-level centroids, not
exact street addresses. Two truck stops in the same city will show
identical coordinates. This is a conscious trade-off for a take-home-sized
project with a free/no-API-key requirement — the corridor search and
optimizer both work correctly at this resolution, but a production version
would want per-address geocoding (batched, cached, and probably paid).

---

## External API call budget

The assignment asks the routing API be called as few times as possible.
Per computed route:

| Step | Calls | Notes |
|---|---|---|
| Geocode `start` | 0 (usual case) or 1 | 0 if it's a recognizable US city (the local reference, ~37k cities, resolves it instantly); 1 (Nominatim) fallback for specific addresses |
| Geocode `finish` | 0 or 1 | same |
| Route (OSRM) | **1** | full geometry + distance + duration requested in the same call (`overview=full&geometries=geojson`) |

**Typical request: 1 external call total. Worst case: 3.** Repeat requests
for the same (start, finish, mpg, range, corridor) are served from cache
without calling anything externally (see [Performance](#performance)).

- **Routing:** [OSRM](http://project-osrm.org/) public demo server — free, no API key.
- **Geocoding fallback:** [Nominatim](https://nominatim.org/) (OpenStreetMap) — free, no API key, used sparingly.

---

## Performance

Measured on a modest dev machine, non-scientifically but honestly:

- **Own computation (geometry processing + station corridor search),
  worst case** (a coast-to-coast route whose bounding box spans nearly the
  full width of the continental US — the least favorable case for the
  bbox pre-filter): **~260-290ms**, against all 6,223 geocoded stations.
  This was **~1050ms** before a profiling pass found the actual bottleneck
  (a denser-than-necessary route polyline sampling — station coordinates
  are already city-level centroids, so sub-5-mile path resolution wasn't
  buying real accuracy) and fixed it; see the comments in
  `planner/services/geometry.py`.
- **End-to-end, first request for a given trip:** dominated entirely by the
  one OSRM network round-trip (typically 1-3s for a long cross-country
  route on the public demo server; this is the free routing service's
  latency, not this codebase's).
- **End-to-end, repeat request for the same trip:** **well under 100ms** —
  the whole computed plan (geocoding + route + fuel plan) is cached
  (`ROUTE_CACHE_TTL_SECONDS`, default 1 hour), so identical requests never
  touch OSRM or Nominatim again until the cache expires.

Query-level choices behind this: indexed `(latitude, longitude)` and
`(state, city)` columns on `Station`, a cheap bounding-box pre-filter before
any heavy geometry math runs, `.only()` to avoid fetching unused columns,
and vectorized (numpy) distance computation instead of a per-station Python
loop.

---

## Testing

```bash
python manage.py test
```

49 tests, all offline (external HTTP calls are mocked with `unittest.mock`
at the service boundary — no test depends on network access or a live
database beyond Django's own test DB):

- `stations/tests/test_import.py` — CSV import: dedup-by-cheapest-price,
  US-only filtering, geocoding join, idempotent re-import, malformed rows.
- `stations/tests/test_geocode_remaining_command.py` — the Nominatim
  fallback command, mocked.
- `stations/tests/test_data_files.py` — sanity checks on the actual bundled
  data files (not the synthetic fixtures used elsewhere).
- `planner/tests/test_geo_math.py`, `test_geometry.py` — distance math and
  route-polyline processing.
- `planner/tests/test_station_finder.py` — corridor filtering against real
  `Station` rows.
- `planner/tests/test_fuel_optimizer.py` — **9 hand-computed scenarios**
  for the core algorithm (exact-value assertions, infeasibility, edge cases,
  the capacity-overfill regression test described above).
- `planner/tests/test_geocoding.py` — local-lookup-first behavior, Nominatim
  fallback, caching, all mocked.
- `planner/tests/test_api.py` — full request/response cycle through DRF,
  including validation errors, infeasible-trip errors, list/retrieve/map
  endpoints, and the cache-avoids-a-second-routing-call behavior.

---

## Design decisions & known trade-offs

- **Django 5.2 (LTS) over 6.0.** 6.0 was newer at build time; 5.2 is the
  currently supported LTS (through April 2028). For a "production-ready"
  backend service, I'd rather ship on the boring, long-supported release
  than the newest `.0`.
- **SQLite by default, Postgres via one env var.** `DATABASE_URL` (via
  `dj-database-url`) switches the whole app to Postgres with no code
  changes — reviewers get a zero-dependency quick start; `docker-compose up`
  gets the real stack the team runs in production.
- **City-level geocoding, not per-address.** See
  [Data pipeline](#data-pipeline--geocoding-strategy) above.
- **A bounding-box pre-filter, not PostGIS.** ~6.6k stations is small enough
  that a plain indexed lat/lng range query + vectorized numpy distance math
  comfortably meets the performance goal without adding a PostGIS dependency.
  At meaningfully larger scale (hundreds of thousands of stations), I'd
  reach for PostGIS's spatial index instead of pushing the numpy approach
  further — noted in "What I'd do next".
- **RoutePlan/FuelStop persist every computed plan.** This wasn't strictly
  required, but it gives GET-by-id (no recomputation), a request history,
  and something to render the map view from, essentially for free.
- **FuelStop denormalizes station details at plan-creation time**
  (name/address/price copied in, not just a foreign key) so a historical
  plan keeps showing exactly what was true when it was computed, even after
  a later `import_fuel_prices` run changes that station's price.

---

## What I'd do next (with more time / at real production scale)

- Swap the bounding-box + numpy approach for **PostGIS** (`PointField` +
  `GiST` index, `dwithin`/`ClosestPoint` queries) once the station count or
  request volume justified it.
- Batch/paid **per-address geocoding** for full station-level (not
  city-level) location accuracy.
- **Redis-backed** caching and rate limiting in front of Nominatim/OSRM
  specifically (beyond the whole-plan cache already in place), so even the
  worst case (both endpoints needing the geocoding fallback) degrades
  gracefully under load.
- A background task (Celery/GCP Cloud Tasks) to pre-warm popular
  routes/corridors instead of computing everything synchronously.
- Swap the public OSRM demo server for a **self-hosted OSRM instance** (or
  a paid provider) for production traffic — the public server is fine for
  this exercise but isn't meant for production load.

---

## Project layout

```
config/                  Django project (settings, urls, wsgi/asgi)
stations/                Station model, CSV import, geocoding data + commands
planner/                 RoutePlan/FuelStop models, services (the algorithm), API, map view
data/
  fuel_prices.csv         provided OPIS fuel price sheet
  uscities.csv            free US city/state -> lat/lng reference (simplemaps Basic, CC BY 4.0)
docker-compose.yml, Dockerfile     Postgres + Redis + gunicorn stack
requirements.txt
.env.example
```
