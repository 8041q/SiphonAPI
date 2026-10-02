# Historical price-color validation

Reference: fixed 2010–2019 monthly median tax-exclusive pump prices; Eurostat HICP excluding energy; current applicable VAT, excise and other indirect taxes. Bands use historical lower/upper quartiles. Recent pump prices never enter calibration.

Backtest uses published historical tax rates and inflation available for each observation month, not present-day taxes. This validates historical price comparisons, not household affordability or a market forecast.

| Observation | Fuel / country | Pump €/L | Green below | Red above | Result |
|---|---|---:|---:|---:|---|
| 2022-06-20 | gasoline95_es | 2.142 | 1.3206 | 1.5331 | red |
| 2022-06-20 | diesel_es | 2.077 | 1.2260 | 1.4833 | red |
| 2022-06-20 | gasoline95_pt | 2.121 | 1.2958 | 1.5074 | red |
| 2022-06-20 | diesel_pt | 2.080 | 1.0045 | 1.2650 | red |
| 2025-01-06 | gasoline95_es | 1.536 | 1.3838 | 1.6142 | amber |
| 2025-01-06 | diesel_es | 1.455 | 1.2908 | 1.5698 | amber |
| 2025-01-06 | gasoline95_pt | 1.746 | 1.5956 | 1.8240 | amber |
| 2025-01-06 | diesel_pt | 1.633 | 1.4952 | 1.7763 | amber |
| 2026-09-28 | gasoline95_es | 1.940 | 1.3754 | 1.6206 | red |
| 2026-09-28 | diesel_es | 1.934 | 1.1023 | 1.3992 | red |
| 2026-09-28 | gasoline95_pt | 2.099 | 1.5720 | 1.8174 | red |
| 2026-09-28 | diesel_pt | 2.186 | 1.4429 | 1.7450 | red |

The June 2022 price surge is red for regular gasoline and diesel in both countries. The January 2025 observations are amber. These spot checks supplement the automated fixed-reference, inflation, taxes, missing-data, cache/outage and pipeline tests; they do not claim exhaustive validation across all market conditions.

The downloaded XLSX inputs are not added to the repository. Published metadata records the reference period, sample months, inflation month, tax components and source date. Premium and alternative fuels without a comparable history stay neutral; Portugal LPG is €/kg and must never borrow the €/L series.

Sources: [Weekly Oil Bulletin](https://energy.ec.europa.eu/data-and-analysis/weekly-oil-bulletin_en), [Eurostat HICP](https://ec.europa.eu/eurostat/web/hicp/information-data).
