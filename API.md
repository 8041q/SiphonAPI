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

