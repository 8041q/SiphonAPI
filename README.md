# fuel-prices-api

A free, open-source, non-commercial data pipeline that fetches official fuel price feeds from **Portugal** and **Spain**, normalizes them into a unified schema, and publishes them as versioned, geo-partitioned **GeoJSON** APIs. Hosted entirely on GitHub.

A scheduled GitHub Action runs the pipeline end-to-end and commits the output back to this repo: the two fetchers write today's prices, `build_history.py` folds them into the daily price-history archive, and `build_manifest.py` ties both countries' manifests and the history index into one root file. Everything is then served globally via free CDN networks like [jsDelivr](https://www.jsdelivr.com/):

```text
https://cdn.jsdelivr.net/gh/YOUR-USERNAME/YOUR-REPO@main/manifest.json
https://cdn.jsdelivr.net/gh/YOUR-USERNAME/YOUR-REPO@main/data/es/manifest.json
https://cdn.jsdelivr.net/gh/YOUR-USERNAME/YOUR-REPO@main/data/es/grid_40_-3.geojson
https://cdn.jsdelivr.net/gh/YOUR-USERNAME/YOUR-REPO@main/data/pt/manifest.json
https://cdn.jsdelivr.net/gh/YOUR-USERNAME/YOUR-REPO@main/data/pt/district_lisboa.geojson
https://cdn.jsdelivr.net/gh/YOUR-USERNAME/YOUR-REPO@main/data/history/index.json
```

The root `manifest.json` is the one file a client needs to poll: it embeds a hash for each country's manifest plus the history index, so you know whether *anything* changed anywhere without downloading a single tile.

## How Smart Fetching Works

Both fetchers rely on caching and content-hashing to keep GitHub commits minimal and avoid hammering the source APIs.

* **Network resiliency (`common.py`)** - a shared HTTP session sends a standard browser User-Agent (avoids WAF drops) and retries transient errors (429/500/502/503/504) with exponential backoff.
* **Content hashing (`common.py`)** - `write_json_if_changed()` hashes each output object (SHA-256, order-independent) before writing, so unchanged data never triggers a disk write or Git diff.
* **Spain (`fetch_spain.py`)** - MINETUR publishes one national dataset with a global `Fecha` timestamp. The script compares it against `state/es_last_fetch.json` and short-circuits only if that timestamp *and* the overrides file are both unchanged.
* **Portugal (`fetch_portugal.py`)** - DGEG updates stations individually. Per-station, per-fuel `DataAtualizacao` timestamps are tracked in `state/pt_stations.json`; a station is only rewritten when one of its fuels actually moved (or the overrides file changed).
* **Portugal enrichment** - a separate pass fetches hours, payment methods, amenities and observations per station, cached in `state/pt_enrichment.json`. It's decoupled from price checks and only re-fetched when a record is new or older than **30 days** (`ENRICHMENT_MAX_AGE_DAYS`), at a polite `0.15s` delay between station lookups.
* **History (`build_history.py`)** - runs after both fetchers. Writes one flat `{id, brand, fuels}` snapshot per day to `data/history/{YYYY}/{YYYY-MM-DD}.json`, skipping the write if it's identical to any of the previous 3 days. Old years are never auto-pruned - delete a year folder by hand if you want to.
* **Manifest (`build_manifest.py`)** - runs last, combining both countries' manifests and the history index hash into the root `manifest.json`. Its `generatedAt` timestamp only advances when something real changed underneath it, not on every scheduled run.

## Data Schema

Every `.geojson` file is a valid GeoJSON `FeatureCollection` containing `Point` features.

```json
{
  "type": "Feature",
  "geometry": {
    "type": "Point",
    "coordinates": [-9.1393, 38.7223]
  },
  "properties": {
    "id": "pt-67360",
    "source": "PT",
    "name": "Station Name",
    "brand": "GALP",
    "address": "Av. Liberdade",
    "municipality": "Lisboa",
    "district": "Lisboa",
    "postalCode": "1250-001",
    "lastUpdated": "2026-07-22 08:40",
    "fuels": {
      "gasoline95": 1.729,
      "diesel": 1.589,
      "lpg": 0.849
    },
    "hours": {
      "weekdays": "07:00-22:00",
      "saturday": "08:00-20:00",
      "sunday": "08:00-20:00",
      "holiday": "Closed"
    },
    "services": ["Car Wash", "Air Pump"],
    "paymentMethods": ["Credit Card", "Cash"],
    "otherServices": "Store",
    "observations": "Self-service late at night",
    "extra": {
      "stationType": "P"
    }
  }
}
```

### Supported Fuel Types Across Sources

| Category | Keys |
|---|---|
| Gasoline | `gasoline95` / `gasoline95E10` / `gasoline95E25` / `gasoline95E85` / `gasoline95Premium`, `gasoline98` / `gasoline98E10` / `gasoline98Plus`, `gasolineMix` (2-stroke), `gasolineRenewable` |
| Diesel | `diesel` (Standard Gasóleo / Gasoleo A), `dieselPremium` (Especial / Premium), `dieselB` / `dieselAgri` (Agricultural), `dieselHeating` (Aquecimento), `dieselRenewable` |
| Gas & Alternative Energies | `lpg` (GPL Auto), `cng` (m³/kg) / `bioCng` (Compressed / Bio-CNG), `lng` / `bioLng` (Liquefied / Bio-LNG), `hydrogen`, `bioethanol` / `biodiesel`, `adblue`, `ammonia` / `methanol` |

## Crowdsourced Overrides

Users report mistakes in station info through the [issue template](.github/ISSUE_TEMPLATE/incorrect-station-info.yml). A maintainer checks the report - confirming the station id (visible in the app's price-trends URL) and verifying the claim - then hand-edits `data/overrides/es.json` or `data/overrides/pt.json` directly. That edit *is* the approval step; there's no separate review pipeline. The automated part happens next: `apply_overrides()` in `common.py` re-validates every field's shape before it's ever published, and each fetch script tracks a hash of its overrides file in `state/` so a changed file triggers a reprocess on the next run - even if the underlying price data hasn't moved at all.

Each file is a JSON object keyed by station id:

```json
{
  "<stationId>": {
    "<field>": <value>,
    "appliedAt": "2026-08-01T10:00:00Z",
    "note": "issue #15"
  }
}
```

- `appliedAt` and `note` are optional, cosmetic - for the maintainer's own record-keeping. Nothing parses, validates, or publishes them.
- **Prices (`fuels`) can never be overridden** - there's no code path that allows it.
- Only list the fields you're actually correcting; anything left out is untouched.
- An override replaces the field exactly as published, so copy the shape from the station's own tile (`data/es/*.geojson` / `data/pt/*.geojson`) rather than inventing one.
- Unmatched ids are silently skipped - expected, since the ES and PT files never share ids and each run only matches its own country.

### Fields

| Field | Countries | Shape |
|---|---|---|
| `brand` | ES, PT | string |
| `address` | ES, PT | string |
| `schedule` | ES only | string, e.g. `"L-D: 24H; S-D: 24H"` |
| `name` | PT only | string |
| `hours` | PT only | object - `{ weekdays, saturday, sunday, holiday }`, each a string or `null` |
| `services` | PT only | array of strings |
| `paymentMethods` | PT only | array of strings |
| `otherServices` | PT only | string |
| `observations` | PT only | string |

A field valid for one country but sent for the other (e.g. `schedule` on a PT station) is rejected - it's not that country's field.

### Examples

Full correction (all 3 ES fields):

```json
{
  "es-12345": {
    "brand": "REPSOL",
    "address": "Av. de la Constitución 10, 28001 Madrid",
    "schedule": "L-D: 24H",
    "appliedAt": "2026-08-01T10:00:00Z",
    "note": "issue #17"
  }
}
```

Partial correction - only `paymentMethods` changes, everything else on the station stays as published:

```json
{
  "pt-67360": {
    "paymentMethods": ["Dinheiro", "Cartão de Crédito", "MB WAY"],
    "appliedAt": "2026-08-01T10:00:00Z",
    "note": "issue #15"
  }
}
```

**`hours` is the one exception to "only list what changed."** Its value is a single object, so correcting it replaces the whole thing - and the validator only checks that the keys you *do* send are among the four allowed ones (it doesn't require all four to be present). But the app reads `hours.weekdays` etc. directly, so a key you leave out of the object behaves like it's simply missing, not like a reported "closed." To safely fix just one day, send all four keys - copy the unchanged ones, use `null` for a day with no reported hours:

```json
{
  "pt-67360": {
    "hours": {
      "weekdays": "07:00-22:00",
      "saturday": "08:00-22:00",
      "sunday": "08:00-20:00",
      "holiday": null
    }
  }
}
```

`null` is only meaningful *inside* `hours`. Everywhere else it's rejected - see below.

### Omit vs. clear vs. reject

| You write | Result |
|---|---|
| Field left out entirely | Untouched - current published value stays |
| `""` or `[]` | Applied - clears the field, it disappears from the app |
| `null` | Rejected with a warning, current value stays (exception: `null` inside `hours` is valid) |

### Validation

`apply_overrides()` checks every field of every entry before applying it. Any of the following gets just *that field* rejected - logged as a warning and skipped, while the rest of that station's valid corrections still go through:

- the field isn't on the whitelist at all,
- the field is real but not one that country publishes,
- the value is the wrong shape for that field (e.g. a string where `hours` expects an object, or `null` anywhere outside `hours`).

Nothing rejected ever reaches the published tiles.

##  Sources & Data Licenses

* **Portugal**: Data provided by [DGEG](https://precoscombustiveis.dgeg.gov.pt) (Direção-Geral de Energia e Geologia). Free to use for **non-commercial applications**.
* **Spain**: Data provided by [MINETUR](https://sedeaplicaciones.minetur.gob.es) (Ministerio de Industria, Comercio y Turismo). Spanish public dataset used under open government data terms (attribution required).

*This repository's source code is distributed under its own `LICENSE` file, which applies strictly to the code - not to the underlying government price datasets.*