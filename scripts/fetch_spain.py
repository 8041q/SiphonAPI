# Fetches Spain's national fuel price feed (MINETUR) and only re-processes it when the government's
# own `Fecha` timestamp has actually changed. Spain publishes one dataset for the *entire country*, refreshed
# roughly once a day
# no re-parsing, no file writes, no git changes, no wasted Action

# Output: one GeoJSON file per 1x1 degree grid tile under data/es/, plus a manifest.json listing the tiles
# Grid partitioning exists purely so a client only has to download the tiles near it, instead of a country on every launch

# manifest.json also carries a content hash + bbox + station count per tile

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
    validate_station_coords,
    write_json_if_changed,
)

# Original API source. Now I use a proxy in cloudfare to bypass Spain block
# "https://sedeaplicaciones.minetur.gob.es/ServiciosRESTCarburantes/"
# "PreciosCarburantes/EstacionesTerrestres/"
SOURCE_URL = os.environ.get("SPAIN_SOURCE_URL")
STATE_PATH = "state/es_last_fetch.json"
DATA_DIR = "data/es"
OVERRIDES_PATH = "data/overrides/es.json"

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
    "Precio Gasoleo B": "dieselB",  # Agricultural / heating
    "Precio Diésel Renovable": "dieselRenewable",

    # Biofuels & Alternative Gases
    "Precio Bioetanol": "bioethanol",
    "Precio Biodiesel": "biodiesel",
    "Precio Gases licuados del petróleo": "lpg",  # GLP
    "Precio Gas Natural Comprimido": "cng",      # GNC
    "Precio Gas Natural Licuado": "lng",         # GNL
    "Precio Biogas Natural Comprimido": "bioCng",
    "Precio Biogas Natural Licuado": "bioLng",
    "Precio Hidrogeno": "hydrogen",
    "Precio Amoniaco": "ammonia",
    "Precio Metanol": "methanol",
    "Precio Adblue": "adblue",
}

GRID_SIZE_DEGREES = 1  # smaller = more, smaller tile files


def grid_key(lat, lng):
    return f"grid_{math.floor(lat / GRID_SIZE_DEGREES)}_{math.floor(lng / GRID_SIZE_DEGREES)}"


def station_to_feature(raw, stats):
    lat = parse_es_number(raw.get("Latitud"))
    lng = parse_es_number(raw.get("Longitud (WGS84)"))
    if lat is None or lng is None:
        return None

    validated = validate_station_coords("ES", lat, lng)
    if validated is None:
        stats["dropped"] += 1
        return None
    if validated != (lat, lng):
        stats["swapped"] += 1
        lat, lng = validated

    fuels = {}
    for raw_key, clean_key in FUEL_FIELDS.items():
        price = parse_es_number(raw.get(raw_key))
        if price is not None:
            fuels[clean_key] = price

    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lng, lat]},
        "properties": {
            "id": f"es-{raw.get('IDEESS')}",
            "source": "ES",
            "brand": (raw.get("Rótulo") or "").strip(),
            "address": raw.get("Dirección"),
            "municipality": raw.get("Municipio"),
            "province": raw.get("Provincia"),
            "postalCode": raw.get("C.P."),
            "schedule": raw.get("Horario"),
            "fuels": fuels,
            "extra": {
                "saleType": raw.get("Tipo Venta"),  # e.g. "P" = público (retail)
                "margin": raw.get("Margen"),  # roadside position: D/I/N
                # "Remisión" is a reporting-frequency code (values seen: "dm",
                # "OM") from MINETUR's own submission rules
                # Kept as-is rather than guessing
                "reportingType": raw.get("Remisión"),
                "ideess": raw.get("IDEESS"),
                "idMunicipio": raw.get("IDMunicipio"),
                "idProvincia": raw.get("IDProvincia"),
                "idCCAA": raw.get("IDCCAA"),
            },
        },
    }


def run():
    session = make_session()
    payload = fetch_json(session, SOURCE_URL)
    current_fecha = payload.get("Fecha")

    state = load_json(STATE_PATH, default={})
    if state.get("fecha") == current_fecha:
        print(f"Spain: no update (Fecha still {current_fecha}), skipping.")
        return False

    print(f"Spain: new data (Fecha {current_fecha}), processing...")

    tiles = {}
    stats = {"swapped": 0, "dropped": 0}
    for raw in payload.get("ListaEESSPrecio", []):
        feature = station_to_feature(raw, stats)
        if feature is None:
            continue
        lng, lat = feature["geometry"]["coordinates"]
        tiles.setdefault(grid_key(lat, lng), []).append(feature)

    changed_tiles = 0
    tile_entries = {}
    for key, features in tiles.items():
        features.sort(key=lambda f: f["properties"]["id"])  # deterministic diffs
        features = apply_overrides(features, OVERRIDES_PATH)
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
        "lastUpdated": current_fecha,
        "tileCount": len(tiles),
        # looks the entry up directly and compares "hash" to what it cached
        # last time instead of downloading every tile to check
        "tiles": tile_entries,
    }

    write_json_if_changed(os.path.join(DATA_DIR, "manifest.json"), manifest)
    write_json_if_changed(STATE_PATH, {"fecha": current_fecha})

    print(f"Spain: {changed_tiles}/{len(tiles)} tile file(s) actually changed.")
    if stats["swapped"] or stats["dropped"]:
        print(f"Spain: swapped {stats['swapped']}, dropped {stats['dropped']} station(s).")
    return True


if __name__ == "__main__":
    run()