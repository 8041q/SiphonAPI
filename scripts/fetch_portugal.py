# Fetches Portugal's DGEG fuel price feed.

# It filters by fuel type via `idsTiposComb`, and each station has its own `DataAtualizacao`.
# So the delta logic here is per-station: we keep the last-seen `DataAtualizacao` for every
# (station, fuel) pair in state/pt_stations.json, and only write output files
# when at least one of those actually changed.

# Output: one GeoJSON file per district under data/pt/, plus a manifest.json.

# manifest.json also carries a content hash + bbox + station count per
# district, plus generatedAt / dataUpdatedThrough freshness fields

import os
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))
from common import (  # noqa: E402
    apply_overrides,
    bbox_from_features,
    content_hash,
    fetch_json,
    load_json,
    make_session,
    parse_pt_price,
    validate_station_coords,
    write_json_if_changed,
)

BASE_URL = "https://precoscombustiveis.dgeg.gov.pt/api/PrecoComb/PesquisarPostos"
MAP_URL = "https://precoscombustiveis.dgeg.gov.pt/api/PrecoComb/GetDadosPostoMapa"

STATE_PATH = "state/pt_stations.json"
ENRICHMENT_STATE_PATH = "state/pt_enrichment.json"
OVERRIDES_STATE_PATH = "state/pt_overrides.json"
DATA_DIR = "data/pt"
OVERRIDES_PATH = "data/overrides/pt.json"

# How long a cached enrichment record is considered good before we bother
# DGEG for it again. Hours/services/payment methods change rarely, so this
# is decoupled from price changes on purpose
ENRICHMENT_MAX_AGE_DAYS = 30

# First run on a station at least once can be several thousand sequential requests
# A small delay keeps that from hammering DGEG's server all at once.
ENRICHMENT_REQUEST_DELAY_SECONDS = 0.15

FUEL_TYPES = {
    # Gasoline
    3201: "gasoline95",      # Gasolina simples 95
    3205: "gasoline95Plus",  # Gasolina especial 95
    3401: "gasoline98",      # Gasolina simples 98
    3400: "gasoline98Plus",  # Gasolina especial 98
    3210: "gasolineMix",     # Gasolina mistura (2-stroke)

    # Diesel
    2101: "diesel",          # Gasóleo simples
    2105: "dieselPremium",   # Gasóleo especial
    2155: "dieselHeating",   # Gasóleo de aquecimento
    2150: "dieselAgri",      # Gasóleo colorido e marcado (agrícola)
    2115: "bioDiesel",      # Biodiesel B15

    # Gas & Alternative
    1120: "lpg",             # GPL Auto
    1141: "cngm3",             # GNC (Gás Natural Comprimido - m3)
    1143: "cngkg",             # GNC (Gás Natural Comprimido - kg)
    1142: "lng",             # GNL (Gás Natural Liquefeito)
}

PAGE_SIZE = 10000  # above Portugal's total stations


def fetch_fuel(session, fuel_id):
    url = f"{BASE_URL}?idsTiposComb={fuel_id}&qtdPorPagina={PAGE_SIZE}"

    payload = fetch_json(session, url)
    print(
        fuel_id,
        payload.get("status"),
        len(payload.get("resultado") or []),
        payload.get("mensagem"),
    )
    return payload.get("resultado")


def _clean(value):
    # DGEG uses "-" as a placeholder for "nothing to report"
    if value in (None, "", "-"):
        return None
    return value


def _extract_descriptions(raw_list):
    # Servicos / MeiosPagamento both are either null or a list of "..."
    # this stays defensive and also accepts plain strings, just in case.
    if not raw_list:
        return []
    descriptions = []
    for item in raw_list:
        if isinstance(item, dict):
            desc = item.get("Descritivo")
            if desc:
                descriptions.append(desc)
        elif isinstance(item, str):
            descriptions.append(item)
    return descriptions


