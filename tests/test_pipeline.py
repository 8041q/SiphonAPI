import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import fetch_spain as es
import fetch_portugal as pt
import fetch_crude as crude
import build_history as history
import build_commodities as commodities
import build_manifest as manifest
from common import load_json, content_hash, write_json_if_changed
from test_price_benchmarks import fixture
from price_benchmarks import build_benchmarks


class PipelineTests(unittest.TestCase):
    def test_fetch_history_benchmarks_dashboard_root_and_idempotent_rerun(self):
        before = os.getcwd()
        with tempfile.TemporaryDirectory() as directory:
            os.chdir(directory)
            try:
                spain = {'Fecha': '02/10/2026 09:00:00', 'ListaEESSPrecio': [{'IDEESS': '1', 'Latitud': '40,4', 'Longitud (WGS84)': '-3,7', 'Rótulo': 'Test', 'Dirección': 'Road', 'Municipio': 'Madrid', 'Provincia': 'Madrid', 'Tipo Venta': 'P', 'Precio Gasolina 95 E5': '1,600', 'Precio Gasoleo A': '1,500'}]}
                portugal = {'Id': 1, 'Nome': 'Test', 'Marca': 'Test', 'Morada': 'Road', 'Municipio': 'Lisboa', 'Distrito': 'Lisboa', 'Latitude': 38.7, 'Longitude': -9.1, 'DataAtualizacao': '2026-10-02 09:00', 'Preco': '1,600'}
                h, taxes, inflation = fixture()
                benchmark = build_benchmarks(h, taxes, inflation, '2026-09-28')
                with patch.object(es, 'SOURCE_URL', 'https://fixture.invalid'), patch.object(es, 'fetch_json', return_value=spain), patch.object(pt, 'fetch_fuel', return_value=[portugal]), patch.object(pt, 'fetch_station_enrichment', return_value={'services': [], 'hours': None, 'paymentMethods': [], 'otherServices': None, 'observations': None}), patch.object(pt.time, 'sleep'), patch.dict(os.environ, {'FRED_API_KEY': 'fixture'}), patch.object(crude, '_fetch_one', return_value=[{'date': '2026-10-01', 'value': 80}]):
                    self.assertTrue(es.run()); self.assertTrue(pt.run())
                    history.run(); self.assertTrue(crude.run())
                    write_json_if_changed('data/commodities/price-benchmarks.json', benchmark)
                    self.assertTrue(commodities.run()); self.assertTrue(manifest.run())
                    dashboard = load_json(commodities.DASHBOARD_PATH)
                    root = load_json(manifest.MANIFEST_PATH)
                    self.assertEqual(root['commodities']['hash'], content_hash(dashboard))
                    self.assertEqual(dashboard['priceBenchmarks'], benchmark)
                    self.assertEqual(set(root['countries']), {'ES', 'PT'})
                    self.assertEqual(len(load_json(history.INDEX_PATH)['days']), 1)
                    first_root = root
                    self.assertFalse(es.run()); self.assertFalse(pt.run())
                    self.assertFalse(history.run()); self.assertFalse(crude.run())
                    self.assertFalse(commodities.run()); self.assertFalse(manifest.run())
                    self.assertEqual(load_json(manifest.MANIFEST_PATH), first_root)
            finally: os.chdir(before)
