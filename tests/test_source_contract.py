"""Plugin API v2 contract: every bundled data source, v1 compatibility, registry behaviour.

Run: python3 -m unittest tests.test_source_contract
"""
import inspect
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import sources  # noqa: E402
from sources.base import (API_VERSION, SETTING_TYPES, DataSource, Setting, SourceError, Station,  # noqa: E402
                          validate_settings)
from fetcher import FetchPolicy  # noqa: E402

CFG = {'paths': {'image': os.path.join(tempfile.gettempdir(), 'lu-contract', 'x.img')}, 'fetch': {}}


class V1Source(DataSource):
    """A plugin written for contract v1 (no api_version, needs_update)."""
    id = 'test_v1'
    name = 'V1'
    listed = False

    def needs_update(self):
        return True, 'sig-1'

    def fetch_stations(self):
        return [Station(id='3D01', name='A', freq_raw='98,5', ecc='E2')]


class Contract(unittest.TestCase):
    def test_every_bundled_plugin(self):
        found = sources.discover(force=True)
        self.assertIn('example', found)
        for sid, cls in found.items():
            with self.subTest(plugin=sid):
                self.assertTrue(issubclass(cls, DataSource))
                self.assertEqual(cls.id, sid)
                self.assertTrue(cls.name)
                self.assertEqual(cls.api_version, API_VERSION, 'bundled plugins follow the current contract')
                self.assertIsInstance(cls.fetch_policy, FetchPolicy)
                self.assertGreaterEqual(cls.fetch_policy.min_interval, 0)
                self.assertFalse(inspect.isabstract(cls))
                src = sources.bind(cls({}), CFG)
                self.assertEqual(src.http is None, not cls.fetch_policy.network)
        self.assertEqual(sources.import_errors(), {})

    def test_no_own_http_sessions(self):
        """Plugins reach the network only through self.http (rate limits, robots.txt)."""
        for sid, cls in sources.discover().items():
            code = inspect.getsource(sys.modules[cls.__module__])
            with self.subTest(plugin=sid):
                self.assertNotIn('requests.Session', code)
                self.assertNotIn('requests.get', code)
                self.assertNotIn('urlopen', code)

    def test_template_hidden_from_ui(self):
        self.assertIn('example', sources.available())
        self.assertNotIn('example', sources.available(listed_only=True))
        self.assertIn('none', sources.available(listed_only=True))

    def test_template_says_not_configured(self):
        src = sources.bind(sources.available()['example']({}), CFG)
        with self.assertRaises(SourceError) as cm:
            list(src.fetch_stations())
        self.assertEqual(cm.exception.key, 'err.source_not_configured')

    def test_csv_missing_file_is_a_source_error(self):
        src = sources.bind(sources.available()['csv_file']({'path': '/nonexistent/x.csv'}), CFG)
        self.assertIsNone(src.change_signature())
        with self.assertRaises(SourceError) as cm:
            list(src.fetch_stations())
        self.assertEqual((cm.exception.key, cm.exception.fmt['path']), ('err.csv_not_found', '/nonexistent/x.csv'))

    def test_v1_plugin_still_works_with_warning(self):
        with self.assertLogs('sources', 'WARNING') as logs:
            src = sources.bind(V1Source({}), CFG)
        self.assertIn('plugin API v1', '\n'.join(logs.output))
        self.assertEqual(src.change_signature(), 'sig-1')
        self.assertIsNotNone(src.http, 'v1 plugins get the default (limited) client')

    def test_progress_and_cancel_hooks(self):
        seen, stop = [], [False]
        src = V1Source({}).bind(None, lambda d, t, m: seen.append((d, t, m)), lambda: stop[0])
        src.progress(1, 3, 'x')
        self.assertFalse(src.cancelled())
        stop[0] = True
        self.assertTrue(src.cancelled())
        self.assertEqual(seen, [(1, 3, 'x')])
        bare = V1Source({})
        bare.progress(1)                      # unbound: no-op
        self.assertFalse(bare.cancelled())

    def test_broken_plugin_reported(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, 'broken_plugin.py'), 'w') as f:
                f.write('raise RuntimeError("boom")\n')
            sources.__path__.append(d)
            try:
                sources.discover(force=True)
                self.assertIn('broken_plugin', sources.import_errors())
                self.assertIn('boom', sources.import_errors()['broken_plugin'])
            finally:
                sources.__path__.remove(d)
                sys.modules.pop('sources.broken_plugin', None)
                sources.discover(force=True)



