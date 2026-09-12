# Fetches Spain's national fuel price feed (MINETUR) and only re-processes it
# when the government's own `Fecha` timestamp or local overrides change.
# Output: one GeoJSON file per 1x1 degree grid tile under data/es/, plus a
# manifest.json listing tile hashes/bboxes/counts.

import glob
import math
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from common import (  # noqa: E402
    apply_overrides,
    bbox_from_features,
    content_hash,
    fetch_json,
    load_json,
    make_session,
    parse_es_number,
    resolve_station_coordinates,
    write_json_if_changed,
)

SOURCE_URL = os.environ.get("SPAIN_SOURCE_URL")
STATE_PATH = "state/es_last_fetch.json"
DATA_DIR = "data/es"
OVERRIDES_PATH = "data/overrides/es.json"
MANIFEST_PATH = os.path.join(DATA_DIR, "manifest.json")

# A valid national feed should not abruptly lose a large fraction of stations.
# Abort before deletion/publication if it does, preserving the last known-good
# dataset. Override only for a known legitimate nationwide source change.
MIN_RETAINED_STATION_RATIO = float(os.environ.get("MIN_RETAINED_STATION_RATIO", "0.75"))

FUEL_FIELDS = {
    # Traditional Gasoline
    "Precio Gasolina 95 E5": "gasoline95",
    "Precio Gasolina 95 E10": "gasoline95E10",
    "Precio Gasolina 95 E25": "gasoline95E25",
    "Precio Gasolina 95 E5 Premium": "gasoline95Premium",
    "Precio Gasolina 95 E85": "gasoline95E85",
    "Precio Gasolina 98 E5": "gasoline98",
    "Precio Gasolina 98 E10": "gasoline98E10",
    "Precio Gasolina Renovable": "gasolineRenewable",
    # Diesel
    "Precio Gasoleo A": "diesel",
    "Precio Gasoleo Premium": "dieselPremium",
    "Precio Gasoleo B": "dieselB",
    "Precio Diésel Renovable": "dieselRenewable",
    # Biofuels & Alternative Gases
    "Precio Bioetanol": "bioethanol",
    "Precio Biodiesel": "biodiesel",
    "Precio Gases licuados del petróleo": "lpg",
    "Precio Gas Natural Comprimido": "cng",
    "Precio Gas Natural Licuado": "lng",
    "Precio Biogas Natural Comprimido": "bioCng",
    "Precio Biogas Natural Licuado": "bioLng",
    "Precio Hidrogeno": "hydrogen",
    "Precio Amoniaco": "ammonia",
    "Precio Metanol": "methanol",
    "Precio Adblue": "adblue",
}

GRID_SIZE_DEGREES = 1


def grid_key(lat, lng):
    return f"grid_{math.floor(lat / GRID_SIZE_DEGREES)}_{math.floor(lng / GRID_SIZE_DEGREES)}"


def _previous_station_count(manifest):
    if not isinstance(manifest, dict):
        return 0
    count = manifest.get("stationCount")
    if isinstance(count, int) and count >= 0:
        return count
    return sum(
        entry.get("stationCount", 0)
        for entry in (manifest.get("tiles") or {}).values()
        if isinstance(entry, dict)
    )


def _validate_retention(new_count, previous_count, label):
    if new_count <= 0:
        raise RuntimeError(f"Spain: refusing to publish an empty {label} dataset.")
    if previous_count <= 0:
        return
    ratio = new_count / previous_count
    if ratio < MIN_RETAINED_STATION_RATIO:
        raise RuntimeError(
            "Spain: refusing suspicious source contraction: "
            f"{label} count {previous_count} -> {new_count} ({ratio:.1%}); "
            f"minimum retained ratio is {MIN_RETAINED_STATION_RATIO:.0%}."
        )


def station_to_feature(raw, stats, overrides):
    sid = raw.get("IDEESS")
    if sid in (None, ""):
        stats["dropped"].append("es-unknown")
        return None

    station_id = f"es-{sid}"
    lat = parse_es_number(raw.get("Latitud"))
    lng = parse_es_number(raw.get("Longitud (WGS84)"))
    resolved = resolve_station_coordinates("ES", lat, lng, overrides, station_id)
    if resolved is None:
        stats["dropped"].append(station_id)
        return None
    new_lat, new_lng, from_override = resolved
    if from_override:
        stats["rescued"].append(station_id)
    elif (new_lat, new_lng) != (lat, lng):
        stats["swapped"] += 1
    lat, lng = new_lat, new_lng

    fuels = {}
    for raw_key, clean_key in FUEL_FIELDS.items():
        price = parse_es_number(raw.get(raw_key))
        if price is not None:
            fuels[clean_key] = price

    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lng, lat]},
        "properties": {
            "id": station_id,
            "source": "ES",
            "brand": (raw.get("Rótulo") or "").strip(),
            "address": raw.get("Dirección"),
            "municipality": raw.get("Municipio"),
            "province": raw.get("Provincia"),
            "postalCode": raw.get("C.P."),
            "schedule": raw.get("Horario"),
            "fuels": fuels,
            "extra": {
                "saleType": raw.get("Tipo Venta"),
                "margin": raw.get("Margen"),
                "reportingType": raw.get("Remisión"),
                "ideess": raw.get("IDEESS"),
                "idMunicipio": raw.get("IDMunicipio"),
                "idProvincia": raw.get("IDProvincia"),
                "idCCAA": raw.get("IDCCAA"),
            },
        },
    }


