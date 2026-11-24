"""Paths and names that behave differently on Windows, and other review-3 fixes.

Run: python3 -m unittest tests.test_portability
"""
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config  # noqa: E402
import ford_db  # noqa: E402
import qnx6fs  # noqa: E402


class SqlitePaths(unittest.TestCase):
    def test_hash_and_percent_in_path(self):
        """A raw file: URI cut '#...' (and ?mode=ro) off and even created an empty file there."""
        with tempfile.TemporaryDirectory() as d:
            folder = os.path.join(d, 'Sync3 #2 %20 x')
            os.makedirs(folder)
            db = os.path.join(folder, 'fm_station_logo.db')
            c = sqlite3.connect(db)
            c.execute('CREATE TABLE fm_station_logo (ECC, STATION_ID, RECEIVER_TYPE, FREQUENCY, LOGO_FILENAME)')
            c.execute("INSERT INTO fm_station_logo VALUES ('E2','3D01','F','098500','A')")
            c.commit()
            c.close()
            self.assertEqual(config._db_rows(db), 1)
            ro = config.connect_ro(db)      # `with sqlite3.connect()` does not close: Windows keeps the file locked
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    ro.execute("DELETE FROM fm_station_logo")          # really read-only
            finally:
                ro.close()
            self.assertEqual(sorted(os.listdir(d)), ['Sync3 #2 %20 x'], 'no stray file under a cut name')


class Names(unittest.TestCase):
    def test_windows_device_names_and_trailing_dots(self):
        for n in ('CON', 'nul.png', 'Com1.PNG', 'LPT9', 'aux', 'foo.', 'foo ', 'a:b', '../x'):
            self.assertFalse(qnx6fs._safe_name(n), n)
        for n in ('CONSOLE.PNG', 'E2', 'ABC.PNG', '.boot', 'Version.inf', 'COM10'):
            self.assertTrue(qnx6fs._safe_name(n), n)

    def test_safe_join(self):
        base = tempfile.gettempdir()
        self.assertEqual(ford_db.safe_join(base, 'E2', 'X.PNG'), os.path.join(os.path.abspath(base), 'E2', 'X.PNG'))
        for seg in ('..', 'C:evil', 'a/b', 'a\\b', 'x\x00', ''):
            self.assertIsNone(ford_db.safe_join(base, seg), repr(seg))
        root = os.path.abspath(os.sep)                         # "/" or "C:\": a drive root works as base
        self.assertEqual(ford_db.safe_join(root, 'x'), os.path.join(root, 'x'))

    def test_mount_label(self):
        self.assertEqual(config.mount_label('E:\\'), 'E:')
        self.assertEqual(config.mount_label('/media/me/NO NAME/'), 'NO NAME')


class Launcher(unittest.TestCase):
    def test_load_with_trailing_backslash_keeps_options(self):
        import view_stations
        argv = ['--load', 'C:\\cars\\backup" --no-browser --port 8768', '--debug']
        self.assertEqual(view_stations._fix_windows_argv(argv),
                         ['--load', 'C:\\cars\\backup', '--no-browser', '--port', '8768', '--debug'])
        self.assertEqual(view_stations._fix_windows_argv(['--port', '9000']), ['--port', '9000'])


class AtomicWrite(unittest.TestCase):
    def test_parallel_writers_never_collide(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'choices.json')
            errors = []

            def w(i):
                try:
                    for _ in range(20):
                        config.atomic_write(p, f'{{"writer": {i}}}')
                except Exception as e:  # noqa: BLE001
                    errors.append(e)
            ts = [threading.Thread(target=w, args=(i,)) for i in range(6)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            self.assertEqual(errors, [])
            self.assertEqual(os.listdir(d), ['choices.json'], 'no temp files left')


class AiResultDownload(unittest.TestCase):
    def test_redirect_to_lan_refused(self):
        import logo_ai
        import fetcher

        class Resp:
            def __init__(self, status, headers=None, body=b''):
                self.status_code, self.headers, self._b = status, headers or {}, body
                self.text = ''

            def iter_content(self, n):
                yield self._b

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        calls = []

        def fake_get(url, **kw):
            calls.append(url)
            self.assertFalse(kw.get('allow_redirects', True), 'redirects must be handled by hand')
            return Resp(302, {'location': 'http://192.168.1.1/admin'})
        with mock.patch.object(logo_ai.requests, 'get', fake_get), \
                mock.patch.object(fetcher, 'public_http_url', lambda u: '192.168.' not in u):
            with self.assertRaises(logo_ai.AIError):
                logo_ai._download_result('https://cdn.example/result.png')
        self.assertEqual(calls, ['https://cdn.example/result.png'], 'nothing sent to the LAN address')


class UserPlugins(unittest.TestCase):
    def test_plugin_dir_follows_data_dir(self):
        """Docker runs from source with LOGOSUPDATER_DATA=/data: /data/sources must be searched too."""
        import sources
        with mock.patch.dict(os.environ, {'LOGOSUPDATER_DATA': os.path.join(os.sep, 'data')}):
            self.assertEqual(sources._user_plugin_dir(), os.path.join(os.path.abspath(os.path.join(os.sep, 'data')), 'sources'))


if __name__ == '__main__':
    unittest.main()
