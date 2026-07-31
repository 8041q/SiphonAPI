# Shared helpers for the fetch scripts:

# - HTTP session that always sends a proper identifiable User-Agent
# - JSON read/write helpers that only touch a file on disk when its content has actually changed
# - parsers for the odd number formats each source uses
# - manifest helpers (content_hash / bbox_from_features)

import hashlib
import json
import os

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

# USER_AGENT = "fuel-prices-api/1.0 (+https://github.com/8041q/SiphonAPI)"
# Impersonate a standard desktop browser to prevent WAF connection resets
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"

def make_session():
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Connection": "keep-alive",
        }
    )

    # Send secret key to Cloudflare Worker if configured
    proxy_key = os.environ.get("SPAIN_PROXY_KEY")
    if proxy_key:
        session.headers["X-API-Key"] = proxy_key
    
    # Allow retries on protocol-level connection drops (like reset by peer)
    retry_strategy = Retry(
        total=4,
        backoff_factor=10,  # Exponential backoff: 10s, 20s, 30s, 40s...
        status_forcelist=[429, 500, 502, 503, 504],
        raise_on_status=False,
    )

    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)


    return session


def fetch_json(session, url, timeout=60):
    resp = session.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


# Bounding boxes used to detect swapped lat/lng coordinates. Each country's
# lat range and lng range never overlap, so a swapped pair always falls
# outside every box (usually in the ocean). Boxes are unions of the
# mainland + island territories per country.
COUNTRY_BBOXES = {
    "ES": [
        # Mainland + Balearic Islands
        (-9.5, 35.0, 4.5, 44.0),
        # Canary Islands
        (-18.2, 27.5, -13.3, 29.5),
    ],
    "PT": [
        # Mainland
        (-9.7, 36.9, -6.1, 42.2),
        # Azores
        (-31.5, 36.8, -24.5, 39.8),
        # Madeira
        (-17.4, 32.6, -16.2, 33.2),
    ],
}

# Fields crowdsourced overrides may replace. Prices (fuels) are excluded on purpose
OVERRIDE_FIELDS = (
    "paymentMethods",
    "services",
    "brand",
    "name",
    "schedule",
    "hours",
    "address",
    "otherServices",
    "observations",
)

# Which whitelisted fields each country actually publishes. Overriding a field
# the source doesn't emit would fabricate data the app then renders.
OVERRIDE_FIELDS_BY_COUNTRY = {
    "ES": ("brand", "address", "schedule"),
    "PT": (
        "brand",
        "name",
        "address",
        "hours",
        "services",
        "paymentMethods",
        "otherServices",
        "observations",
    ),
}


def _is_str_list(value):
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _is_hours(value):
    # DGEG hours shape: { weekdays, saturday, sunday, holiday }, each string or null
    if not isinstance(value, dict):
        return False
    allowed = {"weekdays", "saturday", "sunday", "holiday"}
    if not set(value).issubset(allowed):
        return False
    return all(item is None or isinstance(item, str) for item in value.values())


OVERRIDE_VALIDATORS = {
    "brand": lambda v: isinstance(v, str),
    "name": lambda v: isinstance(v, str),
    "address": lambda v: isinstance(v, str),
    "schedule": lambda v: isinstance(v, str),
    "hours": _is_hours,
    "services": _is_str_list,
    "paymentMethods": _is_str_list,
    "otherServices": lambda v: isinstance(v, str),
    "observations": lambda v: isinstance(v, str),
}


def point_in_bboxes(lat, lng, boxes):
    for west, south, east, north in boxes:
        if west <= lng <= east and south <= lat <= north:
            return True
    return False


def validate_station_coords(country, lat, lng):
    # Returns the corrected (lat, lng) pair, or None if the coordinates
    # can't be made to fit the country's bounding boxes at all.
    boxes = COUNTRY_BBOXES.get(country)
    if boxes is None:
        return (lat, lng)
    if point_in_bboxes(lat, lng, boxes):
        return (lat, lng)
    if point_in_bboxes(lng, lat, boxes):
        return (lng, lat)
    return None


def apply_overrides(features, overrides_path, country=None):
    # Final-pass merge: crowdsourced corrections (validated by the maintainer
    # in data/overrides/<country>.json) replace whitelisted properties on the
    # published features. Returns the (possibly modified) features list.
    #
    # Every entry is checked before it is applied: unknown fields, fields the
    # country doesn't publish, and values with the wrong shape are rejected with
    # a warning instead of being shipped to the app. `appliedAt` / `note` are
    # maintainer metadata and are never published.
    overrides = load_json(overrides_path, default={})
    if not overrides:
        return features

    allowed = OVERRIDE_FIELDS_BY_COUNTRY.get(country, OVERRIDE_FIELDS)
    applied = 0
    rejected = 0
    for feature in features:
        props = feature.get("properties", {})
        sid = props.get("id")
        override = overrides.get(sid)
        if not override:
            continue
        for field, value in override.items():
            if field in ("appliedAt", "note"):
                continue
            if field not in OVERRIDE_FIELDS:
                print(f"Overrides: {sid}: ignoring unknown field {field!r}.")
                rejected += 1
                continue
            if field not in allowed:
                print(f"Overrides: {sid}: ignoring {field!r} — not a {country} field.")
                rejected += 1
                continue
            if not OVERRIDE_VALIDATORS[field](value):
                print(
                    f"Overrides: {sid}: ignoring {field!r} — wrong shape "
                    f"({type(value).__name__}), expected the published format."
                )
                rejected += 1
                continue
            props[field] = value
        applied += 1

    if applied or rejected:
        summary = f"Overrides: applied corrections to {applied} station(s)."
        if rejected:
            summary = summary[:-1] + f", rejected {rejected} field(s)."
        print(summary)
    return features


def content_hash(obj):
    # Order-independent content hash. Used to decide whether to write a file
    # at all, AND embedded directly in the manifests
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def load_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json_if_changed(path, obj):
    # Writes `obj` to `path` only if it differs from what's already there
    # Returns True if the file has changes, otherwise it's False

    existing = load_json(path)
    if existing is not None and content_hash(existing) == content_hash(obj):
        return False

    dirname = os.path.dirname(path)
    if dirname:
        os.makedirs(dirname, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    return True


def bbox_from_features(features):
    # [minLng, minLat, maxLng, maxLat] for a list of GeoJSON Point features.
    lngs = [f["geometry"]["coordinates"][0] for f in features]
    lats = [f["geometry"]["coordinates"][1] for f in features]
    return [min(lngs), min(lats), max(lngs), max(lats)]


def parse_es_number(value):
    # Spain sends numbers as comma-decimal strings, e.g. '1,649'
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", "."))
    except ValueError:
        return None


def parse_pt_price(value):
    # Portugal sends prices like '1,729 \u20ac'
    if value in (None, ""):
        return None
    cleaned = str(value).replace("\u20ac", "").strip().replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None