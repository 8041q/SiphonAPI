# Fetches daily Brent and WTI spot prices from the FRED API and writes
# data/commodities/crude.json. Each series owns its own recovery cursor and
# re-fetches a small overlap so transient failures or FRED revisions are healed.

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
REFETCH_OVERLAP_DAYS = 14
CRUDE_PATH = "data/commodities/crude.json"


def _fetch_one(session, series_id: str, start_date: str, api_key: str):
    """Return [{date, value}] for one FRED series, or None on request failure."""
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
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 - preserve cached series on any upstream failure
        print(f"fetch_crude: FRED request failed for {series_id}: {exc}")
        return None

    observations = data.get("observations")
    if not isinstance(observations, list):
        print(f"fetch_crude: malformed FRED response for {series_id}; preserving cache.")
        return None

    points = []
    for obs in observations:
        try:
            value = float(obs["value"])
            date = obs["date"]
            dtdate.fromisoformat(date)
        except (KeyError, ValueError, TypeError):
            continue
        points.append({"date": date, "value": value})
    return points


def _determine_start(existing_points):
    """Return this series' recovery start date with an overlap for revisions."""
    dates = []
    for point in existing_points:
        try:
            dates.append(dtdate.fromisoformat(point["date"]))
        except (KeyError, TypeError, ValueError):
            continue
    if not dates:
        return BACKFILL_START
    start = max(dates) - timedelta(days=REFETCH_OVERLAP_DAYS)
    floor = dtdate.fromisoformat(BACKFILL_START)
    return max(start, floor).isoformat()


def _merge_points(existing_points, fresh_points):
    """Upsert fetched points by date and report whether the series changed."""
    before = {p["date"]: p["value"] for p in existing_points if "date" in p and "value" in p}
    merged = dict(before)
    for point in fresh_points:
        merged[point["date"]] = point["value"]
    changed = merged != before
    return (
        [{"date": date, "value": merged[date]} for date in sorted(merged)],
        changed,
    )


def run():
    api_key = os.environ.get("FRED_API_KEY", "")
    if not api_key:
        print("fetch_crude: FRED_API_KEY not set — preserving existing data.")
        return False

    existing = load_json(
        CRUDE_PATH,
        default={"source": "FRED", "unit": "USD/barrel", "series": {}},
    ) or {"source": "FRED", "unit": "USD/barrel", "series": {}}
    existing_series = existing.get("series", {})

    session = make_session()
    merged = {}
    any_changed = False
    fetched_any = False

    for name, series_id in SERIES_IDS.items():
        cached = existing_series.get(name, [])
        start = _determine_start(cached)
        print(f"fetch_crude: {name} starting from {start}")
        fresh = _fetch_one(session, series_id, start, api_key)
        if fresh is None:
            merged[name] = cached
            continue

        fetched_any = True
        merged[name], changed = _merge_points(cached, fresh)
        any_changed = any_changed or changed

    # Preserve unknown/future series keys rather than silently dropping them.
    for name, points in existing_series.items():
        merged.setdefault(name, points)

    if not fetched_any:
        print("fetch_crude: all FRED requests failed — preserving existing data.")
        return False
    if not any_changed:
        print("fetch_crude: no new or revised observations — skipping write.")
        return False

    crude = {
        "source": existing.get("source", "FRED"),
        "unit": existing.get("unit", "USD/barrel"),
        "lastUpdated": datetime.now(timezone.utc).isoformat(),
        "series": merged,
    }
    wrote = write_json_if_changed(CRUDE_PATH, crude)
    print(f"fetch_crude: wrote={'yes' if wrote else 'no'}.")
    return wrote


if __name__ == "__main__":
    run()
