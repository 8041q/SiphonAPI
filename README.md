# fuel-prices-api

A free, open-source, non-commercial data pipeline that turns Portugal's and
Spain's official fuel price feeds into a simple, versioned, geo-partitioned
GeoJSON API - hosted entirely on GitHub, no server required.

A scheduled GitHub Action fetches both government sources, normalizes them
into one schema, and commits the result back into this repo. Because it's a
public repo, the data is then servable as a free CDN-backed API via
[jsDelivr](https://www.jsdelivr.com/):

```
https://cdn.jsdelivr.net/gh/YOUR-USERNAME/YOUR-REPO@main/data/es/manifest.json
https://cdn.jsdelivr.net/gh/YOUR-USERNAME/YOUR-REPO@main/data/es/grid_40_-3.geojson
https://cdn.jsdelivr.net/gh/YOUR-USERNAME/YOUR-REPO@main/data/pt/manifest.json
https://cdn.jsdelivr.net/gh/YOUR-USERNAME/YOUR-REPO@main/data/pt/district_lisboa.geojson
```

## Sources & credit

- **Portugal**: [DGEG](https://precoscombustiveis.dgeg.gov.pt) (Direção-Geral
  de Energia e Geologia). DGEG states this data is free to use but **may not
  be used commercially** - fine for this project, but keep that in mind if
  you build on top of it.
- **Spain**: [MINETUR](https://sedeaplicaciones.minetur.gob.es) (Ministerio
  de Industria, Comercio y Turismo).

This repo's own code (see `LICENSE`) - covers the
*code*, not the underlying price data, which stays subject to DGEG's and
MINETUR's own terms.

## Why a repo instead of a server

- Zero hosting cost, zero server to maintain.
- Git gives you a free, browsable history of price changes.
- jsDelivr gives real CDN caching in front of a plain GitHub repo.
- The scheduled fetch, run once for everyone, naturally rate-limits calls
  to the two government APIs instead of every app install hitting them
  directly.

## How the "smart fetching" works

**Spain** publishes one JSON dump for the whole country, stamped with a
single `Fecha` timestamp, refreshed about once a day. `fetch_spain.py`
checks that timestamp against the last one it saw (`state/es_last_fetch.json`)
and does nothing at all - no parsing, no writes, no commit - on every run
where it hasn't changed.

**Portugal** has no single "anything changed" flag; each station has its
own `DataAtualizacao`. `fetch_portugal.py` keeps the last-seen timestamp per
station+fuel in `state/pt_stations.json` and only rewrites the district
files that actually contain a changed station.

In both cases, `common.write_json_if_changed()` also compares file content
directly before writing, so even a full rebuild never produces a git diff
unless something genuinely changed.

The hourly workflow (`update-data.yml`) then only commits/pushes if
`git diff` actually finds something - most hourly runs will do nothing.

A separate `keepalive.yml` runs monthly and makes a one-line timestamp
commit **only if** there's been no real commit in 45+ days, since GitHub
auto-disables scheduled workflows after 60 days of total repo inactivity.

## Running locally

```bash
pip install -r requirements.txt
python scripts/fetch_spain.py
python scripts/fetch_portugal.py
```

## Data format

Each `.geojson` file is a standard `FeatureCollection` of `Point` features -
openable directly in GitHub's own file viewer as a map, and consumable by
any mapping library (Leaflet, Mapbox, `react-native-maps`, etc.).

```json
{
  "type": "Feature",
  "geometry": { "type": "Point", "coordinates": [lng, lat] },
  "properties": {
    "id": "pt-67360",
    "source": "PT",
    "brand": "INTERMARCHÉ",
    "address": "...",
    "fuels": { "gasoline95": 1.729 },
    "lastUpdated": "2026-07-22 08:40"
  }
}
```

Spain is partitioned into 1x1 degree grid tiles (`data/es/grid_{lat}_{lng}.geojson`),
Portugal into districts (`data/pt/district_{name}.geojson`), each with a
`manifest.json` listing what's available. A mobile client should fetch the
manifest first, then only the tiles/districts it actually needs.
