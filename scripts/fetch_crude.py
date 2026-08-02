# Fetches daily Brent and WTI spot prices from the FRED API and writes
# data/commodities/crude.json. Idempotent: on first run it backfills from
# BACKFILL_START; afterwards it fetches only dates newer than the latest
# cached one. Non-numeric observations (weekends/holidays) are skipped.
# Reuses the retry-capable session from common.make_session().

import json
import os
import sys
from datetime import date as dtdate
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))
from common import load_json, make_session, write_json_if_changed  # noqa: E402

FRED_URL = "https://api.stlouisfed.org/fred/series/observations"
SERIES_IDS = {
    "brent": "DCOILBRENTEU",
    "wti": "DCOILWTICO",
}
BACKFILL_START = "2025-01-01"
CRUDE_PATH = "data/commodities/crude.json"


def _fetch_one(session, series_id: str, start_date: str):
    """Return [{date, value}] for *one* FRED series."""
    api_key = os.environ.get("FRED_API_KEY", "")
    if not api_key:
        print("fetch_crude: FRED_API_KEY not set — skipping.")
        return []

    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "observation_start": start_date,
        "sort_order": "asc",
    }
    try:
        resp = session.get(FRED_URL, params=params, timeout=30)
        resp.raise_for_status()
    except Exception as exc:
        print(f"fetch_crude: FRED request failed for {series_id}: {exc}")
        return []

    data = resp.json()
    points = []
    for obs in data.get("observations", []):
        try:
            value = float(obs["value"])
        except (ValueError, TypeError):
            continue
        points.append({"date": obs["date"], "value": value})
    return points


def _determine_start(existing_series):
    """Return the oldest date that still needs fetching."""
    overall = set()
    for pts in existing_series.values():
        for p in pts:
            overall.add(p["date"])
    if not overall:
        return BACKFILL_START
    latest = max(overall)
    return (dtdate.fromisoformat(latest) + timedelta(days=1)).isoformat()


def run():
    existing = load_json(CRUDE_PATH, default={"source": "FRED", "unit": "USD/barrel", "series": {}})
    if existing is None:
        existing = {"source": "FRED", "unit": "USD/barrel", "series": {}}

    existing_series = existing.get("series", {})

    start = _determine_start(existing_series)
    print(f"fetch_crude: starting from {start}")

    session = make_session()
    fresh = {}
    for name, series_id in SERIES_IDS.items():
        fresh[name] = _fetch_one(session, series_id, start)

    merged = {}
    total_new = 0
    for key in SERIES_IDS:
        existing_points = existing_series.get(key, [])
        by_date = {p["date"]: p for p in existing_points}
        added = 0
        for p in fresh.get(key, []):
            if p["date"] not in by_date:
                existing_points.append(p)
                added += 1
        existing_points.sort(key=lambda x: x["date"])
        merged[key] = existing_points
        total_new += added

    if total_new == 0:
        print("fetch_crude: no new observations — skipping write.")
        return

    crude = {
        "source": "FRED",
        "unit": "USD/barrel",
        "lastUpdated": datetime.now(timezone.utc).isoformat(),
        "series": merged,
    }
    wrote = write_json_if_changed(CRUDE_PATH, crude)
    print(f"fetch_crude: {total_new} new observation(s), wrote={'yes' if wrote else 'no'}.")


if __name__ == "__main__":
    run()