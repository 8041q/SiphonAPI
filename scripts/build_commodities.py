# Computes a retail/crude commodity dashboard and writes
# data/commodities/dashboard.json.
#
# Reads the daily retail averages per country/fuel from data/history/{YYYY}/{YYYY-MM-DD}.json
# (produced by build_history.py in the same workflow), then reads Brent + WTI crude closes from
# data/commodities/crude.json (produced by fetch_crude.py). Aligns both date series by picking
# the nearest preceding crude close for each trading day, then computes per-fuel, per-country
# metrics: optimal lag days, Pearson correlation, and rocket-feather piecewise asymmetry.

import math
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
from common import load_json, write_json_if_changed  # noqa: E402

HISTORY_DIR = "data/history"
CRUDE_PATH = "data/commodities/crude.json"
DASHBOARD_PATH = "data/commodities/dashboard.json"

FUELS = ["gasoline95", "diesel"]
COUNTRIES = ["es", "pt", "combined"]

ROLLING_WINDOW = 90
MAX_LAG = 14

# ---------- helpers ----------

def _avg(lst):
    if not lst:
        return None
    return sum(lst) / len(lst)


def _pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return 0.0
    mx = _avg(xs) or 0.0
    my = _avg(ys) or 0.0
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    dx = sum((x - mx) ** 2 for x in xs)
    dy = sum((y - my) ** 2 for y in ys)
    den = math.sqrt(dx * dy)
    if den == 0:
        return 0.0
    return num / den


def _aligned(crude_points, retail_points):
    """Return [(crude_date, crude_value, retail_date, retail_value), ...]"""
    crude_map = {}  # date -> value
    for p in crude_points:
        crude_map[p["date"]] = p["value"]
    crude_dates = sorted(crude_map.keys())

    by_date = {p["date"]: p["value"] for p in retail_points}

    matched = []
    for rdate in sorted(by_date):
        rval = by_date[rdate]
        best = None
        for cd in reversed(crude_dates):
            if cd <= rdate:
                best = cd
                break
        if best is None:
            continue
        matched.append((best, crude_map[best], rdate, rval))
    return matched


def _lag_correlation(aligned, window_days):
    if len(aligned) > window_days:
        aligned = aligned[-window_days:]

    n = len(aligned)
    if n < 10:
        return 0, 0.0

    crude_dates = [t[0] for t in aligned]
    crude_vals = [t[1] for t in aligned]
    retvals = [t[3] for t in aligned]

    crude_index = {crude_dates[i]: i for i in range(n)}

    best_lag = 0
    best_r = -2.0

    for lag in range(0, MAX_LAG + 1):
        xs = []
        ys = []
        for idx in range(n):
            remote = idx - lag
            if remote < 0:
                continue
            xs.append(crude_vals[remote])
            ys.append(retvals[idx])
        if len(xs) < 3:
            continue
        r = _pearson(xs, ys)
        if r > best_r:
            best_r = r
            best_lag = lag

    if best_r < -1.0:
        return 0, 0.0
    return best_lag, best_r


def _rocket_feather(aligned, window):
    if len(aligned) > window:
        aligned = aligned[-window:]
    if len(aligned) < 3:
        return 0.0, 0.0, 1.0

    up_deltas = []
    dn_deltas = []

    for i in range(1, len(aligned)):
        crude_d = aligned[i][1] - aligned[i - 1][1]
        retail_d = aligned[i][3] - aligned[i - 1][3]
        if crude_d > 0:
            up_deltas.append(retail_d)
        elif crude_d < 0:
            dn_deltas.append(retail_d)

    up_avg = _avg(up_deltas) or 0.0
    dn_avg = _avg(dn_deltas) or 0.0
    asymmetry = dn_avg / up_avg if up_avg != 0 else 1.0
    return up_avg, dn_avg, asymmetry


def _trend(pts, days):
    if len(pts) < days:
        return None
    prev = pts[-days]["value"]
    curr = pts[-1]["value"]
    if prev == 0:
        return None
    return round(((curr - prev) / prev) * 100, 2)


# ---------- main ----------

