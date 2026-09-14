# Fetches Portugal's DGEG fuel price feed.
#
# Delta logic is per (station, fuel), while slow-changing station enrichment is
# cached separately and may itself trigger a refresh when its TTL expires.
# Output: one GeoJSON file per district under data/pt/, plus a manifest.json.

import glob
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
    resolve_station_coordinates,
    write_json_if_changed,
)

BASE_URL = "https://precoscombustiveis.dgeg.gov.pt/api/PrecoComb/PesquisarPostos"
MAP_URL = "https://precoscombustiveis.dgeg.gov.pt/api/PrecoComb/GetDadosPostoMapa"

STATE_PATH = "state/pt_stations.json"
ENRICHMENT_STATE_PATH = "state/pt_enrichment.json"
OVERRIDES_STATE_PATH = "state/pt_overrides.json"
DATA_DIR = "data/pt"
OVERRIDES_PATH = "data/overrides/pt.json"
MANIFEST_PATH = os.path.join(DATA_DIR, "manifest.json")

ENRICHMENT_MAX_AGE_DAYS = 30
ENRICHMENT_REQUEST_DELAY_SECONDS = 0.15

MIN_RETAINED_STATION_RATIO = float(
    os.environ.get("MIN_RETAINED_STATION_RATIO", "0.75")
)

MIN_RETAINED_FUEL_RATIO = float(
    os.environ.get(
        "MIN_RETAINED_FUEL_RATIO",
        str(MIN_RETAINED_STATION_RATIO),
    )
)
def _fuel_retention_ratio(fuel_key):
    # Allow a fuel-specific retention threshold without weakening the whole Portugal dataset retention check.
    env_key = f"MIN_RETAINED_FUEL_{fuel_key.upper()}_RATIO"
    return float(os.environ.get(env_key, str(MIN_RETAINED_FUEL_RATIO)))

FUEL_TYPES = {
    3201: "gasoline95",
    3205: "gasoline95Plus",
    3400: "gasoline98",
    3405: "gasoline98Plus",
    3210: "gasolineMix",
    2101: "diesel",
    2105: "dieselPremium",
    2155: "dieselHeating",
    2150: "dieselAgri",
    2115: "bioDiesel",
    1120: "lpg",
    1141: "cngm3",
    1143: "cngkg",
    1142: "lng",
}

PAGE_SIZE = 10000


def fetch_fuel(session, fuel_id):
    url = f"{BASE_URL}?idsTiposComb={fuel_id}&qtdPorPagina={PAGE_SIZE}"
    payload = fetch_json(session, url)
    if not isinstance(payload, dict):
        raise RuntimeError(f"Portugal: fuel {fuel_id} returned non-object JSON.")
    if not payload.get("status"):
        raise RuntimeError(
            f"Portugal: DGEG fuel {fuel_id} returned status=false: "
            f"{payload.get('mensagem') or 'no message'}"
        )
    result = payload.get("resultado")
    if not isinstance(result, list):
        raise RuntimeError(f"Portugal: DGEG fuel {fuel_id} returned malformed resultado.")
    print(fuel_id, payload.get("status"), len(result), payload.get("mensagem"))
    return result


def _clean(value):
    if value in (None, "", "-"):
        return None
    return value


def _extract_descriptions(raw_list):
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
    url = f"{MAP_URL}?id={sid}"
    payload = fetch_json(session, url)
    if not isinstance(payload, dict) or not payload.get("status"):
        message = payload.get("mensagem") if isinstance(payload, dict) else None
        raise ValueError(message or "DGEG returned invalid/status=false enrichment response")
    result = payload.get("resultado") or {}
    if not isinstance(result, dict):
        raise ValueError("DGEG returned malformed enrichment resultado")

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
    if not isinstance(cached_entry, dict):
        return True
    fetched_at = cached_entry.get("fetchedAt")
    if not fetched_at:
        return True
    try:
        fetched = datetime.fromisoformat(fetched_at)
    except ValueError:
        return True
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - fetched > timedelta(days=ENRICHMENT_MAX_AGE_DAYS)


def _has_stale_enrichment(stations, enrichment_cache):
    return any(_is_stale(enrichment_cache.get(sid)) for sid in stations)