def fetch_station_enrichment(session, sid):
    # One call per station: hours, services, payment methods and notes.
    # Deliberately does NOT touch the endpoint's own Combustiveis/DataAtualizacao
    url = f"{MAP_URL}?id={sid}"
    payload = fetch_json(session, url)
    if not payload.get("status"):
        raise ValueError(payload.get("mensagem") or "DGEG returned status=false")
    result = payload.get("resultado") or {}

    horario = result.get("HorarioPosto") or {}
    hours = {
        "weekdays": horario.get("DiasUteis"),
        "saturday": horario.get("Sabado"),
        "sunday": horario.get("Domingo"),
        "holiday": horario.get("Feriado"),
    }
    if not any(hours.values()):
        hours = None

    return {
        "services": _extract_descriptions(result.get("Servicos")),
        "hours": hours,
        "paymentMethods": _extract_descriptions(result.get("MeiosPagamento")),
        "otherServices": _clean(result.get("OutrosServicos")),
        "observations": _clean(result.get("Observacoes")),
    }


def _is_stale(cached_entry):
    fetched_at = cached_entry.get("fetchedAt")
    if not fetched_at:
        return True
    try:
        fetched = datetime.fromisoformat(fetched_at)
    except ValueError:
        return True
    return datetime.now(timezone.utc) - fetched > timedelta(days=ENRICHMENT_MAX_AGE_DAYS)


def enrich_stations(session, stations, enrichment_cache):
    # Called once per unique station id (stations is already deduped), and
    # only actually hits the network for stations that are new or whose
    # cached copy has aged out -> see ENRICHMENT_MAX_AGE_DAYS.
    fetched = 0
    for i, (sid, station) in enumerate(stations.items(), 1):
        if i % 100 == 0:
            print(f"Enrichment: {i}/{len(stations)}")
            
        cached = enrichment_cache.get(sid)
        if cached is None or _is_stale(cached):
            try:
                enrichment = fetch_station_enrichment(session, sid)
                enrichment["fetchedAt"] = datetime.now(timezone.utc).isoformat()
                enrichment_cache[sid] = enrichment
                fetched += 1
                time.sleep(ENRICHMENT_REQUEST_DELAY_SECONDS)
            except Exception as exc:  # noqa: BLE001 - one bad station shouldn't kill the run
                print(f"Portugal: enrichment failed for station {sid} ({exc}); using cached/defaults.")
                enrichment = cached or {}
        else:
            enrichment = cached

        station["services"] = enrichment.get("services", [])
        station["hours"] = enrichment.get("hours")
        station["paymentMethods"] = enrichment.get("paymentMethods", [])
        station["otherServices"] = enrichment.get("otherServices")
        station["observations"] = enrichment.get("observations")

    if fetched:
        print(f"Portugal: fetched fresh enrichment for {fetched} station(s) "
              f"(new or older than {ENRICHMENT_MAX_AGE_DAYS} days).")
    return enrichment_cache


