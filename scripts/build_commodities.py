# Computes a retail/crude commodity dashboard and writes
# data/commodities/dashboard.json.
#
# History snapshots are intentionally sparse (unchanged retail days are omitted),
# so all lag/window/trend calculations below use actual calendar dates rather
# than treating adjacent observations as adjacent days.

import math
import os
import sys
from bisect import bisect_right
from datetime import date as dtdate
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))
from common import content_hash, load_json, write_json_if_changed  # noqa: E402

HISTORY_DIR = "data/history"
CRUDE_PATH = "data/commodities/crude.json"
DASHBOARD_PATH = "data/commodities/dashboard.json"

FUELS = ["gasoline95", "diesel"]
COUNTRIES = ["es", "pt", "combined"]

ROLLING_WINDOW_DAYS = 90
MAX_LAG_DAYS = 14
MAX_CRUDE_CARRY_DAYS = 7
MIN_CORRELATION_SAMPLES = 10


def _avg(values):
    if not values:
        return None
    return sum(values) / len(values)


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


def _dated_points(points):
    parsed = []
    for point in points:
        try:
            day = dtdate.fromisoformat(point["date"])
            value = float(point["value"])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(value):
            parsed.append((day, value))
    parsed.sort(key=lambda item: item[0])
    return parsed


def _preceding_value(days, values, target):
    """Return (day, value) for the latest observation <= target, if fresh enough."""
    idx = bisect_right(days, target) - 1
    if idx < 0:
        return None
    matched_day = days[idx]
    if target - matched_day > timedelta(days=MAX_CRUDE_CARRY_DAYS):
        return None
    return matched_day, values[idx]


def _aligned(crude_points, retail_points, lag_days=0, window_days=ROLLING_WINDOW_DAYS):
    """Calendar-align crude to retail snapshots.

    For a retail snapshot on date R and lag L, use the latest crude close on or
    before R-L calendar days. Weekend/holiday carry-forward is allowed for up to
    MAX_CRUDE_CARRY_DAYS, but stale crude values are not silently reused forever.
    Returns [(crude_date, crude_value, retail_date, retail_value), ...].
    """
    crude = _dated_points(crude_points)
    retail = _dated_points(retail_points)
    if not crude or not retail:
        return []

    latest_retail_day = retail[-1][0]
    window_start = latest_retail_day - timedelta(days=window_days - 1)
    retail = [(day, value) for day, value in retail if day >= window_start]

    crude_days = [day for day, _ in crude]
    crude_values = [value for _, value in crude]
    lag = timedelta(days=lag_days)

    aligned = []
    for retail_day, retail_value in retail:
        matched = _preceding_value(crude_days, crude_values, retail_day - lag)
        if matched is None:
            continue
        crude_day, crude_value = matched
        aligned.append((crude_day, crude_value, retail_day, retail_value))
    return aligned


def _aligned_changes(aligned):
    """Return paired crude/retail changes between successive snapshots.

    Correlating price *levels* can report a strong relationship merely because
    both series trend over time. Using changes makes the metric describe whether
    retail moves with crude instead of whether both happened to trend together.
    """
    crude_changes = []
    retail_changes = []
    for i in range(1, len(aligned)):
        crude_delta = aligned[i][1] - aligned[i - 1][1]
        retail_delta = aligned[i][3] - aligned[i - 1][3]
        if math.isfinite(crude_delta) and math.isfinite(retail_delta):
            crude_changes.append(crude_delta)
            retail_changes.append(retail_delta)
    return crude_changes, retail_changes


def _lag_correlation(crude_points, retail_points, window_days):
    best_lag = 0
    best_r = -2.0
    best_samples = 0

    for lag in range(0, MAX_LAG_DAYS + 1):
        aligned = _aligned(crude_points, retail_points, lag_days=lag, window_days=window_days)
        xs, ys = _aligned_changes(aligned)
        if len(xs) < MIN_CORRELATION_SAMPLES:
            continue
        r = _pearson(xs, ys)
        if r > best_r:
            best_r = r
            best_lag = lag
            best_samples = len(xs)

    if best_r < -1.0:
        return 0, 0.0, 0
    return best_lag, best_r, best_samples


def _rocket_feather(aligned):
    if len(aligned) < 3:
        return 0.0, 0.0, 1.0

    up_deltas = []
    down_deltas = []
    for i in range(1, len(aligned)):
        crude_delta = aligned[i][1] - aligned[i - 1][1]
        retail_delta = aligned[i][3] - aligned[i - 1][3]
        if crude_delta > 0:
            up_deltas.append(retail_delta)
        elif crude_delta < 0:
            down_deltas.append(retail_delta)

    up_avg = _avg(up_deltas) or 0.0
    down_avg = _avg(down_deltas) or 0.0
    asymmetry = down_avg / up_avg if up_avg != 0 else 1.0
    return up_avg, down_avg, asymmetry


def _trend(points, days):
    parsed = _dated_points(points)
    if len(parsed) < 2:
        return None

    current_day, current_value = parsed[-1]
    target = current_day - timedelta(days=days)
    point_days = [day for day, _ in parsed]
    point_values = [value for _, value in parsed]
    matched = _preceding_value(point_days, point_values, target)
    if matched is None:
        return None
    _, previous_value = matched
    if previous_value == 0:
        return None
    return round(((current_value - previous_value) / previous_value) * 100, 2)