class SettingsSchema(unittest.TestCase):
    SCHEMA = (Setting('path', 'path', required=True), Setting('default_ecc', 'ecc'),
              Setting('url', 'url'), Setting('n', 'number', min=1, max=10, default=3),
              Setting('on', 'bool'), Setting('mode', 'choice', choices=('a', 'b')), Setting('api_key', 'secret'))

    def test_bundled_schemas_are_valid(self):
        for sid, cls in sources.discover(force=True).items():
            keys = [f.key for f in cls.settings_schema]
            with self.subTest(plugin=sid):
                self.assertEqual(len(keys), len(set(keys)))
                for f in cls.settings_schema:
                    self.assertIn(f.type, SETTING_TYPES)
                    self.assertTrue(f.label)

    def test_clean_values(self):
        got = validate_settings(self.SCHEMA, {'path': ' x.csv ', 'default_ecc': 'e0', 'url': 'https://a.b/c',
                                              'n': '4,5', 'on': 'on', 'mode': 'b', 'junk': 1})
        self.assertEqual(got, {'path': 'x.csv', 'default_ecc': 'E0', 'url': 'https://a.b/c', 'n': 4.5,
                               'on': True, 'mode': 'b'})

    def test_invalid_values_name_the_field(self):
        for data, field in (({'path': ''}, 'path'), ({'path': 'a', 'default_ecc': 'XYZ'}, 'default_ecc'),
                            ({'path': 'a', 'url': 'ftp://x'}, 'url'), ({'path': 'a', 'n': '99'}, 'n'),
                            ({'path': 'a', 'mode': 'c'}, 'mode'), ({'path': 'a\nb'}, 'path')):
            with self.subTest(data=data):
                with self.assertRaises(SourceError) as cm:
                    validate_settings(self.SCHEMA, data)
                self.assertEqual((cm.exception.key, cm.exception.fmt['field']), ('err.setting_invalid', field))

    def test_secret_kept_when_left_empty(self):
        cur = {'path': 'a', 'api_key': 'SECRET'}
        self.assertEqual(validate_settings(self.SCHEMA, {'path': 'a', 'api_key': ''}, cur)['api_key'], 'SECRET')
        self.assertEqual(validate_settings(self.SCHEMA, {'path': 'a', 'api_key': 'NEW'}, cur)['api_key'], 'NEW')

    def test_settings_api_uses_schema(self):
        import config
        import i18n
        import server
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'cfg.yaml')
            with open(p, 'w') as f:
                f.write('sources:\n  csv_file:\n    path: old.csv\n    default_ecc: E1\n    columns: {id: PI}\n')
            old = (server.CFG, server.SOURCE, server.CONFIG_PATH, server.T)
            i18n.load('en')
            server.CFG, server.CONFIG_PATH, server.T = config.load_config(p), p, i18n.catalog()
            try:
                info = server.settings_info()
                csv = next(s for s in info['sources'] if s['id'] == 'csv_file')
                self.assertEqual([(f['key'], f['value']) for f in csv['fields']],
                                 [('path', 'old.csv'), ('default_ecc', 'E1')])
                self.assertNotIn('example', [s['id'] for s in info['sources']])
                with self.assertRaises(ValueError) as cm:
                    server.save_settings({'data_source': 'csv_file', 'sources': {'csv_file': {'path': 'a.csv',
                                                                                             'default_ecc': 'XYZ'}}})
                self.assertIn('Default ECC', str(cm.exception))
                server.save_settings({'data_source': 'csv_file', 'sources': {'csv_file': {'path': 'new.csv',
                                                                                         'default_ecc': ''}}})
                saved = config.load_config(p)['sources']['csv_file']
                self.assertEqual(saved, {'path': 'new.csv', 'columns': {'id': 'PI'}}, 'cleared key removed, others kept')
                self.assertEqual((server.SOURCE.id, server.SOURCE.path), ('csv_file', 'new.csv'))
            finally:
                server.CFG, server.SOURCE, server.CONFIG_PATH, server.T = old


if __name__ == '__main__':
    unittest.main()
