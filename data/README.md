# Data files

- **`fuel_prices.csv`**, the OPIS fuel price sheet provided with the
  assignment. Imported via `python manage.py import_fuel_prices`.

- **`uscities.csv`**, a free US city/state → latitude/longitude reference
  (~37.5k unique city/state pairs), used to geocode stations at the city
  level with zero external API calls. Derived from the "Basic" (free) tier
  of [SimpleMaps' US Cities database](https://simplemaps.com/data/us-cities),
  licensed CC BY 4.0. Trimmed down from the original ~37.5k-row source
  (which includes many extra columns: county, timezone, ZIP codes, etc.) to
  just `city, state_id, lat, lng, population`, the columns
  `stations/geodata.py` actually uses, and the `population` column is only
  used to pick the more likely candidate when the same city/state name
  appears more than once in the source (e.g. picking the well-known
  Chicago, IL over an unrelated hamlet that happens to share a name).
