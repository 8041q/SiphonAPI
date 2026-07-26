# Shared helpers for the fetch scripts:

# - HTTP session that always sends a proper identifiable User-Agent
# - JSON read/write helpers that only touch a file on disk when its content has actually changed
# - parsers for the odd number formats each source uses

import hashlib
import json
import os

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

USER_AGENT = "fuel-prices-api/1.0 (+https://github.com/8041q/SiphonAPI)"

def make_session():
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }
    )

    # Config retries for connection drops and temporary server errors (5xx, 429)
    retry_strategy = Retry(
        total=3,
        backoff_factor=30,  # Waits 30s, then 60s, then 90s between retries
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )

    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)


    return session


def fetch_json(session, url, timeout=60):
    resp = session.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _hash(obj):
    # Order-independent content hash, used to detect real changes
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
    if existing is not None and _hash(existing) == _hash(obj):
        return False

    dirname = os.path.dirname(path)
    if dirname:
        os.makedirs(dirname, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    return True


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
