"""Refresh weekly on the API, preserving the last good reference on outages."""
import argparse
import html
import re
from datetime import date
from urllib.parse import urljoin, urlparse

from common import load_json, make_session, write_json_if_changed
from price_benchmarks import build_benchmarks, workbook_rows

PAGE = "https://energy.ec.europa.eu/data-and-analysis/weekly-oil-bulletin_en"
INFLATION = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/prc_hicp_minr"
CACHE_PATH = "state/price-benchmarks.json"
OUTPUT_PATH = "data/commodities/price-benchmarks.json"


def refresh(force=False, session=None, today=None):
    today = today or date.today()
    cached = load_json(CACHE_PATH, default={}) or {}
    try:
        age = (today - date.fromisoformat(cached.get("checkedAt", ""))).days
    except ValueError:
        age = 999
    if not force and 0 <= age < 7 and cached.get("benchmarks", {}).get("schemaVersion") == 1:
        write_json_if_changed(OUTPUT_PATH, cached["benchmarks"])
        print("Price benchmarks: weekly cache is current.")
        return False
    session = session or make_session()
    try:
        response = session.get(PAGE, timeout=45)
        response.raise_for_status()
        links = [html.unescape(link) for link in re.findall(r'href=["\']([^"\']+)["\']', response.text)]
        def download(marker):
            matches = [urljoin(PAGE, link) for link in links if marker in link]
            if not matches:
                raise ValueError("Oil Bulletin document link unavailable: " + marker)
            url = matches[0]
            if urlparse(url).hostname != "energy.ec.europa.eu":
                raise ValueError("Unexpected document host")
            response = session.get(url, timeout=60)
            response.raise_for_status()
            return workbook_rows(response.content)
        history = download("Prices_History")
        taxes = download("Duties_and_taxes")
        response = session.get(INFLATION, params=[("geo", "PT"), ("geo", "ES"),
            ("unit", "I25"), ("coicop18", "TOT_X_NRG"), ("lang", "EN")], timeout=45)
        response.raise_for_status()
        # A new HTTP check alone must not make old source data appear fresh.
        days = [row.get("A") for row in history.get("Prices wo taxes", [])[3:]]
        from price_benchmarks import excel_day
        source_days = [day for value in days if (day := excel_day(value)) and day <= today.isoformat()]
        if not source_days:
            raise ValueError("Missing source observation dates")
        as_of = max(source_days)
        if (today - date.fromisoformat(as_of)).days > 30:
            raise ValueError("Oil Bulletin source is stale")
        output = build_benchmarks(history, taxes, response.json(), as_of)
        write_json_if_changed(CACHE_PATH, {"checkedAt": today.isoformat(), "benchmarks": output})
        changed = write_json_if_changed(OUTPUT_PATH, output)
        print(f"Price benchmarks: {len(output['bands'])} fuel/country references, through {as_of}.")
        return changed
    except Exception as error:
        # Secondary reference failures never stop station/history publishing.
        print(f"Price benchmarks: retaining last good reference ({type(error).__name__}: {error}).")
        if cached.get("benchmarks", {}).get("schemaVersion") == 1:
            write_json_if_changed(OUTPUT_PATH, cached["benchmarks"])
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    refresh(force=parser.parse_args().force)
