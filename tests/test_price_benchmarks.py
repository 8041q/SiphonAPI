import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import date
from unittest.mock import Mock, patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import price_benchmarks as prices
import fetch_price_benchmarks as fetcher
from common import write_json_if_changed, load_json


def fixture():
    columns = [('B', 'ES', 'euro95'), ('C', 'ES', 'diesel'), ('D', 'PT', 'euro95'), ('E', 'PT', 'diesel'), ('F', 'ES', 'LPG'), ('G', 'PT', 'LPG')]
    rows = [{column: f'{country}_price_wo_tax_{fuel}' for column, country, fuel in columns}, {}, {}]
    months = {}
    for year in range(2010, 2020):
        for month in range(1, 13):
            day = date(year, month, 15)
            serial = (day - date(1899, 12, 30)).days
            rows.append({'A': str(serial), **{col: str(400 + month * 15 + (80 if country == 'PT' else 0)) for col, country, _ in columns}})
            months[day.strftime('%Y-%m')] = len(months)
    months['2026-09'] = len(months)
    inflation = {'id': ['freq', 'unit', 'coicop18', 'geo', 'time'], 'size': [1, 1, 1, 2, len(months)],
        'dimension': {'unit': {'category': {'index': {'I25': 0}}}, 'coicop18': {'category': {'index': {'TOT_X_NRG': 0}}},
            'geo': {'category': {'index': {'ES': 0, 'PT': 1}}}, 'time': {'category': {'index': months}}},
        'value': {str(country * len(months) + index): 120 if month == '2026-09' else 100 for country in range(2) for month, index in months.items()}}
    tax_rows = [{'A': country + '_', 'B': '40000', 'C': value, 'D': value, 'H': value} for country in ('PT', 'ES') for value in ('20',)]
    taxes = {'VAT': tax_rows, 'Excise duties': [{**r, 'C': '400', 'D': '300', 'H': '50'} for r in tax_rows],
             'Other Indirect Taxes': [{**r, 'C': '0', 'D': '0', 'H': '0'} for r in tax_rows]}
    return {'Prices wo taxes': rows}, taxes, inflation


class BenchmarkTests(unittest.TestCase):
    def test_reference_is_fixed_and_does_not_normalize_a_crisis(self):
        history, taxes, inflation = fixture()
        original = prices.build_benchmarks(history, taxes, inflation, '2026-09-28')
        history['Prices wo taxes'].extend([{'A': '46200', 'B': '3000', 'C': '3000', 'D': '3000', 'E': '3000'}] * 200)
        self.assertEqual(original, prices.build_benchmarks(history, taxes, inflation, '2026-09-28'))
        for band in original['bands'].values():
            self.assertLess(band['redAbove'], 2)
            self.assertEqual(band['referenceMonths'], 120)
        self.assertNotIn('lpg_pt', original['bands'])

    def test_energy_excluding_inflation_and_tax_changes_adjust_the_reference(self):
        h, taxes, inflation = fixture()
        original = prices.build_benchmarks(h, taxes, inflation, '2026-09-28')['bands']['diesel_es']
        taxes['Excise duties'].append({'A': 'ES_', 'B': '46000', 'D': '400'})
        changed = prices.build_benchmarks(h, taxes, inflation, '2026-09-28')['bands']['diesel_es']
        self.assertAlmostEqual(changed['redAbove'] - original['redAbove'], 0.12, places=4)
        inflation['dimension']['coicop18']['category']['index'] = {'TOTAL': 0}
        with self.assertRaises(ValueError): prices.build_benchmarks(h, taxes, inflation, '2026-09-28')

    def test_missing_or_stale_inflation_and_incomplete_history_are_rejected(self):
        h, taxes, inflation = fixture()
        with self.assertRaises(ValueError): prices.build_benchmarks(h, taxes, inflation, '2028-01-01')
        h['Prices wo taxes'] = h['Prices wo taxes'][:30]
        with self.assertRaises(ValueError): prices.build_benchmarks(h, taxes, inflation, '2026-09-28')

    def test_partial_tax_rows_retain_each_fuels_previous_rate_and_zero_is_valid(self):
        series = prices.tax_series([{'VAT': [{'A': 'PT_', 'B': '40000', 'C': '23', 'D': '23'}, {'B': '45000', 'C': '20'}],
            'Excise duties': [{'A': 'PT_', 'B': '40000', 'C': '400', 'D': '300'}],
            'Other Indirect Taxes': [{'A': 'PT_', 'B': '40000', 'C': '0', 'D': '0'}]}])
        self.assertEqual(prices.tax_at(series, 'PT', 'diesel', '2026-09-28')['vat'], 0.23)
        self.assertEqual(prices.tax_at(series, 'PT', 'gasoline95', '2026-09-28')['vat'], 0.2)
        self.assertEqual(prices.tax_at(series, 'PT', 'diesel', '2026-09-28')['other'], 0)

    def test_workbook_uses_relationship_targets_and_shared_or_inline_strings(self):
        out = io.BytesIO()
        with zipfile.ZipFile(out, 'w') as archive:
            archive.writestr('xl/workbook.xml', '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Prices wo taxes" sheetId="99" r:id="r1"/></sheets></workbook>')
            archive.writestr('xl/_rels/workbook.xml.rels', '<Relationships><Relationship Id="r1" Target="worksheets/sheet7.xml"/></Relationships>')
            archive.writestr('xl/sharedStrings.xml', '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>ES_price_wo_tax_euro95</t></si></sst>')
            archive.writestr('xl/worksheets/sheet7.xml', '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row><c r="B1" t="s"><v>0</v></c><c r="C1" t="inlineStr"><is><t>diesel</t></is></c></row><row><c r="A2"><v>46000</v></c></row></sheetData></worksheet>')
        rows = prices.workbook_rows(out.getvalue())['Prices wo taxes']
        self.assertEqual(rows[0], {'B': 'ES_price_wo_tax_euro95', 'C': 'diesel'})
        self.assertEqual(rows[1]['A'], '46000')

    def test_weekly_cache_and_outage_keep_last_good_data_without_new_freshness(self):
        before = os.getcwd()
        with tempfile.TemporaryDirectory() as directory:
            os.chdir(directory)
            try:
                h, taxes, inflation = fixture()
                benchmark = prices.build_benchmarks(h, taxes, inflation, '2026-09-28')
                write_json_if_changed(fetcher.CACHE_PATH, {'checkedAt': '2026-10-01', 'benchmarks': benchmark})
                session = Mock(); session.get.side_effect = RuntimeError('offline')
                fetcher.refresh(session=session, today=date(2026, 10, 2))
                session.get.assert_not_called()
                fetcher.refresh(force=True, session=session, today=date(2026, 10, 2))
                self.assertEqual(load_json(fetcher.OUTPUT_PATH), benchmark)
                self.assertEqual(load_json(fetcher.CACHE_PATH)['checkedAt'], '2026-10-01')
            finally: os.chdir(before)