def run():
    # Fallback for existing, already outdated outputs
    dashboard = load_json(DASHBOARD_PATH, default={}) or {}

    # 1. Load crude
    crude_data = load_json(CRUDE_PATH, default={}) or {}
    crude_series = crude_data.get("series", {})
    brent_pts = crude_series.get("brent", [])
    wti_pts = crude_series.get("wti", [])

    status = "ok"
    if not brent_pts and not wti_pts:
        print("build_commodities: no crude data available — dashboard will be empty.")
        status = "no_crude"

    # 2. Gather retail averages from history day files
    #    key = "<fuel>_<country>" -> [{date, value}]
    retail = {}
    for c in COUNTRIES:
        for f in FUELS:
            retail[f"{f}_{c}"] = []

    if os.path.isdir(HISTORY_DIR):
        for year in sorted(os.listdir(HISTORY_DIR)):
            ypath = os.path.join(HISTORY_DIR, year)
            if not os.path.isdir(ypath) or not year.isdigit():
                continue
            for fname in sorted(os.listdir(ypath)):
                if not fname.endswith(".json"):
                    continue

                day_stations = load_json(os.path.join(ypath, fname))
                if day_stations is None:
                    continue

                date = fname[:-5]

                sums = {f"{f}_{c}": 0.0 for c in COUNTRIES for f in FUELS}
                cnts = {f"{f}_{c}": 0 for c in COUNTRIES for f in FUELS}

                for st in day_stations:
                    sid = st.get("id", "")
                    if sid.startswith("es-"):
                        country = "es"
                    elif sid.startswith("pt-"):
                        country = "pt"
                    else:
                        continue

                    fuels = st.get("fuels", {})
                    for f in FUELS:
                        price = fuels.get(f)
                        if isinstance(price, (int, float)) and price > 0 and math.isfinite(price):
                            key = f"{f}_{country}"
                            sums[key] += price
                            cnts[key] += 1

                # combined = es + pt
                for f in FUELS:
                    es_key = f"{f}_es"
                    pt_key = f"{f}_pt"
                    comb_key = f"{f}_combined"
                    sums[comb_key] = sums[es_key] + sums[pt_key]
                    cnts[comb_key] = cnts[es_key] + cnts[pt_key]

                for key in sums:
                    if cnts[key] > 0:
                        retail[key].append({"date": date, "value": round(sums[key] / cnts[key], 3)})

            # end-for each file
        # end-for each year
    else:
        print("build_commodities: history directory not found — skipping retail.")
        if status == "ok":
            status = "no_history"

    # 3. Compute metrics for each fuel x country against crude
    metrics = {}
    crude_for_analysis = brent_pts if brent_pts else wti_pts

    for f in FUELS:
        for c in COUNTRIES:
            k = f"{f}_{c}"
            points = retail.get(k, [])

            if not points or not crude_for_analysis:
                metrics[k] = {
                    "fuel": f,
                    "country": c,
                    "status": "insufficient_data",
                    "lagDays": 0,
                    "correlation": 0.0,
                    "rocket": 0.0,
                    "feather": 0.0,
                    "asymmetry": 1.0,
                    "crudeTrend7d": None,
                    "crudeTrend30d": None,
                }
                continue

            adj = _aligned(crude_for_analysis, points)
            lag, corr = _lag_correlation(adj, ROLLING_WINDOW)
            rock, feat, asym = _rocket_feather(adj, ROLLING_WINDOW)

            metrics[k] = {
                "fuel": f,
                "country": c,
                "status": "ok" if len(adj) >= 3 else "insufficient_data",
                "lagDays": lag,
                "correlation": corr,
                "rocket": rock,
                "feather": feat,
                "asymmetry": asym,
                "crudeTrend7d": _trend(crude_for_analysis, 7),
                "crudeTrend30d": _trend(crude_for_analysis, 30),
            }

    # 4. Build output
    output = {
        "lastUpdated": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "source": crude_data.get("source", "FRED"),
        "unit": crude_data.get("unit", "USD/barrel"),
        "crude": {
            "brent": brent_pts,
            "wti": wti_pts,
        },
        "retail": retail,
        "metrics": metrics,
    }

    write_json_if_changed(DASHBOARD_PATH, output)
    print(f"build_commodities: status={status}, metric_groups={len(metrics)}")


if __name__ == "__main__":
    run()