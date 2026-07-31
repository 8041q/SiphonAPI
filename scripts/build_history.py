# Builds the daily price-history dataset and keeps data/history/index.json in
# sync. Runs after both fetch scripts, before build_manifest.py.
#
# Output: one file per date under data/history/{YYYY}/{YYYY-MM-DD}.json,
# plus data/history/index.json listing every day across all years with a
# content hash apiece. A flat array of {id, brand, fuels} per station.
#
# No pruning is ever done here — old years are deleted by hand (remove the
# folder) if desired. The root manifest embeds a hash of the index, so a
# client only re-downloads files when the index actually changes.

import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))
from common import content_hash, load_json, write_json_if_changed  # noqa: E402

ES_DATA_DIR = "data/es"
PT_DATA_DIR = "data/pt"
HISTORY_DIR = "data/history"
INDEX_PATH = os.path.join(HISTORY_DIR, "index.json")


def stations_from_dir(data_dir):
    # Extract {id, brand, fuels} from every feature in every geojson file in
    # the directory. Portugal falls back from brand to name
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
            entries.append(
                {
                    "id": props.get("id"),
                    "brand": props.get("brand") or props.get("name"),
                    "fuels": props.get("fuels") or {},
                }
            )
    return [e for e in entries if e.get("id")]


def run():
    today = datetime.now(timezone.utc).date()
    year_dir = os.path.join(HISTORY_DIR, str(today.year))
    day_path = os.path.join(year_dir, today.isoformat() + ".json")

    index = load_json(INDEX_PATH, default={"lastUpdated": None, "days": []})
    known = {day["date"]: day for day in index.get("days", [])}

    changed = False

    # If it doesn't exist yet
    if not os.path.exists(day_path):
        stations = stations_from_dir(ES_DATA_DIR) + stations_from_dir(PT_DATA_DIR)
        if stations:
            stations.sort(key=lambda e: e["id"])

            # Skip when the content is identical to the previous day's file
            identical_to_previous = False
            for offset in (1, 2, 3):
                prev = today - timedelta(days=offset)
                prev_path = os.path.join(
                    HISTORY_DIR, str(prev.year), prev.isoformat() + ".json"
                )
                if os.path.exists(prev_path):
                    identical_to_previous = (
                        content_hash(stations)
                        == content_hash(load_json(prev_path, default=[]))
                    )
                    break

            if identical_to_previous:
                print("History: prices unchanged since yesterday, skipping day file.")
            else:
                os.makedirs(year_dir, exist_ok=True)
                with open(day_path, "w", encoding="utf-8") as f:
                    json.dump(stations, f, ensure_ascii=False, separators=(",", ":"))
                print(f"History: wrote {day_path} ({len(stations)} stations).")
                changed = True
        else:
            print("History: no station data on disk, skipping today's file.")

    # Backfill any day files on disk missing from the index
    if os.path.isdir(HISTORY_DIR):
        for year in sorted(os.listdir(HISTORY_DIR)):
            if not year.isdigit():
                continue
            year_path = os.path.join(HISTORY_DIR, year)
            for fname in sorted(os.listdir(year_path)):
                if not fname.endswith(".json"):
                    continue
                date = fname[:-5]
                if date in known:
                    continue
                day_obj = load_json(os.path.join(year_path, fname))
                if day_obj is None:
                    continue
                known[date] = {
                    "date": date,
                    "path": f"data/history/{year}/{fname}",
                    "hash": content_hash(day_obj),
                }
                changed = True

    if changed:
        days = [known[date] for date in sorted(known)]
        index = {
            "lastUpdated": datetime.now(timezone.utc).isoformat(),
            "days": days,
        }
        write_json_if_changed(INDEX_PATH, index)
        print(f"History: index now covers {len(days)} day(s).")
        return True

    print("History: nothing to do.")
    return False


if __name__ == "__main__":
    run()
