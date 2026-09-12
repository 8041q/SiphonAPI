# Builds the top-level manifest.json at the repo root.
# Existing public paths/keys are preserved; added metadata is optional for older
# clients and helps newer clients reason about source health/counts.

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
    existing = load_json(MANIFEST_PATH, default={}) or {}

    countries = {}
    for code, path in COUNTRY_MANIFESTS.items():
        country_manifest = load_json(path)
        if country_manifest is None:
            continue
        entry = {
            "manifest": path.replace(os.sep, "/"),
            "hash": content_hash(country_manifest),
            "lastUpdated": country_manifest.get("lastUpdated")
            or country_manifest.get("dataUpdatedThrough"),
        }
        # Additive fields: older clients can ignore them.
        if country_manifest.get("sourceStatus") is not None:
            entry["status"] = country_manifest.get("sourceStatus")
        if country_manifest.get("stationCount") is not None:
            entry["stationCount"] = country_manifest.get("stationCount")
        countries[code] = entry

    history_index = load_json(HISTORY_INDEX_PATH)
    history = None
    if history_index is not None:
        history = {
            "path": HISTORY_INDEX_PATH.replace(os.sep, "/"),
            "hash": content_hash(history_index),
            "lastUpdated": history_index.get("lastUpdated"),
            "snapshotCount": len(history_index.get("days", [])),
        }

    dashboard = load_json(COMMODITIES_DASHBOARD_PATH)
    commodities = None
    if dashboard is not None:
        commodities = {
            "path": COMMODITIES_DASHBOARD_PATH.replace(os.sep, "/"),
            "hash": content_hash(dashboard),
            "lastUpdated": dashboard.get("lastUpdated"),
            "status": dashboard.get("status"),
        }

    stable_manifest = {
        # Keep the existing public version for client compatibility. The new
        # fields are backward-compatible additions, not a contract break.
        "version": 2,
        "schemaVersion": 2,
        "countries": countries,
    }
    if history is not None:
        stable_manifest["history"] = history
    if commodities is not None:
        stable_manifest["commodities"] = commodities

    existing_stable = {k: v for k, v in existing.items() if k != "generatedAt"}
    if content_hash(existing_stable) == content_hash(stable_manifest):
        print("Root manifest: nothing changed, skipping.")
        return False

    manifest = {
        **stable_manifest,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
    }
    write_json_if_changed(MANIFEST_PATH, manifest)
    print("Root manifest: updated ->", ", ".join(sorted(countries)))
    return True


if __name__ == "__main__":
    run()
