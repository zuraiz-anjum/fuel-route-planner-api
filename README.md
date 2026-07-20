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
gallons/cost per stop, and the trip's total fuel cost, computed with a
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
- [Bugs I found and fixed along the way](#bugs-i-found-and-fixed-along-the-way)
- [Design decisions & known trade-offs](#design-decisions--known-trade-offs)
- [What I'd do next](#what-id-do-next-with-more-time)
- [Project layout](#project-layout)

---

## Quick start

### Option A, zero setup (SQLite)

```bash
python -m venv .venv
source .venv/bin/activate        # .venv\Scripts\activate on Windows
pip install -r requirements.txt

python manage.py migrate
python manage.py import_fuel_prices   # loads data/fuel_prices.csv (~8k rows -> 6,626 unique US stations)
python manage.py runserver
```

That's it, no Postgres, no Redis, no API keys. Everything defaults to
SQLite + Django's in-memory cache.

Optional, one-time data-quality pass (see [Data pipeline](#data-pipeline--geocoding-strategy)):
```bash
python manage.py geocode_remaining_stations   # ~5 min, rate-limited Nominatim fallback
                                                # for the ~6% of stations the free local
                                                # dataset alone can't place
```

### Option B, Docker Compose (Postgres + Redis)

```bash
docker compose up --build
```
This runs migrations, imports the fuel price data, and serves the API on
`http://localhost:8000` backed by Postgres (the DB this team uses) and Redis
(for caching), same code, just different `DATABASE_URL`/`REDIS_URL` env vars.

### Running the tests

```bash
python manage.py test          # 81 tests, all offline/mocked, ~4-5s
```

---

## API

### `POST /api/v1/route-plans/`, compute a route + fuel plan

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

### `GET /api/v1/route-plans/`, list recently computed plans (paginated)
### `GET /api/v1/route-plans/{id}/`, re-fetch a previously computed plan (no recomputation)
### `GET /api/v1/route-plans/{id}/map/`, a Leaflet/OpenStreetMap HTML view of the plan

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
from reading `Station` rows), it's plain, unit-testable Python. Persistence
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
  buy only enough fuel to just reach it, there's no reason to pay more now
  for fuel you could get cheaper shortly.
- **If nothing cheaper is reachable**, fill the tank completely, this is
  the best price available for as far as the vehicle can currently see.

The trip is reported as infeasible (`400`, not a wrong answer) if any gap
between usable stations, including start→first-stop and last-stop→destination,
exceeds the vehicle's range; no purchasing strategy can fix a genuine
coverage gap.

**Assumption: the vehicle departs with an empty tank.** This is a deliberate
choice, not an oversight, it's what makes "total money spent on fuel"
reflect the *entire* trip. (The alternative, starting full, would report
`$0` for any trip shorter than the vehicle's range, which isn't a useful
answer to the question being asked.) Because there's no earlier station to
have bought fuel from, the distance driven to reach the first stop is billed
at that first stop's price, this is handled as a one-time adjustment that
never causes a stop to exceed physical tank capacity (see the module
docstring for the exact mechanics; it's also directly tested).

Every numeric example in `planner/tests/test_fuel_optimizer.py` is hand
computed and asserted exactly (down to the cent/gallon), including the
scenario that caught a real bug during development, see below.

Candidate stations are sorted by **`(position, price)`**, not position
alone, see [Correctness pass](#correctness-pass-bugs-found-and-fixed-by-an-adversarial-self-review)
below for why that second sort key isn't optional: two stations tied on
position (plausible given the route is sampled at ~5mi resolution) used to
have the empty-tank "first leg" billing assigned to whichever one came
first in arbitrary DB row order, not the cheaper one, a 75% cost swing for
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
>    "retroactive" portion, see
>    `test_never_carries_more_than_a_full_tank_forward_from_any_stop`.
> 2. Sorting candidates by position alone made the result depend on
>    arbitrary input/database ordering whenever two stations tied on
>    position, see `test_tied_position_stations_do_not_change_cost_based_on_input_order`,
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
   database, CC BY 4.0), a **local, offline join, zero network calls**.

Result: **6,626 unique US stations, 6,223 (94%) geocoded for free**, with
zero API calls, in under a second.

The remaining ~400 stations (small/unincorporated places not in the free
city dataset) can optionally be filled in with a **one-time, rate-limited**
Nominatim (OpenStreetMap) pass:
```bash
python manage.py geocode_remaining_stations   # ~1 req/sec per Nominatim's usage policy
```
This is a deliberate, explicit, one-time data-quality command, never
something the live API calls per-request.

**Decommissioned stations.** A station that disappears from a future price
sheet (closed truck stop) is reported on every import (`N previously-imported
station(s) are not present in this file`) but left in place by default;
running `import_fuel_prices --prune-missing` actually removes them. This is
opt-in, not automatic: running the import against a partial/test CSV would
otherwise delete most of a real station table the moment `--prune-missing`
is the default.

**Known limitation:** station coordinates are city-level centroids, not
exact street addresses. Two truck stops in the same city will show
identical coordinates. This is a conscious trade-off for a take-home-sized
project with a free/no-API-key requirement, the corridor search and
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
instead of silently serving stale prices for up to the cache TTL, see
[Correctness pass](#correctness-pass-bugs-found-and-fixed-by-an-adversarial-self-review).

- **Routing:** [OSRM](http://project-osrm.org/) public demo server, free, no API key.
- **Geocoding fallback:** [Nominatim](https://nominatim.org/) (OpenStreetMap), free, no API key, used sparingly.

---

## Performance

Measured on a modest dev machine, non-scientifically but honestly:

- **Own computation (geometry processing + station corridor search),
  worst case** (a coast-to-coast route whose bounding box spans nearly the
  full width of the continental US, the least favorable case for the
  bbox pre-filter): **~260-290ms**, against all 6,223 geocoded stations.
  This was **~1050ms** before a profiling pass found the actual bottleneck
  (a denser-than-necessary route polyline sampling, station coordinates
  are already city-level centroids, so sub-5-mile path resolution wasn't
  buying real accuracy) and fixed it; see the comments in
  `planner/services/geometry.py`.
- **End-to-end, first request for a given trip:** dominated entirely by the
  one OSRM network round-trip (typically 1-3s for a long cross-country
  route on the public demo server; this is the free routing service's
  latency, not this codebase's).
- **End-to-end, repeat request for the same trip:** **well under 100ms**:
  the whole computed plan (geocoding + route + fuel plan) is cached
  (`ROUTE_CACHE_TTL_SECONDS`, default 1 hour), so identical requests never
  touch OSRM or Nominatim again until the cache expires *or the station data
  changes* (see below), and the persisted `RoutePlan` row is reused rather
  than duplicated, so a flood of identical/repeat requests doesn't grow the
  database unboundedly either.

Query-level choices behind this: indexed `(latitude, longitude)` and
`(state, city)` columns on `Station`, a cheap bounding-box pre-filter before
any heavy geometry math runs, `.only()` to avoid fetching unused columns,
and vectorized (numpy) distance computation instead of a per-station Python
loop.

**Two trade-offs worth calling out explicitly:**

- The data-version cache (5s TTL) means a price reimport can, in the
  absolute worst case, take up to 5 seconds to become visible to a cached
  trip, instead of instantly. That's a deliberate trade against hitting the
  DB on every single request (including cache hits), 5 seconds of possible
  staleness for a price update is a good trade for removing a DB round-trip
  from the hot path of every request.
- The concurrency lock (`cache.add()`-based mutex around computing a plan)
  is *best-effort*, not a hard guarantee. Its TTL is derived from the
  configured OSRM/Nominatim timeouts plus a safety margin, so it should
  comfortably outlive any realistic computation, but if something still
  runs long enough to outlast it, a second request can legitimately start
  a redundant computation alongside the first. A per-acquisition token
  stops that from cascading any further than the one genuine timeout,
  and either way, a waiting request that gives up after 10s just computes
  independently rather than hanging forever. The *correctness* guarantee
  (no duplicate rows) never depends on this lock at all, that's the
  database's `plan_key` uniqueness constraint, which holds regardless of
  what the cache layer does.

---

## Security

Secure-by-default, not secure-by-remembering-to-set-an-env-var:

- `DEBUG` defaults to `False` and `ALLOWED_HOSTS` defaults to
  `localhost,127.0.0.1,[::1]`, both fail *closed* (generic 500s / rejected
  Host headers) if you forget to configure them for a real deployment,
  rather than *open* (leaked stack traces and settings; a wildcard Host
  header accepted from anyone). Local dev opts **in** to `DEBUG=True` via
  `.env.example`, that's a deliberate choice you make by copying the file,
  not the out-of-the-box default.
- `python manage.py check --deploy` passes clean with zero warnings when the
  documented production env vars are set (`DJANGO_SECURE_SSL_REDIRECT`,
  `DJANGO_BEHIND_PROXY`, `DJANGO_SECURE_HSTS_SECONDS`, a real
  `DJANGO_SECRET_KEY`), see `config/settings.py`'s hardening block and
  `.env.example`. SSL redirect/HSTS are opt-in rather than on-by-default
  specifically because turning them on unconditionally the moment
  `DEBUG=False` would break `docker-compose up` (plain HTTP, no TLS
  termination in front of it) with an infinite redirect loop.
- CORS (`django-cors-headers`) is installed but allows **no origins** by
  default; opt in per-deployment via `CORS_ALLOWED_ORIGINS` or
  `CORS_ALLOW_ALL_ORIGINS` for local/demo use.
- Anonymous request throttling (`API_ANON_THROTTLE_RATE`, default 60/min)
  protects the free upstream services from being hammered by this API's own
  callers. It's keyed by `REMOTE_ADDR`, not by `X-Forwarded-For`
  (`NUM_PROXIES` is set to 0 by default), DRF trusts that header as-is
  when `NUM_PROXIES` isn't configured, which means a client could just send
  a different fake value on every request and never get throttled at all
  (checked this against a running server: it works). Behind a real reverse
  proxy/load balancer, set `DJANGO_TRUSTED_PROXY_COUNT` to how many proxy
  hops sit in front of the app so DRF only trusts that many entries from
  the end of the header instead of the client-supplied value directly.
  **Caveat:** throttle state lives in the configured cache backend; with
  the default `LocMemCache` (no `REDIS_URL` set) each worker process has
  its own counter, so running multiple gunicorn workers without Redis
  makes the effective limit `60/min × worker count`, not a true global
  60/min. `docker-compose.yml` sets `REDIS_URL`, so the bundled Postgres+Redis
  stack doesn't have this gap, it only applies if you serve the SQLite/no-Redis
  configuration with more than one worker process.
- **No authentication is implemented**, every endpoint is open, including
  the list endpoint, which is effectively a public log of every start/finish
  location anyone has queried (compounded by the fact that, before a fix
  described below, that log also grew without bound). This is a deliberate
  scope decision for a take-home assignment that never asked for user
  accounts, not an oversight, but it's a real limitation for anything
  beyond this exercise. Adding it would mean: an `IsAuthenticated` permission
  class, a `user` FK on `RoutePlan`, and scoping the list endpoint to
  `request.user`'s own plans.

---

## Testing

```bash
python manage.py test
```

81 tests, all offline (external HTTP calls are mocked with `unittest.mock`
at the service boundary, no test depends on network access or a live
database beyond Django's own test DB):

- `stations/tests/test_import.py`, CSV import: dedup-by-cheapest-price,
  US-only filtering, geocoding join, idempotent re-import, malformed rows,
  `DataImportLog` creation, `--prune-missing` (both the safe report-only
  default and the opt-in delete behavior), and that re-running the import
  doesn't erase geocoding a previous `geocode_remaining_stations` pass added.
- `stations/tests/test_geocode_remaining_command.py`, the Nominatim
  fallback command, mocked.
- `stations/tests/test_data_files.py`, sanity checks on the actual bundled
  data files (not the synthetic fixtures used elsewhere).
- `planner/tests/test_geo_math.py`, `test_geometry.py`, distance math and
  route-polyline processing.
- `planner/tests/test_station_finder.py`, corridor filtering against real
  `Station` rows, including a station the route passes near twice being
  offered at both encounters instead of just the nearer one.
- `planner/tests/test_fuel_optimizer.py`, **11 hand-computed scenarios**
  for the core algorithm (exact-value assertions, infeasibility, edge cases,
  and two regression tests for real bugs caught during development, see
  below).
- `planner/tests/test_geocoding.py`, local-lookup-first behavior, Nominatim
  fallback, caching (including a malformed-cache-entry regression test), all
  mocked.
- `planner/tests/test_routing.py`, that OSRM's `NoRoute`/`NoSegment` codes
  get the "no route exists" message while other error codes (e.g. `TooBig`)
  don't get mislabeled the same way.
- `planner/tests/test_throttling.py`, the anonymous throttle keys off
  `REMOTE_ADDR`, not a client-supplied `X-Forwarded-For`.
- `planner/tests/test_route_planner.py`, whole-plan caching, the
  data-version cache-invalidation-on-reimport regression test, the
  post-geocode same-location check, that a failed computation gets reused
  by a concurrent caller instead of being recomputed, and that the compute
  lock's cleanup can't steal a later request's lock out from under it.
- `planner/tests/test_query_normalization.py`, comma/whitespace/case
  canonicalization used for cache-key hashing.
- `planner/tests/test_api.py`, full request/response cycle through DRF,
  including validation errors, infeasible-trip errors, list/retrieve/map
  endpoints, cache-avoids-a-second-routing-call, plan-dedup-on-cache-hit,
  rounding-consistency, dangling-FK-after-deletion, explicit-null-mpg, and
  a DB-level unique-constraint regression test, plus
  `ConcurrentIdenticalRequestsTests`, an `APITransactionTestCase` that
  fires 8 real threads at the identical trip simultaneously (real commits,
  not the single wrapping transaction a plain `APITestCase` would use) and
  asserts exactly one `RoutePlan` row, one upstream routing call, and a
  correct 200/201 status split come out the other side.

---

## Bugs I found and fixed along the way

I went back over this a few times after the initial build looking for
things that were actually wrong rather than just missing test coverage,
and kept finding real ones, some in the original code, a couple hiding
inside earlier fixes. Logging them here instead of quietly folding them in,
since a few of these are the kind of thing a passing test suite and a
"provably optimal" claim can both hide.

**First pass, right after the initial build:**

| # | Issue | Severity | Fix |
|---|---|---|---|
| 1 | The "optimal" fuel algorithm wasn't actually deterministic. Two stations tied on route position (plausible at ~5mi route sampling) had the empty-tank first-leg billing assigned by whatever order they came out of the DB in, not price. Flipping row order on an otherwise identical trip changed the total cost by 75%. | Critical | Sort candidates by `(position, price)`, not position alone. |
| 2 | Whole-plan caching served stale prices with no invalidation. After a price reimport, a cached plan (up to 1hr TTL) kept returning the old price. | Critical | Cache key now folds in a DB-backed "data version" (`DataImportLog`, bumped every import), a reimport invalidates every cached plan automatically. |
| 3 | A cached, since-deleted Station reference crashed persistence with a raw FK `IntegrityError`. `on_delete=SET_NULL` doesn't cover this, it only fires when an *existing* referencing row's target is deleted, not a brand-new row created against an already-gone id. | Critical | Re-check each referenced station still exists right before the write; degrade its FK to `None` (keeping the denormalized snapshot fields) instead of crashing. |
| 4 | A malformed cache entry crashed with an unhandled `TypeError`, which didn't match the rest of the app's "callers get a clean error, not a 500" behavior. | Critical | Treat any cache-deserialization failure as a miss (log it, recompute, self-heal) instead of raising. |
| 5 | Every POST created new DB rows, even on a cache hit, repeat/duplicate requests grew the table forever. | High | A cache hit now reuses the already-persisted plan (`200`); only a genuine new computation persists (`201`). |
| 6 | "Start ≠ finish" validation was a plain string compare. `"Chicago, IL"` vs `"Chicago, Illinois"` sailed through as a "valid" trip with 0 miles and $0 cost. | High | Added a post-geocode distance check (under a mile apart = rejected), on top of the existing string check. |
| 7 | `total_cost`/`total_gallons` could disagree with the sum of their own line items, two numbers independently rounded from the same unrounded float don't always agree to the cent. | High | Totals are now the sum of the already-rounded per-stop values, never rounded separately. |
| 8 | Insecure settings defaults. `DEBUG` defaulted to `True`, `ALLOWED_HOSTS` to `"*"`, both fail *open* if a deployment forgets to set an env var. | High | Both now default to the secure option; `manage.py check --deploy` passes clean once the documented env vars are set. |
| 9 | Decommissioned stations lived forever, import only ever upserted, never noticed a station had disappeared from a newer file. | Medium | `import_fuel_prices` now always reports stations missing from the current import, and deletes them with an explicit `--prune-missing` flag. |
| 10 | No CORS configuration, a browser frontend couldn't call this API cross-origin at all. | Medium | Added `django-cors-headers`, no origins allowed by default, opt-in via env var. |
| 11 | Misleading error for a genuine "no route exists" case (e.g. Hawaii↔mainland), OSRM's public server answers this with an HTTP 400, which got caught by the generic "service unavailable" handler instead of an accurate message. | Low/Medium | Parse the structured OSRM error body regardless of HTTP status before falling back to a generic message. |
| 12 | Nominatim requires a real User-Agent under its usage policy; the shipped default is an obvious placeholder that could get the app blocked with no warning. | Low | Logs a loud, once-per-process warning the first time a call goes out with the placeholder still set. |

**Second pass**, digging specifically for anything the first pass missed,
including inside its own fixes. Found the worst bug in this project here:

| # | Issue | Severity | Fix |
|---|---|---|---|
| 1 | Race condition: concurrent identical requests created duplicate `RoutePlan` rows and each fired its own OSRM call. Fix #5 above made a cache *hit* reuse the persisted plan, but said nothing about two requests racing on the same cache *miss*, both compute, both `INSERT`. Confirmed with real threads and again against a live `runserver`: N simultaneous identical POSTs, N rows, N upstream calls. | Critical | Added a DB-enforced `plan_key` (`unique=True`). Persistence now inserts, catches the `IntegrityError` if it loses the race, and fetches the winner's row instead, the database's uniqueness guarantee is what actually prevents duplicates, since an application-level check can't be atomic under real concurrency. A `cache.add()` mutex sits on top purely to avoid the redundant OSRM calls while a computation is in flight. Verified with a mocked-threading test and a live 8-thread run: exactly 1 row, 1 upstream call. |
| 2 | The data-version fix above (#2) added a DB query to *every* request, including cache hits, defeating half the point of caching. | Medium | The data-version lookup is now itself cached for 5 seconds, with the import command proactively clearing it the moment a reimport finishes. |
| 3 | Cache keys were sensitive to cosmetic differences. `"Chicago, IL"` and `"Chicago,IL"` (no space) hashed to different entries, needless duplicate Nominatim calls and duplicate plans for what's obviously the same request. | Medium | Added `normalize_query()` (whitespace/comma/case normalization) ahead of every cache-key hash. |
| 4 | Explicit JSON `null` for `mpg`/`vehicle_range_miles`/`corridor_miles` got rejected with a 400, even though just omitting the key worked and used the same default. | Medium | `allow_null=True` on all three fields. |
| 5 | `--prune-missing`'s "what's gone" query used one big `exclude(opis_id__in=...)`, fine at ~6.6k rows, but SQLite caps bound variables per statement, so this would silently break on a larger source file. | Low/Medium | Compute the stale-id set in Python and delete in bounded chunks of 500. |
| 6 | `DataImportLog` had no admin visibility, and `RoutePlan`'s admin had a broken "Add" page (every field read-only, no Save button). | Low | Registered `DataImportLogAdmin` read-only; disabled the add page. |

That race condition is worth sitting with for a second: a green test suite
and the "cache hit reuses the persisted plan" fix from the pass right above
both made this look solved, right up until real concurrent requests hit it.
"Check, then act" is never enough under real concurrency, only a database
constraint (or real transactional isolation) actually is.

**Third pass.** Went back over it one more time and found six more:

| # | Issue | Severity | Fix |
|---|---|---|---|
| 1 | The anon rate limiter, whose entire job is protecting OSRM/Nominatim from abuse, was trivially bypassable. DRF trusts `X-Forwarded-For` as-is when `NUM_PROXIES` isn't set, so a client sending a different fake value on every request never got throttled at all. | Critical | Set `NUM_PROXIES=0` by default (ignore the header, key off `REMOTE_ADDR`), configurable via `DJANGO_TRUSTED_PROXY_COUNT` for real deployments behind a proxy. |
| 2 | Re-running `import_fuel_prices`, a normal thing to do, e.g. a price refresh, silently wiped out any station previously enriched by `geocode_remaining_stations`, since the rebuilt rows always overwrote lat/lng/geocode_source, even when the fresh lookup came back empty. | Critical | Look up each station's existing geocoding before rebuilding; keep it when the city-reference lookup doesn't have an answer. |
| 3 | The round-2 compute-lock released unconditionally. If a computation ever outlived the lock's TTL, a second request could legitimately grab it, and then the first request's cleanup would delete the *second* request's lock, letting a third steal it too, with no bound on how far that could cascade. | High | Each acquisition gets its own token; release only checks and deletes if it's still the caller's own lock. Also widened the TTL to track the actual configured OSRM/Nominatim timeouts instead of a flat guess, so the underlying steal itself should be much rarer too. |
| 4 | Only successful computations were cached. A request that lost the compute-lock race and then found the winner had *failed* just repeated the same failing pipeline itself instead of reusing the answer, timed one losing request at 10.4s to fail, vs. 0.2s for the request that failed first. | High | Cache `PlannerError` failures too, for a short TTL, a losing request now fails about as fast as the winner did. |
| 5 | Any non-`"Ok"` OSRM response code got the same "no driving route exists between these locations" message, true for `NoRoute`/`NoSegment`, but not for a request/capacity problem like `TooBig`, which isn't a geographic fact at all. | Medium | Only `NoRoute`/`NoSegment` get the "no route" message now; everything else says the routing service couldn't process the request. |
| 6 | `station_finder`'s nearest-point search collapsed a station to a single position even when the route genuinely passes near it twice (a spur, a cloverleaf, two legs running close together), silently dropping whichever encounter wasn't the closest match. | Low/Medium | Split a station's in-corridor route points into separate encounters when there's a real gap between them, and report each one as its own candidate. |

---

## Design decisions & known trade-offs

- **Django 5.2 (LTS) over 6.0.** 6.0 was newer at build time; 5.2 is the
  currently supported LTS (through April 2028). For a "production-ready"
  backend service, I'd rather ship on the boring, long-supported release
  than the newest `.0`.
- **SQLite by default, Postgres via one env var.** `DATABASE_URL` (via
  `dj-database-url`) switches the whole app to Postgres with no code
  changes, reviewers get a zero-dependency quick start; `docker-compose up`
  gets the real stack the team runs in production.
- **City-level geocoding, not per-address.** See
  [Data pipeline](#data-pipeline--geocoding-strategy) above.
- **A bounding-box pre-filter, not PostGIS.** ~6.6k stations is small enough
  that a plain indexed lat/lng range query + vectorized numpy distance math
  comfortably meets the performance goal without adding a PostGIS dependency.
  At meaningfully larger scale (hundreds of thousands of stations), I'd
  reach for PostGIS's spatial index instead of pushing the numpy approach
  further, noted in "What I'd do next".
- **RoutePlan/FuelStop persist every *distinct* computed plan** (deduplicated
  by resolved inputs + current data version, see the [correctness pass](#correctness-pass-bugs-found-and-fixed-by-an-adversarial-self-review)
  above), GET-by-id, a request history, and something to render the map view
  from, without the table growing purely from cache hits or repeat requests.
- **FuelStop denormalizes station details at plan-creation time**
  (name/address/price copied in, not just a foreign key) so a historical
  plan keeps showing exactly what was true when it was computed, even after
  a later `import_fuel_prices` run changes that station's price, and, since
  the cache-invalidation fix above, that's now actually guaranteed rather
  than being an aspiration the cache could quietly undermine.
- **Pruning decommissioned stations is opt-in, not automatic.** Detecting
  "missing from this import" is safe to always report; *deleting* on that
  basis is only safe with a complete, authoritative source file, so it's a
  deliberate `--prune-missing` flag rather than default behavior.
- **No authentication.** See [Security](#security) above, a deliberate,
  disclosed scope decision, not an oversight.

---

## What I'd do next (with more time / at real production scale)

- **Authentication and per-user scoping**, `IsAuthenticated`, a `user` FK
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
  a paid provider) for production traffic, the public server is fine for
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
