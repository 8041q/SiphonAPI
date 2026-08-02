# Builds the top-level manifest.json at the repo root.

# fetch_spain.py and fetch_portugal.py each already maintain their own
# manifest (data/es/manifest.json, data/pt/manifest.json) listing their
# tiles/districts with a hash, bbox and station count apiece. This script
# just combines those two into one tiny root file, so a client only ever
# has to fetch ONE small thing to know whether anything changed anywhere,

# Run this after both fetch scripts

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
from common import content_hash, load_json, write_json_if_changed  # noqa: E402

MANIFEST_PATH = "manifest.json"
HISTORY_INDEX_PATH = "data/history/index.json"
COMMODITIES_DASHBOARD_PATH = "data/commodities/dashboard.json"

COUNTRY_MANIFESTS = {
    "ES": "data/es/manifest.json",
    "PT": "data/pt/manifest.json",
}


def run():
    existing = load_json(MANIFEST_PATH, default={})
    existing_countries = existing.get("countries", {})
    existing_history = existing.get("history")
    existing_commodities = existing.get("commodities")

    countries = {}
    for code, path in COUNTRY_MANIFESTS.items():
        country_manifest = load_json(path)
        if country_manifest is None:
            # First run before that country has ever written anything, just skip it rather than fail; it'll appear once it exists.
            continue
        countries[code] = {
            "manifest": path,
            "hash": content_hash(country_manifest),
            # Spain calls this lastUpdated, Portugal calls it dataUpdatedThrough - normalize to one field so a client doesn't need to know each country's field name
            "lastUpdated": country_manifest.get("lastUpdated")
            or country_manifest.get("dataUpdatedThrough"),
        }

    history_index = load_json(HISTORY_INDEX_PATH)
    history = None
    if history_index is not None:
        history = {
            "path": HISTORY_INDEX_PATH.replace(os.sep, "/"),
            "hash": content_hash(history_index),
            "lastUpdated": history_index.get("lastUpdated"),
        }

    dashboard = load_json(COMMODITIES_DASHBOARD_PATH)
    commodities = None
    if dashboard is not None:
        commodities = {
            "path": COMMODITIES_DASHBOARD_PATH.replace(os.sep, "/"),
            "hash": content_hash(dashboard),
            "lastUpdated": dashboard.get("lastUpdated"),
        }

    if (
        countries == existing_countries
        and history == existing_history
        and commodities == existing_commodities
    ):
        print("Root manifest: nothing changed, skipping.")
        return False

    manifest = {
        "version": 2,
        # Only advances when a country's hash or the history index hash actually
        # changed, not on every run. This stays a meaningful "last real change"
        # signal instead of "last time the workflow happened to run".
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "countries": countries,
    }
    if history is not None:
        manifest["history"] = history
    if commodities is not None:
        manifest["commodities"] = commodities

    write_json_if_changed(MANIFEST_PATH, manifest)
    print("Root manifest: updated ->", ", ".join(sorted(countries)))
    return True


if __name__ == "__main__":
    run()