def enrich_stations(session, stations, enrichment_cache):
    fetched = 0
    for i, (sid, station) in enumerate(stations.items(), 1):
        if i % 100 == 0:
            print(f"Enrichment: {i}/{len(stations)}")

        cached = enrichment_cache.get(sid)
        if _is_stale(cached):
            try:
                enrichment = fetch_station_enrichment(session, sid)
                enrichment["fetchedAt"] = datetime.now(timezone.utc).isoformat()
                enrichment_cache[sid] = enrichment
                fetched += 1
                time.sleep(ENRICHMENT_REQUEST_DELAY_SECONDS)
            except Exception as exc:  # noqa: BLE001 - one bad station must not kill the run
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
        print(
            f"Portugal: fetched fresh enrichment for {fetched} station(s) "
            f"(new or older than {ENRICHMENT_MAX_AGE_DAYS} days)."
        )
    return fetched


def _previous_station_count(manifest):
    if not isinstance(manifest, dict):
        return 0
    count = manifest.get("stationCount")
    if isinstance(count, int) and count >= 0:
        return count
    return sum(
        entry.get("stationCount", 0)
        for entry in (manifest.get("districts") or {}).values()
        if isinstance(entry, dict)
    )

def _validate_retention(
    new_count,
    previous_count,
    label,
    min_ratio=MIN_RETAINED_STATION_RATIO,
):
    if new_count <= 0:
        raise RuntimeError(
            f"Portugal: refusing to publish an empty {label} dataset."
        )

    if previous_count <= 0:
        return

    ratio = new_count / previous_count

    if ratio < min_ratio:
        raise RuntimeError(
            "Portugal: refusing suspicious source contraction: "
            f"{label} count {previous_count} -> {new_count} ({ratio:.1%}); "
            f"minimum retained ratio is {min_ratio:.0%}."
        )

def _previous_fuel_count(state, fuel_key):
    if not isinstance(state, dict):
        return 0
    return sum(
        1
        for fuels in state.values()
        if isinstance(fuels, dict) and fuel_key in fuels
    )


def _state_ids_changed(previous_state, next_state):
    ids = set(previous_state) | set(next_state)
    return {sid for sid in ids if previous_state.get(sid) != next_state.get(sid)}


