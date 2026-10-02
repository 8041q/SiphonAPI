"""Stable, inflation/tax-adjusted pump-price bands. Never fit to today's stations.

The 2010–2019 reference is deliberately fixed: moving windows would eventually
turn an extended energy shock into a green "normal". Monthly medians give every
month equal weight; the lower/upper quartiles describe that historical range.
This is a historical price comparison, not a claim about household affordability.
"""
import io
import math
import posixpath
import re
import statistics
import zipfile
from datetime import date, timedelta
from xml.etree import ElementTree as ET

REFERENCE_START = "2010-01"
REFERENCE_END = "2019-12"
MIN_MONTHS = 96
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
FUELS = {"euro95": ("gasoline95", "C"), "diesel": ("diesel", "D"), "LPG": ("lpg", "H")}


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def excel_day(value):
    n = number(value)
    if n is not None and 30000 <= n <= 80000:
        return (date(1899, 12, 30) + timedelta(days=int(n))).isoformat()
    return None


def workbook_rows(content):
    """Read XLSX cells with stdlib only; resolve sheet names through relationships."""
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared = ["".join(node.itertext()) for node in
                      ET.fromstring(archive.read("xl/sharedStrings.xml")).findall("m:si", NS)]
        rels = {node.attrib["Id"]: node.attrib["Target"] for node in
                ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))}
        result = {}
        for sheet in ET.fromstring(archive.read("xl/workbook.xml")).findall("m:sheets/m:sheet", NS):
            rel = sheet.attrib["{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"]
            target = rels[rel]
            path = target.lstrip("/") if target.startswith("/") else posixpath.normpath("xl/" + target)
            rows = []
            for row in ET.fromstring(archive.read(path)).findall("m:sheetData/m:row", NS):
                values = {}
                for cell in row.findall("m:c", NS):
                    column = re.sub(r"\d+", "", cell.attrib["r"])
                    value = cell.find("m:v", NS)
                    kind = cell.attrib.get("t")
                    if kind == "inlineStr":
                        values[column] = "".join(cell.find("m:is", NS).itertext())
                    elif value is not None and value.text is not None:
                        values[column] = shared[int(value.text)] if kind == "s" else value.text
                rows.append(values)
            result[sheet.attrib["name"]] = rows
        return result


def inflation_series(payload):
    """Decode JSON-stat by dimension strides, including absent/provisional months."""
    ids, sizes = payload["id"], payload["size"]
    dims = payload["dimension"]
    if sizes[ids.index("unit")] != 1 or "I25" not in dims["unit"]["category"]["index"]:
        raise ValueError("Expected one consistently rebased inflation index (I25)")
    if "TOT_X_NRG" not in dims["coicop18"]["category"]["index"]:
        raise ValueError("Inflation must exclude energy")
    strides = [math.prod(sizes[i + 1:]) for i in range(len(sizes))]
    result = {}
    for country, position in dims["geo"]["category"]["index"].items():
        points = {}
        for month, time_position in dims["time"]["category"]["index"].items():
            index = position * strides[ids.index("geo")] + time_position * strides[ids.index("time")]
            raw = payload["value"]
            value = number(raw.get(str(index)) if isinstance(raw, dict) else raw[index])
            if value is not None and value > 0:
                points[month] = value
        result[country] = points
    return result


def tax_series(tables):
    """Combine effective-dated histories and current tables, per individual fuel.

    Country names are merged cells in historical sheets. Empty cells mean no
    new rate for that fuel, whereas zero is a real published tax rate.
    """
    result = {}
    for sheets in tables:
        for sheet_name, kind in (("VAT", "vat"), ("Excise duties", "excise"),
                                 ("Other Indirect Taxes", "other")):
            country = None
            for row in sheets.get(sheet_name, []):
                if row.get("A"):
                    country = row["A"].rstrip("_")
                effective = excel_day(row.get("B"))
                if country not in ("ES", "PT") or not effective:
                    continue
                for fuel, column in FUELS.values():
                    value = number(row.get(column))
                    if value is not None and value >= 0:
                        result.setdefault((country, fuel, kind), {})[effective] = value / (100 if kind == "vat" else 1000)
    return result


def tax_at(taxes, country, fuel, day):
    result = {}
    for kind in ("vat", "excise", "other"):
        rates = taxes.get((country, fuel, kind), {})
        eligible = [effective for effective in rates if effective <= day]
        if not eligible:
            return None
        result[kind] = rates[max(eligible)]
    if result["vat"] > 0.5 or result["excise"] > 2 or result["other"] > 2:
        return None
    return result


def quantile(values, fraction):
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low = math.floor(position)
    high = math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def build_benchmarks(history, current_taxes, inflation, as_of):
    indices = inflation_series(inflation)
    taxes = tax_series([current_taxes, history])  # history can contain newer component rates
    rows = history.get("Prices wo taxes", [])
    if not rows:
        raise ValueError("Missing tax-exclusive fuel history")
    columns = {}
    for column, label in rows[0].items():
        match = re.fullmatch(r"(PT|ES)_price_wo_tax_(euro95|diesel|LPG)", label)
        if match:
            country, name = match.groups()
            fuel = FUELS[name][0]
            # PT station LPG is €/kg; the Bulletin series is €/L. Never alias units.
            if country == "PT" and fuel == "lpg":
                continue
            columns[column] = (country, fuel)
    monthly = {}
    for row in rows[3:]:
        day = excel_day(row.get("A"))
        if not day or not REFERENCE_START <= day[:7] <= REFERENCE_END:
            continue
        for column, (country, fuel) in columns.items():
            value = number(row.get(column))
            index = indices.get(country, {}).get(day[:7])
            if value is not None and 0 < value < 10000 and index:
                monthly.setdefault((country, fuel), {}).setdefault(day[:7], []).append(value / 1000 / index)
    bands = {}
    for (country, fuel), months in monthly.items():
        if len(months) < MIN_MONTHS:
            continue
        available = [month for month in indices[country] if month <= as_of[:7]]
        if not available:
            continue
        index_month = max(available)
        if (date.fromisoformat(as_of) - date.fromisoformat(index_month + "-01")).days > 120:
            continue
        rates = tax_at(taxes, country, fuel, as_of)
        if not rates:
            continue
        # Equal month weights prevent weeks or reporting gaps skewing the reference.
        real_values = [statistics.median(values) for values in months.values()]
        current_index = indices[country][index_month]
        def pump(value):
            return round((value * current_index + rates["excise"] + rates["other"]) * (1 + rates["vat"]), 4)
        green, red = pump(quantile(real_values, 0.25)), pump(quantile(real_values, 0.75))
        if green <= 0 or red <= green:
            continue
        bands[f"{fuel}_{country.lower()}"] = {
            "fuel": fuel, "country": country, "unit": "EUR/L", "greenBelow": green,
            "redAbove": red, "reference": pump(statistics.median(real_values)),
            "referenceMonths": len(months), "inflationMonth": index_month,
            "taxes": rates,
        }
    if any(f"{fuel}_{country}" not in bands for fuel in ("gasoline95", "diesel") for country in ("pt", "es")):
        raise ValueError("Incomplete regular gasoline/diesel reference")
    return {"schemaVersion": 1, "method": "anchored_real_net_price_quartiles",
            "referenceStart": REFERENCE_START, "referenceEnd": REFERENCE_END,
            "asOf": as_of, "source": "European Commission Weekly Oil Bulletin / Eurostat HICP excluding energy",
            "bands": bands}
