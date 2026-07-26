# fuel-prices-api

A free, open-source, non-commercial data pipeline that fetches official fuel price feeds from **Portugal** and **Spain**, normalizes them into a unified schema, and publishes them as versioned, geo-partitioned **GeoJSON** APIs. Hosted entirely on GitHub.

A scheduled GitHub Action runs the fetchers, normalizes the data, and commits the output back to this repository. The resulting files can be served globally via free CDN networks like [jsDelivr](https://www.jsdelivr.com/):

```text
https://cdn.jsdelivr.net/gh/YOUR-USERNAME/YOUR-REPO@main/data/es/manifest.json
https://cdn.jsdelivr.net/gh/YOUR-USERNAME/YOUR-REPO@main/data/es/grid_40_-3.geojson
https://cdn.jsdelivr.net/gh/YOUR-USERNAME/YOUR-REPO@main/data/pt/manifest.json
https://cdn.jsdelivr.net/gh/YOUR-USERNAME/YOUR-REPO@main/data/pt/district_lisboa.geojson
```


## How Smart Fetching Works

Both scripts rely on smart caching and content-hashing to keep GitHub commits minimal and prevent redundant requests. As to also avoid rate limits.

* **Network Resiliency (`common.py`)**: Standardizes HTTP sessions with browser User-Agent impersonation (preventing Web Application Firewall drops) and automated exponential backoff retries for transient HTTP errors (429, 500, 502, etc.).
* **Content Hashing (`common.py`)**: `write_json_if_changed()` hashes output objects via SHA-256 before writing to disk, ensuring unchanged data does not trigger a disk write or Git diff.
* **Spain (`fetch_spain.py`)**: Spain (MINETUR) publishes a national dataset with a global `Fecha` timestamp. The script compares this timestamp with `state/es_last_fetch.json` and short-circuits execution if the timestamp is unchanged.
* **Portugal (`fetch_portugal.py`)**: DGEG updates stations granularly. Per-station fuel update timestamps (`DataAtualizacao`) are tracked in `state/pt_stations.json`.
* **Station Enrichment (`fetch_portugal.py`)**: Portugal stations undergo an additional enrichment pass (`state/pt_enrichment.json`) to pull operating hours, payment methods, amenities, and observations. Cached enrichment data is decoupled from prices and re-fetched only when older than **30 days** (`ENRICHMENT_MAX_AGE_DAYS`) or for newly discovered stations, using polite rate limiting (`0.15s` delay between station lookups).


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

#### Gasoline
* `gasoline95` / `gasoline95E10` / `gasoline95E25` / `gasoline95E85` / `gasoline95Premium`
* `gasoline98` / `gasoline98E10` / `gasoline98Plus`
* `gasolineMix` (2-stroke)
* `gasolineRenewable`

#### Diesel
* `diesel` (Standard Gasóleo / Gasoleo A)
* `dieselPremium` (Especial / Premium)
* `dieselB` / `dieselAgri` (Agricultural)
* `dieselHeating` (Aquecimento)
* `dieselRenewable`

#### Gas & Alternative Energies
* `lpg` (GPL Auto)
* `cng`(m3/kg) / `bioCng` (Compressed Natural Gas / Bio-CNG)
* `lng` / `bioLng` (Liquefied Natural Gas / Bio-LNG)
* `hydrogen`
* `bioethanol` / `biodiesel`
* `adblue`
* `ammonia` / `methanol`

##  Sources & Data Licenses

* **Portugal**: Data provided by [DGEG](https://precoscombustiveis.dgeg.gov.pt) (Direção-Geral de Energia e Geologia). DGEG data is free to use for **non-commercial applications**.
* **Spain**: Data provided by [MINETUR](https://sedeaplicaciones.minetur.gob.es) (Ministerio de Industria, Comercio y Turismo). Spanish public dataset used under open government data terms (attribution required).

*Note: This repository's source code is distributed under its explicit license file (`LICENSE`), which applies strictly to the code execution logic - not to the underlying government price datasets.*