def run():
    session = make_session()
    state = load_json(STATE_PATH, default={})  # {fuel_key: DataAtualizacao}
    enrichment_cache = load_json(ENRICHMENT_STATE_PATH, default={})  # {sid: {...}}
    override_state = load_json(OVERRIDES_STATE_PATH, default={})  # {"hash": ...}

    stations = {}
    changed_ids = set()

    for fuel_id, fuel_key in FUEL_TYPES.items():
        for row in fetch_fuel(session, fuel_id):
            sid = str(row["Id"])
            updated = row.get("DataAtualizacao")

            station = stations.setdefault(
                sid,
                {
                    "id": f"pt-{sid}",
                    "source": "PT",
                    "name": row.get("Nome"),
                    "brand": row.get("Marca"),
                    "address": row.get("Morada"),
                    "municipality": row.get("Municipio"),
                    "district": row.get("Distrito"),
                    "postalCode": row.get("CodPostal"),
                    "lat": row.get("Latitude"),
                    "lng": row.get("Longitude"),
                    "fuels": {},
                    "lastUpdated": updated,
                    # Country-specific field with no Spanish equivalent
                    # kept out of the shared top-level schema on purpose.
                    "extra": {"stationType": row.get("TipoPosto")},
                },
            )

            price = parse_pt_price(row.get("Preco"))
            if price is not None:
                station["fuels"][fuel_key] = price
            if updated and updated > (station["lastUpdated"] or ""):
                station["lastUpdated"] = updated

            prev_updated = state.get(sid, {}).get(fuel_key)
            if updated != prev_updated:
                changed_ids.add(sid)
                state.setdefault(sid, {})[fuel_key] = updated

    overrides_hash = content_hash(load_json(OVERRIDES_PATH, default={}))
    if not changed_ids and override_state.get("hash") == overrides_hash:
        print("Portugal: no station updates found, skipping write.")
        return False

    if changed_ids:
        print(f"Portugal: {len(changed_ids)} station(s) changed.")
    else:
        print("Portugal: no station price updates; overrides changed, reprocessing.")

    # Enrichment pass -- once per unique station (see enrich_stations), not
    # once per fuel type. On a cold start (empty cache) this enriches every
    # station in `stations`, which can take a while; subsequent runs only
    # touch new stations or ones whose cache has aged out.
    enrich_stations(session, stations, enrichment_cache)

    by_district = {}
    stats = {"swapped": 0, "dropped": []}
    for sid, station in stations.items():
        if station["lat"] is None or station["lng"] is None:
            stats["dropped"].append(station["id"])
            continue
        validated = validate_station_coords("PT", station["lat"], station["lng"])
        if validated is None:
            stats["dropped"].append(station["id"])
            continue
        if validated != (station["lat"], station["lng"]):
            stats["swapped"] += 1
            station["lat"], station["lng"] = validated
        feature = {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [station["lng"], station["lat"]]},
            "properties": {k: v for k, v in station.items() if k not in ("lat", "lng")},
        }
        district = (station["district"] or "unknown").strip().lower().replace(" ", "_")
        by_district.setdefault(district, []).append(feature)

    changed_files = 0
    district_entries = {}
    data_updated_through = None
    for district, features in by_district.items():
        features.sort(key=lambda f: f["properties"]["id"])  # deterministic diffs
        features = apply_overrides(features, OVERRIDES_PATH, country="PT")
        geojson = {"type": "FeatureCollection", "features": features}
        path = os.path.join(DATA_DIR, f"district_{district}.geojson")
        if write_json_if_changed(path, geojson):
            changed_files += 1
        district_entries[district] = {
            "path": path.replace(os.sep, "/"),
            "stationCount": len(features),
            "bbox": bbox_from_features(features),
            "hash": content_hash(geojson),
        }
        for feature in features:
            updated = feature["properties"].get("lastUpdated")
            if updated and (data_updated_through is None or updated > data_updated_through):
                data_updated_through = updated

    manifest = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "dataUpdatedThrough": data_updated_through,
        "stationCount": len(stations),
        "districts": district_entries,
    }

    write_json_if_changed(os.path.join(DATA_DIR, "manifest.json"), manifest)
    write_json_if_changed(STATE_PATH, state)
    write_json_if_changed(ENRICHMENT_STATE_PATH, enrichment_cache)
    write_json_if_changed(OVERRIDES_STATE_PATH, {"hash": overrides_hash})

    print(f"Portugal: {changed_files}/{len(by_district)} district file(s) actually changed.")
    if stats["swapped"] or stats["dropped"]:
        print(f"Portugal: swapped {stats['swapped']} station(s).")
        print(f"Portugal: dropped {len(stats['dropped'])} station(s): {', '.join(stats['dropped'])}.")

    return True


if __name__ == "__main__":
    run()