def _load_retail_history():
    retail = {f"{fuel}_{country}": [] for country in COUNTRIES for fuel in FUELS}
    if not os.path.isdir(HISTORY_DIR):
        return retail, False

    for year in sorted(os.listdir(HISTORY_DIR)):
        year_path = os.path.join(HISTORY_DIR, year)
        if not os.path.isdir(year_path) or not year.isdigit():
            continue
        for fname in sorted(os.listdir(year_path)):
            if not fname.endswith(".json"):
                continue
            try:
                dtdate.fromisoformat(fname[:-5])
            except ValueError:
                continue

            day_stations = load_json(os.path.join(year_path, fname))
            if not isinstance(day_stations, list):
                continue

            date = fname[:-5]
            sums = {key: 0.0 for key in retail}
            counts = {key: 0 for key in retail}

            for station in day_stations:
                sid = station.get("id", "")
                if sid.startswith("es-"):
                    country = "es"
                elif sid.startswith("pt-"):
                    country = "pt"
                else:
                    continue

                fuels = station.get("fuels", {})
                for fuel in FUELS:
                    price = fuels.get(fuel)
                    if isinstance(price, (int, float)) and price > 0 and math.isfinite(price):
                        key = f"{fuel}_{country}"
                        sums[key] += price
                        counts[key] += 1

            for fuel in FUELS:
                es_key = f"{fuel}_es"
                pt_key = f"{fuel}_pt"
                combined_key = f"{fuel}_combined"
                sums[combined_key] = sums[es_key] + sums[pt_key]
                counts[combined_key] = counts[es_key] + counts[pt_key]

            for key in retail:
                if counts[key] > 0:
                    retail[key].append(
                        {"date": date, "value": round(sums[key] / counts[key], 3)}
                    )

    return retail, True


def run():
    existing_dashboard = load_json(DASHBOARD_PATH, default={}) or {}
    crude_data = load_json(CRUDE_PATH, default={}) or {}
    crude_series = crude_data.get("series", {})
    brent_points = crude_series.get("brent", [])
    wti_points = crude_series.get("wti", [])

    status = "ok"
    if not brent_points and not wti_points:
        print("build_commodities: no crude data available — dashboard will be empty.")
        status = "no_crude"

    retail, history_exists = _load_retail_history()
    if not history_exists:
        print("build_commodities: history directory not found — skipping retail.")
        if status == "ok":
            status = "no_history"

    metrics = {}
    crude_for_analysis = brent_points if brent_points else wti_points

    for fuel in FUELS:
        for country in COUNTRIES:
            key = f"{fuel}_{country}"
            points = retail.get(key, [])

            if not points or not crude_for_analysis:
                metrics[key] = {
                    "fuel": fuel,
                    "country": country,
                    "status": "insufficient_data",
                    "lagDays": 0,
                    "correlation": 0.0,
                    "sampleCount": 0,
                    "rocket": 0.0,
                    "feather": 0.0,
                    "asymmetry": 1.0,
                    "crudeTrend7d": None,
                    "crudeTrend30d": None,
                }
                continue

            lag, correlation, sample_count = _lag_correlation(
                crude_for_analysis, points, ROLLING_WINDOW_DAYS
            )
            zero_lag = _aligned(
                crude_for_analysis, points, lag_days=0, window_days=ROLLING_WINDOW_DAYS
            )
            rocket, feather, asymmetry = _rocket_feather(zero_lag)

            metrics[key] = {
                "fuel": fuel,
                "country": country,
                "status": "ok" if sample_count >= MIN_CORRELATION_SAMPLES else "insufficient_data",
                "lagDays": lag,
                "correlation": correlation,
                "sampleCount": sample_count,
                "rocket": rocket,
                "feather": feather,
                "asymmetry": asymmetry,
                "crudeTrend7d": _trend(crude_for_analysis, 7),
                "crudeTrend30d": _trend(crude_for_analysis, 30),
            }

    stable_output = {
        "schemaVersion": 2,
        "status": status,
        "source": crude_data.get("source", "FRED"),
        "unit": crude_data.get("unit", "USD/barrel"),
        "analysis": {
            "rollingWindowDays": ROLLING_WINDOW_DAYS,
            "maxLagDays": MAX_LAG_DAYS,
            "lagUnit": "calendar_days",
            "correlationBasis": "snapshot_price_changes",
            "historySampling": "sparse_price_change_snapshots",
        },
        "crude": {
            "brent": brent_points,
            "wti": wti_points,
        },
        "retail": retail,
        "metrics": metrics,
    }

    existing_stable = {k: v for k, v in existing_dashboard.items() if k != "lastUpdated"}
    if content_hash(existing_stable) == content_hash(stable_output):
        print(f"build_commodities: unchanged, status={status}, metric_groups={len(metrics)}")
        return False

    output = {
        "lastUpdated": datetime.now(timezone.utc).isoformat(),
        **stable_output,
    }
    write_json_if_changed(DASHBOARD_PATH, output)
    print(f"build_commodities: status={status}, metric_groups={len(metrics)}")
    return True


if __name__ == "__main__":
    run()
