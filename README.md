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
- [Security](#security)
- [Testing](#testing)
- [Correctness pass: bugs found and fixed by an adversarial self-review](#correctness-pass-bugs-found-and-fixed-by-an-adversarial-self-review)
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
python manage.py test          # 64 tests, all offline/mocked, ~0.8-1s
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
`fuel_optimizer.py` be tested with 11 hand-verified numeric scenarios and zero
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

Candidate stations are sorted by **`(position, price)`**, not position
alone — see [Correctness pass](#correctness-pass-bugs-found-and-fixed-by-an-adversarial-self-review)
below for why that second sort key isn't optional: two stations tied on
position (plausible given the route is sampled at ~5mi resolution) used to
have the empty-tank "first leg" billing assigned to whichever one came
first in arbitrary DB row order, not the cheaper one — a 75% cost swing for
an identical trip, caught by a deliberately adversarial review after the
initial build, and now covered by an explicit regression test.

> **Two bugs I found and fixed while testing this:**
> 1. An early version tracked "fuel remaining" as a signed value that could
>    go negative before the first purchase (to represent the empty-tank
>    assumption above). That's fine for *totals*, but in the "fill up
>    completely" branch it let a single stop compute a purchase *larger
>    than the tank's physical capacity* when the first reachable station
>    wasn't at mile 0. Fixed by splitting each purchase into a
>    capacity-bounded "forward" portion and a one-time, capacity-exempt
>    "retroactive" portion — see
>    `test_never_carries_more_than_a_full_tank_forward_from_any_stop`.
> 2. Sorting candidates by position alone made the result depend on
>    arbitrary input/database ordering whenever two stations tied on
>    position — see `test_tied_position_stations_do_not_change_cost_based_on_input_order`,
>    which reproduces a **75% cost difference for the identical trip** from
>    row order alone before the fix (sorting by `(position, price)`).

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

**Decommissioned stations.** A station that disappears from a future price
sheet (closed truck stop) is reported on every import (`N previously-imported
station(s) are not present in this file`) but left in place by default —
running `import_fuel_prices --prune-missing` actually removes them. This is
opt-in, not automatic: running the import against a partial/test CSV would
otherwise delete most of a real station table the moment `--prune-missing`
is the default.

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
for the same (start, finish, mpg, range, corridor, *and current station data
version*) are served from cache without calling anything externally (see
[Performance](#performance)). That last part matters: the cache key includes
a timestamp of the latest `import_fuel_prices` run, so re-running the import
(prices changing) automatically invalidates every previously cached plan
instead of silently serving stale prices for up to the cache TTL — see
[Correctness pass](#correctness-pass-bugs-found-and-fixed-by-an-adversarial-self-review).

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
  touch OSRM or Nominatim again until the cache expires *or the station data
  changes* (see below) — and the persisted `RoutePlan` row is reused rather
  than duplicated, so a flood of identical/repeat requests doesn't grow the
  database unboundedly either.

Query-level choices behind this: indexed `(latitude, longitude)` and
`(state, city)` columns on `Station`, a cheap bounding-box pre-filter before
any heavy geometry math runs, `.only()` to avoid fetching unused columns,
and vectorized (numpy) distance computation instead of a per-station Python
loop.

**Two trade-offs worth being explicit about**, both surfaced by the second
adversarial pass (see above):

- The data-version cache (5s TTL) means a price reimport can, in the
  absolute worst case, take up to 5 seconds to become visible to a cached
  trip, instead of instantly. That's a deliberate trade against hitting the
  DB on every single request (including cache hits) — 5 seconds of possible
  staleness for a price update is a good trade for removing a DB round-trip
  from the hot path of every request.
- The concurrency lock (`cache.add()`-based mutex around computing a plan)
  is *best-effort*, not a hard guarantee — it saves redundant OSRM calls
  when several identical requests race, but if the lock holder crashes
  before releasing it, other requests fall back to polling for up to 10s
  and then computing independently rather than hanging forever. The
  *correctness* guarantee (no duplicate rows) never depends on this lock at
  all — that's the database's `plan_key` uniqueness constraint, which holds
  regardless of what the cache layer does.

---

## Security

Secure-by-default, not secure-by-remembering-to-set-an-env-var:

- `DEBUG` defaults to `False` and `ALLOWED_HOSTS` defaults to
  `localhost,127.0.0.1,[::1]` — both fail *closed* (generic 500s / rejected
  Host headers) if you forget to configure them for a real deployment,
  rather than *open* (leaked stack traces and settings; a wildcard Host
  header accepted from anyone). Local dev opts **in** to `DEBUG=True` via
  `.env.example` — that's a deliberate choice you make by copying the file,
  not the out-of-the-box default.
- `python manage.py check --deploy` passes clean with zero warnings when the
  documented production env vars are set (`DJANGO_SECURE_SSL_REDIRECT`,
  `DJANGO_BEHIND_PROXY`, `DJANGO_SECURE_HSTS_SECONDS`, a real
  `DJANGO_SECRET_KEY`) — see `config/settings.py`'s hardening block and
  `.env.example`. SSL redirect/HSTS are opt-in rather than on-by-default
  specifically because turning them on unconditionally the moment
  `DEBUG=False` would break `docker-compose up` (plain HTTP, no TLS
  termination in front of it) with an infinite redirect loop.
- CORS (`django-cors-headers`) is installed but allows **no origins** by
  default; opt in per-deployment via `CORS_ALLOWED_ORIGINS` or
  `CORS_ALLOW_ALL_ORIGINS` for local/demo use.
- Anonymous request throttling (`API_ANON_THROTTLE_RATE`, default 60/min)
  protects the free upstream services from being hammered by this API's own
  callers. **Caveat:** throttle state lives in the configured cache backend;
  with the default `LocMemCache` (no `REDIS_URL` set) each worker process
  has its own counter, so running multiple gunicorn workers without Redis
  makes the effective limit `60/min × worker count`, not a true global
  60/min. `docker-compose.yml` sets `REDIS_URL`, so the bundled Postgres+Redis
  stack doesn't have this gap — it only applies if you serve the SQLite/no-Redis
  configuration with more than one worker process.
- **No authentication is implemented** — every endpoint is open, including
  the list endpoint, which is effectively a public log of every start/finish
  location anyone has queried (compounded by the fact that, before a fix
  described below, that log also grew without bound). This is a deliberate
  scope decision for a take-home assignment that never asked for user
  accounts, not an oversight — but it's a real limitation for anything
  beyond this exercise. Adding it would mean: an `IsAuthenticated` permission
  class, a `user` FK on `RoutePlan`, and scoping the list endpoint to
  `request.user`'s own plans.

---

## Testing

```bash
python manage.py test
```

72 tests, all offline (external HTTP calls are mocked with `unittest.mock`
at the service boundary — no test depends on network access or a live
database beyond Django's own test DB):

- `stations/tests/test_import.py` — CSV import: dedup-by-cheapest-price,
  US-only filtering, geocoding join, idempotent re-import, malformed rows,
  `DataImportLog` creation, and `--prune-missing` (both the safe
  report-only default and the opt-in delete behavior).
- `stations/tests/test_geocode_remaining_command.py` — the Nominatim
  fallback command, mocked.
- `stations/tests/test_data_files.py` — sanity checks on the actual bundled
  data files (not the synthetic fixtures used elsewhere).
- `planner/tests/test_geo_math.py`, `test_geometry.py` — distance math and
  route-polyline processing.
- `planner/tests/test_station_finder.py` — corridor filtering against real
  `Station` rows.
- `planner/tests/test_fuel_optimizer.py` — **11 hand-computed scenarios**
  for the core algorithm (exact-value assertions, infeasibility, edge cases,
  and two regression tests for real bugs caught during development — see
  below).
- `planner/tests/test_geocoding.py` — local-lookup-first behavior, Nominatim
  fallback, caching (including a malformed-cache-entry regression test), all
  mocked.
- `planner/tests/test_route_planner.py` — whole-plan caching, the
  data-version cache-invalidation-on-reimport regression test, and the
  post-geocode same-location check.
- `planner/tests/test_query_normalization.py` — comma/whitespace/case
  canonicalization used for cache-key hashing.
- `planner/tests/test_api.py` — full request/response cycle through DRF,
  including validation errors, infeasible-trip errors, list/retrieve/map
  endpoints, cache-avoids-a-second-routing-call, plan-dedup-on-cache-hit,
  rounding-consistency, dangling-FK-after-deletion, explicit-null-mpg, and
  a DB-level unique-constraint regression test — plus
  `ConcurrentIdenticalRequestsTests`, an `APITransactionTestCase` that
  fires 8 real threads at the identical trip simultaneously (real commits,
  not the single wrapping transaction a plain `APITestCase` would use) and
  asserts exactly one `RoutePlan` row, one upstream routing call, and a
  correct 200/201 status split come out the other side.

---

## Correctness pass: bugs found and fixed by an adversarial self-review

After the initial build (and its own 49-test suite, all green), I deliberately
re-reviewed this project in "harshest possible reviewer" mode — trying to
break it rather than confirm it worked — and reproduced 12 real issues, each
with a concrete repro before writing a fix, then a regression test after.
Recording them here rather than quietly folding them in, because the
*process* is as relevant to the job as the fixes:

| # | Issue | Severity | Fix |
|---|---|---|---|
| 1 | **The "optimal" fuel algorithm wasn't deterministic.** Two stations tied on route position (plausible at ~5mi route sampling) had the empty-tank first-leg billing assigned by arbitrary DB row order, not price. Reproduced a **75% cost difference for the identical trip** from list order alone. | Critical | Sort candidates by `(position, price)`, not position alone. |
| 2 | **Whole-plan caching served stale prices with no invalidation.** After a price reimport, a cached plan (up to 1hr TTL) kept returning the old price — reproduced directly (`cache still says $3.399 after the DB says $0.001`). | Critical | Cache key now folds in a DB-backed "data version" (`DataImportLog`, bumped every import) — a reimport invalidates every cached plan automatically. |
| 3 | **A cached, since-deleted Station reference crashed persistence.** Reproduced a raw `IntegrityError` (FK constraint) creating a `FuelStop` against an already-deleted station. `on_delete=SET_NULL` doesn't cover this case (it only fires when an *existing* referencing row's target is deleted, not a brand-new row created against an already-gone id). | Critical | Re-verify each referenced station still exists immediately before the write; degrade its FK to `None` (keeping the denormalized snapshot fields) instead of crashing. |
| 4 | **A malformed cache entry crashed with an unhandled `TypeError`**, contradicting the codebase's own "callers get a stable error contract" promise. Reproduced with a simulated schema-drifted cache payload. | Critical | Treat any cache-deserialization failure as a miss (log + recompute + self-heal) instead of raising. |
| 5 | **Every POST created new DB rows, even on a cache hit** — repeat/duplicate requests grew the table forever, unbounded. | High | A cache hit now reuses the already-persisted plan (`200`); only a genuine new computation persists (`201`). |
| 6 | **"Start ≠ finish" validation was a naive string compare.** `"Chicago, IL"` vs `"Chicago, Illinois"` sailed through as a "valid" `201` with 0 miles / $0 cost. | High | Added a post-geocode distance check (< 1mi apart ⇒ rejected) on top of the existing string check. |
| 7 | **`total_cost`/`total_gallons` could disagree with the sum of their own line items** — two independently-rounded numbers, not guaranteed to match (`round(30.015,2)=30.02` but `sum(round(p,2) for p in [10.005]*3)=30.03`). | High | Totals are now the sum of the already-rounded per-stop values, never independently rounded. |
| 8 | **Insecure settings defaults.** `DEBUG` defaulted to `True`, `ALLOWED_HOSTS` to `"*"` — both fail *open* if a real deployment forgets to set an env var. No production hardening (SSL redirect, HSTS, secure cookies) at all. | High | Both now default to the secure option; `manage.py check --deploy` passes clean once the documented (opt-in) env vars are set. |
| 9 | **Decommissioned stations live forever** — the import only ever upserts, never detects/removes stations absent from a newer file. | Medium | `import_fuel_prices` now reports missing-from-this-import stations always, and deletes them with an explicit, non-default `--prune-missing` flag. |
| 10 | **No CORS configuration** — a browser frontend couldn't call this API cross-origin at all. | Medium | Added `django-cors-headers`, no origins allowed by default, opt-in via env var. |
| 11 | **Misleading error for a genuine "no route exists" case** (e.g. Hawaii↔mainland) — OSRM's public server returns this as an HTTP 400, which was being caught by the generic "service unavailable, try again" handler instead of an accurate "no driving route exists between these locations" message. | Low/Medium | Parse the structured OSRM error body regardless of HTTP status before falling back to a generic message. |
| 12 | **Nominatim's usage policy requires a real User-Agent**; the shipped default is an obvious placeholder that could get the app rate-limited/blocked with no warning. | Low | Logs a loud, once-per-process warning the first time a Nominatim call is made with the placeholder still active. |

Every row above has a corresponding regression test (see [Testing](#testing))
and, for the ones that could be demonstrated with a standalone script, a
before/after repro. Rows 1-4 in particular are the kind of thing a
"provably optimal" claim and a passing test suite can both quietly hide —
which is exactly why this pass existed.

---

## Second adversarial pass: concurrency and more

Ran the same "harshest possible reviewer" exercise a second time against the
already-fixed codebase, specifically hunting for anything the first pass
missed — including inside the round-1 fixes themselves. Found 6 more real
issues, the first of which is the most serious bug in this project at any
point during its development:

| # | Issue | Severity | Fix |
|---|---|---|---|
| 1 | **Race condition: concurrent identical requests created duplicate `RoutePlan` rows and each triggered its own OSRM call.** The round-1 fix (#5 above) made a cache *hit* reuse the persisted plan, but said nothing about two requests racing on the same cache *miss* — both would compute, and both would `INSERT`. Reproduced with real Python threads (mocked pipeline) and again against a live `runserver` with real HTTP requests: N simultaneous identical POSTs produced N rows and N upstream calls. | **Critical** | Added a DB-enforced `plan_key` (`unique=True`) derived from the resolved request + vehicle params + data version. Persistence now does insert-then-catch-`IntegrityError`-then-fetch-the-winner — the *database's* uniqueness guarantee is what actually prevents duplicates, not an application-level check (which can never be atomic under real concurrency). Layered a best-effort `cache.add()` mutex on top purely to save redundant OSRM calls while a computation is in flight (a few seconds), with a bounded poll-and-give-up fallback so a crashed lock-holder can never wedge other requests forever. Verified with both a deterministic mocked-threading test and a live 8-thread test against a running server: exactly 1 row, 1 upstream call, correct 200/201 split either way. |
| 2 | **The data-version cache-invalidation fix (round-1 #2) added a DB query to every single request**, including cache hits — the whole point of caching a plan was to avoid exactly this kind of per-request DB round-trip. | Medium | The data-version lookup itself is now cached for 5 seconds, with the import command proactively invalidating it the moment a reimport finishes — so a hot path stays a pure cache hit, and a price update is still visible within, worst case, 5 seconds instead of silently up to an hour. |
| 3 | **Cache keys were sensitive to cosmetic input differences.** `"Chicago, IL"` and `"Chicago,IL"` (no space) or `"Chicago,  IL"` (extra space) are the same query to any human, but hashed to different geocode/route-plan cache entries — needless duplicate Nominatim calls and duplicate persisted plans for what a user would consider identical requests. | Medium | Added `normalize_query()` (whitespace-collapse + comma-spacing + case fold) and route every cache-key computation through it before hashing. |
| 4 | **Explicit JSON `null` for `mpg`/`vehicle_range_miles`/`corridor_miles` was rejected with a 400**, even though omitting the key entirely was accepted and used the same default. Any typed client/generated SDK that always sends every field (using `null` for "unset") hit a confusing, inconsistent error for the identical intent. | Medium | Added `allow_null=True` alongside the existing `required=False, default=None` on all three fields. |
| 5 | **`--prune-missing`'s "what's no longer present" query used a single `exclude(opis_id__in=...)` over the entire new import's id set** — fine at ~6.6k rows, but a portability trap: SQLite caps the number of bound variables in a single statement (`SQLITE_MAX_VARIABLE_NUMBER`, default 999), so this silently breaks on a large enough source file even though nothing about the *logic* is wrong. | Low/Medium | Compute the stale-id set in Python (`existing - imported`, one flat `values_list` query) and delete in bounded chunks of 500 — works identically on SQLite and Postgres regardless of import file size. |
| 6 | **`DataImportLog` had no admin visibility, and `RoutePlan`'s admin offered a broken "Add" page** (every field is read-only, so it rendered with no editable fields and no Save button) — small, but exactly the kind of rough edge a harsh reviewer is told to look for. | Low | Registered `DataImportLogAdmin` (read-only); disabled `RoutePlanAdmin`'s add permission entirely instead of leaving a dead-end link. |

Issue #1 is the one worth dwelling on: a passing test suite and a
"cache-hit reuses the persisted plan" fix from the *first* review round both
made this look solved, right up until it was hit with actual concurrent
requests. It's a reminder that "check, then act" is never sufficient under
real concurrency — only a database constraint (or genuine transactional
isolation) is, and it's exactly the class of bug that's invisible until
something running at the same time actually finds the gap.

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
- **RoutePlan/FuelStop persist every *distinct* computed plan** (deduplicated
  by resolved inputs + current data version, see the [correctness pass](#correctness-pass-bugs-found-and-fixed-by-an-adversarial-self-review)
  above) — GET-by-id, a request history, and something to render the map view
  from, without the table growing purely from cache hits or repeat requests.
- **FuelStop denormalizes station details at plan-creation time**
  (name/address/price copied in, not just a foreign key) so a historical
  plan keeps showing exactly what was true when it was computed, even after
  a later `import_fuel_prices` run changes that station's price — and, since
  the cache-invalidation fix above, that's now actually guaranteed rather
  than being an aspiration the cache could quietly undermine.
- **Pruning decommissioned stations is opt-in, not automatic.** Detecting
  "missing from this import" is safe to always report; *deleting* on that
  basis is only safe with a complete, authoritative source file, so it's a
  deliberate `--prune-missing` flag rather than default behavior.
- **No authentication.** See [Security](#security) above — a deliberate,
  disclosed scope decision, not an oversight.

---

## What I'd do next (with more time / at real production scale)

- **Authentication and per-user scoping** — `IsAuthenticated`, a `user` FK
  on `RoutePlan`, and the list endpoint scoped to the caller's own plans.
  The single biggest remaining gap; see [Security](#security).
- Swap the bounding-box + numpy approach for **PostGIS** (`PointField` +
  `GiST` index, `dwithin`/`ClosestPoint` queries) once the station count or
  request volume justified it.
- Batch/paid **per-address geocoding** for full station-level (not
  city-level) location accuracy.
- Dedicated **rate limiting in front of Nominatim/OSRM specifically**
  (distinct from the whole-plan cache and the per-client API throttle
  already in place), so a burst of never-before-seen locations can't
  exceed Nominatim's ~1 req/sec usage policy even under load.
- A background task (Celery/GCP Cloud Tasks) to pre-warm popular
  routes/corridors instead of computing everything synchronously.
- Swap the public OSRM demo server for a **self-hosted OSRM instance** (or
  a paid provider) for production traffic — the public server is fine for
  this exercise but isn't meant for production load.
- A scheduled job to periodically run `import_fuel_prices` (and, on a slower
  cadence, `--prune-missing`) instead of both being manual/on-demand.

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
