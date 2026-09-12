# Builds sparse price-history snapshots and keeps data/history/index.json in sync.
# A new snapshot is written only when station/fuel content differs from the most
# recent snapshot. The index itself is rebuilt from disk so manual deletion of an
# old year is reflected automatically.

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
from common import content_hash, load_json, write_json_if_changed  # noqa: E402

ES_DATA_DIR = "data/es"
PT_DATA_DIR = "data/pt"
HISTORY_DIR = "data/history"
INDEX_PATH = os.path.join(HISTORY_DIR, "index.json")


def stations_from_dir(data_dir):
    # Extract {id, brand, fuels} from every feature in every geojson file in
    # the directory. Portugal falls back from brand to name.
    entries = []
    if not os.path.isdir(data_dir):
        return entries
    for fname in sorted(os.listdir(data_dir)):
        if not fname.endswith(".geojson"):
            continue
        geojson = load_json(os.path.join(data_dir, fname))
        if not geojson:
            continue
        for feature in geojson.get("features", []):
            props = feature.get("properties", {})
            entry = {
                "id": props.get("id"),
                "brand": props.get("brand") or props.get("name"),
                "fuels": props.get("fuels") or {},
            }
            if entry["id"]:
                entries.append(entry)
    return entries


def _latest_snapshot_before(day):
    latest_day = None
    latest_path = None
    if not os.path.isdir(HISTORY_DIR):
        return None
    for year in os.listdir(HISTORY_DIR):
        if not year.isdigit():
            continue
        year_path = os.path.join(HISTORY_DIR, year)
        if not os.path.isdir(year_path):
            continue
        for fname in os.listdir(year_path):
            if not fname.endswith(".json"):
                continue
            try:
                snapshot_day = datetime.fromisoformat(fname[:-5]).date()
            except ValueError:
                continue
            if snapshot_day >= day:
                continue
            if latest_day is None or snapshot_day > latest_day:
                latest_day = snapshot_day
                latest_path = os.path.join(year_path, fname)
    return latest_path


def _index_entries_from_disk():
    entries = []
    if not os.path.isdir(HISTORY_DIR):
        return entries

    for year in sorted(os.listdir(HISTORY_DIR)):
        if not year.isdigit():
            continue
        year_path = os.path.join(HISTORY_DIR, year)
        if not os.path.isdir(year_path):
            continue
        for fname in sorted(os.listdir(year_path)):
            if not fname.endswith(".json"):
                continue
            date = fname[:-5]
            try:
                datetime.fromisoformat(date)
            except ValueError:
                continue
            day_obj = load_json(os.path.join(year_path, fname))
            if day_obj is None:
                continue
            entries.append(
                {
                    "date": date,
                    "path": f"data/history/{year}/{fname}",
                    "hash": content_hash(day_obj),
                }
            )
    entries.sort(key=lambda item: item["date"])
    return entries


def run():
    today = datetime.now(timezone.utc).date()
    year_dir = os.path.join(HISTORY_DIR, str(today.year))
    day_path = os.path.join(year_dir, today.isoformat() + ".json")

    stations = stations_from_dir(ES_DATA_DIR) + stations_from_dir(PT_DATA_DIR)
    if stations:
        stations.sort(key=lambda e: e["id"])
        if os.path.exists(day_path):
            # A second workflow run on the same UTC day should refresh that day's
            # snapshot if upstream prices changed after the first run.
            if write_json_if_changed(day_path, stations):
                print(f"History: refreshed {day_path} ({len(stations)} stations).")
        else:
            previous_path = _latest_snapshot_before(today)
            previous = load_json(previous_path, default=[]) if previous_path else None
            if previous is not None and content_hash(stations) == content_hash(previous):
                print("History: station prices unchanged since the latest snapshot; skipping day file.")
            else:
                write_json_if_changed(day_path, stations)
                print(f"History: wrote {day_path} ({len(stations)} stations).")
    else:
        print("History: no station data on disk, skipping today's file.")

    # Rebuild from disk every run. This both adds new snapshots and removes
    # stale index entries after an old year/folder is deleted manually.
    days = _index_entries_from_disk()
    existing = load_json(INDEX_PATH, default={"lastUpdated": None, "days": []}) or {}
    if existing.get("days", []) == days:
        print("History: index already matches disk.")
        return False

    index = {
        "lastUpdated": datetime.now(timezone.utc).isoformat(),
        "days": days,
    }
    write_json_if_changed(INDEX_PATH, index)
    print(f"History: index now covers {len(days)} snapshot day(s).")
    return True


if __name__ == "__main__":
    run()
