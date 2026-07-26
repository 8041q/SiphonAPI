# Fetches Portugal's DGEG fuel price feed.

# It filters by fuel type via `idsTiposComb`, and each station has its own `DataAtualizacao`.
# So the delta logic here is per-station: we keep the last-seen `DataAtualizacao` for every
# (station, fuel) pair in state/pt_stations.json, and only write output files
# when at least one of those actually changed.

# Output: one GeoJSON file per district under data/pt/, plus a manifest.json.


import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from common import (  # noqa: E402
    fetch_json,
    load_json,
    make_session,
    parse_pt_price,
    write_json_if_changed,
)

BASE_URL = "https://precoscombustiveis.dgeg.gov.pt/api/PrecoComb/PesquisarPostos"
STATE_PATH = "state/pt_stations.json"
DATA_DIR = "data/pt"

FUEL_TYPES = {
    # Gasoline
    3201: "gasoline95",      # Gasolina simples 95
    3205: "gasoline95Plus",  # Gasolina especial 95
    3401: "gasoline98",      # Gasolina simples 98
    3405: "gasoline98Plus",  # Gasolina especial 98
    3202: "gasolineMix",     # Gasolina mistura (2-stroke)

    # Diesel
    2101: "diesel",          # Gasóleo simples
    2105: "dieselPremium",   # Gasóleo especial
    2102: "dieselHeating",   # Gasóleo de aquecimento
    2103: "dieselAgri",      # Gasóleo colorido e marcado (agrícola)

    # Gas & Alternative
    1101: "lpg",             # GPL Auto
    1201: "cng",             # GNC (Gás Natural Comprimido)
    1301: "lng",             # GNL (Gás Natural Liquefeito)
}

PAGE_SIZE = 10000  # above Portugal's total stations


def fetch_fuel(session, fuel_id):
    url = f"{BASE_URL}?idsTiposComb={fuel_id}&qtdPorPagina={PAGE_SIZE}"
    payload = fetch_json(session, url)
    return payload.get("resultado", [])


def run():
    session = make_session()
    state = load_json(STATE_PATH, default={})  # {fuel_key: DataAtualizacao}

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

    if not changed_ids:
        print("Portugal: no station updates found, skipping write.")
        return False

    print(f"Portugal: {len(changed_ids)} station(s) changed.")

    by_district = {}
    for sid, station in stations.items():
        if station["lat"] is None or station["lng"] is None:
            continue
        feature = {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [station["lng"], station["lat"]]},
            "properties": {k: v for k, v in station.items() if k not in ("lat", "lng")},
        }
        district = (station["district"] or "unknown").strip().lower().replace(" ", "_")
        by_district.setdefault(district, []).append(feature)

    changed_files = 0
    for district, features in by_district.items():
        features.sort(key=lambda f: f["properties"]["id"])  # deterministic diffs
        geojson = {"type": "FeatureCollection", "features": features}
        path = os.path.join(DATA_DIR, f"district_{district}.geojson")
        if write_json_if_changed(path, geojson):
            changed_files += 1

    manifest = {
        "districts": sorted(by_district.keys()),
        "stationCount": len(stations),
    }
    write_json_if_changed(os.path.join(DATA_DIR, "manifest.json"), manifest)
    write_json_if_changed(STATE_PATH, state)

    print(f"Portugal: {changed_files}/{len(by_district)} district file(s) actually changed.")
    return True


if __name__ == "__main__":
    run()