def run():
    if not 0 < MIN_RETAINED_STATION_RATIO <= 1:
        raise RuntimeError(
            "Portugal: MIN_RETAINED_STATION_RATIO must be > 0 and <= 1."
        )

    if not 0 < MIN_RETAINED_FUEL_RATIO <= 1:
        raise RuntimeError(
            "Portugal: MIN_RETAINED_FUEL_RATIO must be > 0 and <= 1."
        )

    for fuel_key in FUEL_TYPES.values():
        fuel_ratio = _fuel_retention_ratio(fuel_key)
        if not 0 < fuel_ratio <= 1:
            raise RuntimeError(
                f"Portugal: retention ratio for {fuel_key} "
                "must be > 0 and <= 1."
            )

    session = make_session()
    state = load_json(STATE_PATH, default={}) or {}
    enrichment_cache = load_json(ENRICHMENT_STATE_PATH, default={}) or {}
    override_state = load_json(OVERRIDES_STATE_PATH, default={}) or {}
    previous_manifest = load_json(MANIFEST_PATH, default={}) or {}
    previous_count = _previous_station_count(previous_manifest)

    stations = {}
    next_state = {}
    source_row_count = 0

    for fuel_id, fuel_key in FUEL_TYPES.items():
        rows = fetch_fuel(session, fuel_id)
        previous_fuel_count = _previous_fuel_count(state, fuel_key)
        
        if previous_fuel_count > 0:
            fuel_min_ratio = _fuel_retention_ratio(fuel_key)
        
            _validate_retention(
                len(rows),
                previous_fuel_count,
                f"{fuel_key} source row",
                fuel_min_ratio,
            )
        source_row_count += len(rows)
        for row in rows:
            if not isinstance(row, dict) or row.get("Id") in (None, ""):
                continue
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
                    "extra": {"stationType": row.get("TipoPosto")},
                },
            )

            price = parse_pt_price(row.get("Preco"))
            if price is not None:
                station["fuels"][fuel_key] = price
            if updated and updated > (station["lastUpdated"] or ""):
                station["lastUpdated"] = updated

            next_state.setdefault(sid, {})[fuel_key] = updated

    changed_ids = _state_ids_changed(state, next_state)
    source_state_changed = bool(changed_ids)
    state = next_state

    _validate_retention(len(stations), previous_count, "received station")

    overrides = load_json(OVERRIDES_PATH, default={}) or {}
    overrides_hash = content_hash(overrides)
    overrides_changed = override_state.get("hash") != overrides_hash
    enrichment_stale = _has_stale_enrichment(stations, enrichment_cache)

    if not source_state_changed and not overrides_changed and not enrichment_stale:
        print("Portugal: no station, override, or enrichment updates found; skipping write.")
        return False

    if changed_ids:
        print(f"Portugal: {len(changed_ids)} station(s) changed.")
    if overrides_changed:
        print("Portugal: overrides changed, reprocessing.")
    if enrichment_stale:
        print("Portugal: enrichment TTL reached for at least one station; refreshing.")

    enrich_stations(session, stations, enrichment_cache)

    by_district = {}
    stats = {"swapped": 0, "dropped": [], "rescued": []}
    for sid, station in stations.items():
        resolved = resolve_station_coordinates(
            "PT", station["lat"], station["lng"], overrides, station["id"]
        )
        if resolved is None:
            stats["dropped"].append(station["id"])
            continue
        new_lat, new_lng, from_override = resolved
        if from_override:
            stats["rescued"].append(station["id"])
        elif (new_lat, new_lng) != (station["lat"], station["lng"]):
            stats["swapped"] += 1
        station["lat"], station["lng"] = new_lat, new_lng
        feature = {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [station["lng"], station["lat"]]},
            "properties": {k: v for k, v in station.items() if k not in ("lat", "lng")},
        }
        district = (station["district"] or "unknown").strip().lower().replace(" ", "_")
        by_district.setdefault(district, []).append(feature)

    published_count = sum(len(features) for features in by_district.values())
    _validate_retention(published_count, previous_count, "published station")

    changed_files = 0
    district_entries = {}
    data_updated_through = None
    for district, features in by_district.items():
        features.sort(key=lambda f: f["properties"]["id"])
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

    manifest_core = {
        "schemaVersion": 1,
        "sourceStatus": "ok",
        "dataUpdatedThrough": data_updated_through,
        "stationCount": published_count,
        "receivedStationCount": len(stations),
        "sourceRowCount": source_row_count,
        "droppedStationCount": len(stats["dropped"]),
        "districts": district_entries,
    }
    previous_core = {k: v for k, v in previous_manifest.items() if k != "generatedAt"}
    if content_hash(previous_core) == content_hash(manifest_core):
        generated_at = previous_manifest.get("generatedAt") or datetime.now(timezone.utc).isoformat()
    else:
        generated_at = datetime.now(timezone.utc).isoformat()
    manifest = {"generatedAt": generated_at, **manifest_core}
    manifest_changed = write_json_if_changed(MANIFEST_PATH, manifest)

    # Once the replacement manifest is valid/on disk, remove no-longer-referenced files.
    removed_districts = []
    for path in glob.glob(os.path.join(DATA_DIR, "district_*.geojson")):
        stem = os.path.splitext(os.path.basename(path))[0]
        key = stem.removeprefix("district_")
        if key not in by_district:
            os.remove(path)
            removed_districts.append(key)

    state_changed = write_json_if_changed(STATE_PATH, state)
    enrichment_changed = write_json_if_changed(ENRICHMENT_STATE_PATH, enrichment_cache)
    override_state_changed = write_json_if_changed(OVERRIDES_STATE_PATH, {"hash": overrides_hash})

    print(f"Portugal: {changed_files}/{len(by_district)} district file(s) actually changed.")
    if removed_districts:
        print(
            f"Portugal: removed {len(removed_districts)} stale district file(s): "
            f"{', '.join(removed_districts)}."
        )
    if stats["rescued"]:
        print(f"Portugal: re-placed {len(stats['rescued'])} station(s) via override coordinates.")
    if stats["swapped"] or stats["dropped"]:
        print(f"Portugal: swapped {stats['swapped']} station(s).")
        print(f"Portugal: dropped {len(stats['dropped'])} station(s): {', '.join(stats['dropped'])}.")
        if stats["dropped"]:
            print(
                'Portugal: dropped stations need an override with "coordinates" [lng, lat] '
                "(or a source fix) to be re-published."
            )

    return bool(
        changed_files
        or removed_districts
        or manifest_changed
        or state_changed
        or enrichment_changed
        or override_state_changed
    )


if __name__ == "__main__":
    run()
