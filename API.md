# Siphon API

This *is* the API: the app reads files straight out of it over HTTPS
(`https://raw.githubusercontent.com/<user>/<repo>/main/<path>`)
This document describes the file layout and the schema to support that, plus the polling algorithm a client should
use so it never has to download more than it needs.

## Layout

```
manifest.json                                 <- root. Poll/conditional-GET this.
data/es/manifest.json                         <- Spain: one entry per 1°x1° grid tile
data/es/grid_{latFloor}_{lngFloor}.geojson     <- e.g. grid_40_-3.geojson
data/pt/manifest.json                         <- Portugal: one entry per district
data/pt/district_{name}.geojson               <- e.g. district_lisboa.geojson
state/*                                       <- internal bookkeeping for fetch scripts only. Not part of the API
```

## Why three tiers

There is a small hash embedded at every level, so each tier only needs to be opened when the one above it says something moved/changed:

```
manifest.json  --(hash differs?)-->  data/{es,pt}/manifest.json  --(hash differs?)-->  the one .geojson file that actually changed
```

## Schemas

### `manifest.json` (root)

```json
{
  "version": 1,
  "generatedAt": "2026-07-26T04:17:03+00:00",
  "countries": {
    "ES": { "manifest": "data/es/manifest.json", "hash": "…sha256…", "lastUpdated": "…" },
    "PT": { "manifest": "data/pt/manifest.json", "hash": "…sha256…", "lastUpdated": "…" }
  }
}
```

`generatedAt` only advances when a country's `hash` actually changes, not on every hourly run - it's safe to read as "last real change," not "last time the workflow happened to fire."

### `data/es/manifest.json`

```json
{
  "lastUpdated": "26/07/2026 08:15:00",
  "tileCount": 187,
  "tiles": {
    "grid_40_-3": {
      "path": "data/es/grid_40_-3.geojson",
      "stationCount": 214,
      "bbox": [-3.9, 40.1, -3.0, 40.9],
      "hash": "…sha256…"
    }
  }
}
```

`lastUpdated` is MINETUR's own `Fecha` field - Spain publishes one snapshot a day, so this barely changes.

### `data/pt/manifest.json`

```json
{
  "generatedAt": "2026-07-26T04:15:40+00:00",
  "dataUpdatedThrough": "26-07-2026 07:40:00",
  "stationCount": 3421,
  "districts": {
    "lisboa": {
      "path": "data/pt/district_lisboa.geojson",
      "stationCount": 812,
      "bbox": [-9.5, 38.6, -9.0, 39.0],
      "hash": "…sha256…"
    }
  }
}
```

`dataUpdatedThrough` is the newest `DataAtualizacao` seen across all PT stations (a data-freshness signal)
`generatedAt` is when the workflow last found a real change. Portugal has no single feed-wide timestamp the way Spain has `Fecha`


## Client algorithm

1. Conditional GET `manifest.json` (send `If-None-Match` with whatever
   `ETag` you got last time). A `304` means nothing changed anywhere. stop, one request, near-zero bytes.
2. On `200`, compare each country's `hash` to the copy you cached locally.
   Unchanged countries: skip entirely.
3. For a changed country, fetch its `data/{es,pt}/manifest.json` and diff
   `tiles`/`districts` entries against your cached copy of *that* manifest.
4. Only fetch the `.geojson` files whose `hash` changed
5. Cache the new manifests (and their ETags) for next time's diff.

Worst case (something relevant changed): 3 requests - root, country manifest, one tile file. Common case (nothing changed): 1 request, `304`.

## Spatial selection

- **Spain**: the grid key is computable directly from a location --
  `grid_{floor(lat)}_{floor(lng)}` (same formula as `grid_key()` in `fetch_spain.py`).
  No need to consult `bbox` at all; just build the key and look it up.
- **Portugal**: districts are administrative, not geometric, so there's no
  formula. Use each district's `bbox` as a cheap prefilter. Only fetch the districts
  that match.

## Rate limits & caching

GitHub tightened rate limits on unauthenticated `raw.githubusercontent.com`
requests:

- A conditional request that comes back `304` does **not** count against
  the limit, so always send `If-None-Match` once you have an ETag.
- The algorithm above is designed to need very few requests per check
  regardless - lean on the manifest hashes rather than re-fetching things "just in case."


### Historical price-color references

`data/commodities/dashboard.json` includes optional `priceBenchmarks` (schema 1).
It is fetched and cached with the existing dashboard; no extra mobile request is
required. Each `bands` entry is keyed by exact fuel and country, e.g. `diesel_pt`:

```json
{"fuel":"diesel","country":"PT","unit":"EUR/L","greenBelow":1.4429,"redAbove":1.745,"reference":1.5301,"referenceMonths":120,"inflationMonth":"2026-09"}
```

These illustrative boundaries are not constants: clients use the published
values. Below `greenBelow` is green, above `redAbove` is red, and the inclusive
interval between them is amber. Missing, malformed, unknown-method, incompatible
unit or older-than-120-day references are neutral in the app. No premium fuel is
aliased to regular fuel. Portugal LPG is not covered because its station price
unit is kg rather than the Oil Bulletin's litres.

Method `anchored_real_net_price_quartiles`: the fixed 2010–2019 tax-exclusive
weekly series is converted to monthly medians in non-energy-inflation-adjusted
terms. The lower/upper quartiles of those monthly medians are converted to pump
prices with the latest applicable VAT, excise and other indirect taxes. At least
96 reference months are required. Recent station averages, rankings and crude
prices never change these bands. A broad price surge can therefore leave every
station red. The reference period only changes through an explicit methodology
revision, not a rolling window. This is a historical price-level comparison,
not an affordability promise or a forecast.

The API refreshes from the European Commission Weekly Oil Bulletin and Eurostat
at most once every seven days. `state/price-benchmarks.json` holds the weekly
cache; `data/commodities/price-benchmarks.json` holds the reference embedded in the
dashboard. Source outages retain the last good reference and do not stop station
publishing. Rechecking HTTP alone does not refresh `asOf`; that date follows the
source observation. New calibration rejects source observations older than 30
days and inflation older than 120 days.

Validation: `python -m unittest discover -s tests`. The workflow runs these tests
before fetching data. Historical checks are recorded in
[price-benchmark-validation.md](docs/price-benchmark-validation.md).