def run():
    if not SOURCE_URL:
        raise RuntimeError("Spain: SPAIN_SOURCE_URL is not configured.")
    if not 0 < MIN_RETAINED_STATION_RATIO <= 1:
        raise RuntimeError("Spain: MIN_RETAINED_STATION_RATIO must be > 0 and <= 1.")

    session = make_session()
    request_headers = None
    proxy_key = os.environ.get("SPAIN_PROXY_KEY")
    if proxy_key:
        # Request-scoped on purpose: never leak this secret to Portugal/FRED.
        request_headers = {"X-API-Key": proxy_key}

    payload = fetch_json(session, SOURCE_URL, headers=request_headers)
    if not isinstance(payload, dict):
        raise RuntimeError("Spain: source returned a non-object JSON payload.")
    current_fecha = payload.get("Fecha")
    rows = payload.get("ListaEESSPrecio")
    if not current_fecha:
        raise RuntimeError("Spain: source payload is missing Fecha.")
    if not isinstance(rows, list):
        raise RuntimeError("Spain: source payload is missing ListaEESSPrecio list.")
    if not rows:
        raise RuntimeError("Spain: source returned zero station rows; preserving published data.")

    state = load_json(STATE_PATH, default={}) or {}
    overrides = load_json(OVERRIDES_PATH, default={}) or {}
    overrides_hash = content_hash(overrides)
    overrides_changed = state.get("overridesHash") != overrides_hash
    if state.get("fecha") == current_fecha and not overrides_changed:
        print(f"Spain: no update (Fecha still {current_fecha}), skipping.")
        return False

    print(
        "Spain: overrides changed, reprocessing..."
        if overrides_changed and state.get("fecha") == current_fecha
        else f"Spain: new data (Fecha {current_fecha}), processing..."
    )

    previous_manifest = load_json(MANIFEST_PATH, default={}) or {}
    previous_count = _previous_station_count(previous_manifest)

    tiles = {}
    stats = {"swapped": 0, "dropped": [], "rescued": []}
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        feature = station_to_feature(raw, stats, overrides)
        if feature is None:
            continue
        lng, lat = feature["geometry"]["coordinates"]
        tiles.setdefault(grid_key(lat, lng), []).append(feature)

    published_count = sum(len(features) for features in tiles.values())
    _validate_retention(published_count, previous_count, "published station")

    changed_tiles = 0
    tile_entries = {}
    for key, features in tiles.items():
        features.sort(key=lambda f: f["properties"]["id"])
        features = apply_overrides(features, OVERRIDES_PATH, country="ES")
        geojson = {"type": "FeatureCollection", "features": features}
        path = os.path.join(DATA_DIR, f"{key}.geojson")
        if write_json_if_changed(path, geojson):
            changed_tiles += 1
        tile_entries[key] = {
            "path": path.replace(os.sep, "/"),
            "stationCount": len(features),
            "bbox": bbox_from_features(features),
            "hash": content_hash(geojson),
        }

    manifest = {
        "schemaVersion": 1,
        "sourceStatus": "ok",
        "lastUpdated": current_fecha,
        "stationCount": published_count,
        "sourceRowCount": len(rows),
        "droppedStationCount": len(stats["dropped"]),
        "tileCount": len(tiles),
        "tiles": tile_entries,
    }
    manifest_changed = write_json_if_changed(MANIFEST_PATH, manifest)

    # Delete only after a fully validated replacement manifest is on disk.
    removed_tiles = []
    for path in glob.glob(os.path.join(DATA_DIR, "grid_*.geojson")):
        key = os.path.splitext(os.path.basename(path))[0]
        if key not in tiles:
            os.remove(path)
            removed_tiles.append(key)

    state_changed = write_json_if_changed(
        STATE_PATH, {"fecha": current_fecha, "overridesHash": overrides_hash}
    )

    print(f"Spain: {changed_tiles}/{len(tiles)} tile file(s) actually changed.")
    if removed_tiles:
        print(f"Spain: removed {len(removed_tiles)} stale tile(s): {', '.join(removed_tiles)}.")
    if stats["rescued"]:
        print(f"Spain: re-placed {len(stats['rescued'])} station(s) via override coordinates.")
    if stats["swapped"] or stats["dropped"]:
        print(f"Spain: swapped {stats['swapped']} station(s).")
        print(f"Spain: dropped {len(stats['dropped'])} station(s): {', '.join(stats['dropped'])}.")
        if stats["dropped"]:
            print(
                'Spain: dropped stations need an override with "coordinates" [lng, lat] '
                "(or a source fix) to be re-published."
            )
    return bool(changed_tiles or removed_tiles or manifest_changed or state_changed)


if __name__ == "__main__":
    run()
