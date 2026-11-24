"""Whole update pipeline against a fake station site: RadioPolska plugin -> ford_db -> shared Fetcher.

Checks the polite-fetching rules end to end: one request at a time with spacing, each page/logo once,
robots.txt honoured, a second update served from the signature/cache, cancel, user-facing errors,
and the "public addresses only" check of /api/fetch_remote_logo.
Run: python3 -m unittest tests.test_update_pipeline
"""
import http.server
import io
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import fetcher  # noqa: E402
import ford_db  # noqa: E402
import sources  # noqa: E402
from fetcher import FetchPolicy  # noqa: E402
from PIL import Image  # noqa: E402

SCHEMA = "CREATE TABLE {t} (ECC TEXT, STATION_ID TEXT, RECEIVER_TYPE TEXT, FREQUENCY TEXT, LOGO_FILENAME TEXT)"
GAP = 0.1


def png(color):
    b = io.BytesIO()
    Image.new('RGBA', (160, 120), color).save(b, 'PNG')
    return b.getvalue()


LOGOS = {'1': png((255, 0, 0, 255)), '2': png((0, 255, 0, 255)), '3': png((0, 0, 255, 255))}
LIST = ("<table><tr><th>PI</th><th>Name</th><th>Freq</th></tr>"
        "<tr><td>3D01</td><td><a href='/stacja/1'>Alpha</a></td><td>98,5</td></tr>"
        "<tr><td>3D02</td><td><a href='/stacja/2'>Beta</a></td><td><ul><li>88,7</li><li>101,3</li></ul></td></tr>"
        "<tr><td>3DA1</td><td><a href='/stacja/3'>Gamma</a></td><td>DAB+</td></tr>"
        "<tr><td>3D04</td><td><a href='/stacja/1'>Alpha 2</a></td><td>95,9</td></tr></table>")


class Site(http.server.BaseHTTPRequestHandler):
    log, lock, inflight, peak = [], threading.Lock(), [0], [0]
    robots = "User-agent: *\nDisallow: /private/\n"
    list_page = LIST
    on_page = None

    def log_message(self, *a):
        pass

    def do_GET(self):
        with self.lock:
            self.inflight[0] += 1
            self.peak[0] = max(self.peak[0], self.inflight[0])
            self.log.append((self.path, time.monotonic()))
        try:
            self._serve()
        finally:
            with self.lock:
                self.inflight[0] -= 1

    def _send(self, body, ctype='text/html; charset=utf-8', etag=None):
        if etag and self.headers.get('If-None-Match') == etag:
            self.send_response(304)
            self.send_header('ETag', etag)
            self.end_headers()
            return
        self.send_response(200)
        if ctype:
            self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        if etag:
            self.send_header('ETag', etag)
        self.end_headers()
        self.wfile.write(body)

    def _serve(self):
        p = self.path
        time.sleep(0.01)                                   # make overlaps observable
        if p == '/robots.txt':
            return self._send(self.robots.encode(), 'text/plain')
        if p == '/wykaz/zmiany':                          # long, like the real one (~2 MB)
            return self._send(b"<table><tr><th>d</th></tr><tr><td>2026-11-20</td><td>x</td></tr>"
                              + b"<tr><td>older</td><td>y</td></tr>" * 200 + b"</table>")
        if p == '/wykaz/bazapi':
            return self._send(self.list_page.encode())
        if p.startswith('/stacja/'):
            if Site.on_page:
                Site.on_page()
            n = p.rsplit('/', 1)[1]
            return self._send(f"<img class='wykaz-logotyp' src='/logo/{n}.png'>".encode(), etag=f'"p{n}"')
        if p.startswith('/logo/'):
            n = p.rsplit('/', 1)[1].split('.')[0]
            # logo 3 like radiopolska.pl thumbnails: no Content-Type at all
            return self._send(LOGOS[n], None if n == '3' else 'image/png', etag=f'"l{n}"')
        if p == '/notimage':
            return self._send(b'<html>hi</html>', None)
        if p == '/redir':
            self.send_response(302)
            self.send_header('Location', '/logo/1.png')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        self.send_response(404)
        self.send_header('Content-Length', '0')
        self.end_headers()


class Pipeline(unittest.TestCase):
    def setUp(self):
        Site.log.clear()
        Site.peak[0] = 0
        Site.robots, Site.list_page, Site.on_page = "User-agent: *\nDisallow: /private/\n", LIST, None
        self.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Site)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.httpd.server_address[1]}'
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)                           # output.csv / last_change.txt are cwd-relative
        for f, t in (('fm.db', 'fm_station_logo'), ('dab.db', 'dab_station_logo')):
            c = sqlite3.connect(f)
            c.execute(SCHEMA.format(t=t))
            c.commit()
            c.close()
        self.cfg = {'paths': {'db_fm': 'fm.db', 'db_dab': 'dab.db', 'db_ocr': 'ocr.db', 'logo_dir': 'logos',
                              'image': os.path.join(self.tmp.name, 'work', 'x.img')},
                    # page limit below the changes page size: the signature must still work (regression)
                    'fetch': {'min_interval': GAP, 'timeout': 5, 'budget': 200, 'public_only': False,
                              'max_bytes': {'page': 4000, 'list': 1000000, 'image': 1000000}}}
        self.patch = mock.patch.object(fetcher, 'HARD_MIN_INTERVAL', 0.01)
        self.patch.start()
        self.patch_check = mock.patch.object(ford_db, 'LOGO_URL_CHECK', None)   # the fake site is on 127.0.0.1
        self.patch_check.start()

    def tearDown(self):
        self.patch_check.stop()
        self.patch.stop()
        for f in list(sources._HTTP.values()):
            f.close()
        sources._HTTP.clear()
        os.chdir(self.cwd)
        self.tmp.cleanup()
        self.httpd.shutdown()
        self.httpd.server_close()

    def source(self):
        cls = sources.available()['radiopolska']
        src = cls({'base_url': self.base})
        src.fetch_policy = FetchPolicy(min_interval=GAP)   # the real plugin asks for 1 s
        return sources.bind(src, self.cfg)

    def hits(self, prefix):
        return [p for p, _ in Site.log if p.startswith(prefix)]

    def run_update(self, progress=None):
        return ford_db.run_update(self.cfg, self.source(), True, True, progress or ford_db.UpdateProgress())

    def test_logo_urls_on_lan_are_skipped(self):
        """With the real check (public hosts only) a logo URL on 127.0.0.1 is never fetched."""
        with mock.patch.object(ford_db, 'LOGO_URL_CHECK', fetcher.public_http_url):
            st = self.run_update()
        self.assertIsNone(st['error'], st)
        self.assertGreaterEqual(st['added'], 4, 'the station data itself still comes in')
        self.assertEqual(self.hits('/logo/'), [])
        self.assertEqual(st['downloaded'], 0)
        self.assertGreaterEqual(st['logos']['failed'], 1)
        self.assertFalse(fetcher.public_http_url('http://192.168.1.1/x.png'))
        self.assertFalse(fetcher.public_http_url('file:///etc/passwd'))

    def test_polite_full_update_then_cached(self):
        st = self.run_update()
        self.assertIsNone(st['error'], st)
        self.assertGreaterEqual(st['added'], 4)
        self.assertEqual(st['downloaded'], len(os.listdir('logos/E2')))
        self.assertEqual(st['logos']['failed'], 0, 'an image without Content-Type is recognised by its bytes')
        # each detail page and each logo exactly once (3D04 shares page 1 and its logo)
        self.assertEqual(sorted(self.hits('/stacja/')), ['/stacja/1', '/stacja/2', '/stacja/3'])
        self.assertEqual(sorted(self.hits('/logo/')), ['/logo/1.png', '/logo/2.png', '/logo/3.png'])
        self.assertEqual(len(self.hits('/robots.txt')), 1)
        self.assertEqual(Site.peak[0], 1, 'never two requests at once')
        starts = [t for _, t in Site.log]
        self.assertGreaterEqual(min(b - a for a, b in zip(starts, starts[1:])), GAP - 0.03)  # server-side clock: scheduling jitter
        with open('last_change.txt', encoding='utf-8') as f:
            self.assertIn('2026-11-20', f.read())
        # second update: same signature -> no list download; logos present -> no downloads
        Site.log.clear()
        st2 = self.run_update()
        self.assertIsNone(st2['error'], st2)
        self.assertEqual((st2['added'], st2['downloaded']), (0, 0))
        self.assertEqual(self.hits('/wykaz/bazapi') + self.hits('/logo/'), [])

    def test_cancel_keeps_signature_unsaved(self):
        prog = ford_db.UpdateProgress()
        Site.on_page = prog.cancel.set                    # user presses Stop while pages load
        st = self.run_update(prog)
        self.assertTrue(st['cancelled'])
        self.assertFalse(os.path.exists('last_change.txt'), 'a cancelled run is redone next time')
        self.assertEqual(len(self.hits('/stacja/')), 1)
        self.assertEqual(self.hits('/logo/'), [])

    def test_robots_disallow_stops_with_message(self):
        Site.robots = "User-agent: *\nDisallow: /wykaz/\n"
        st = self.run_update()
        self.assertEqual(st['error']['key'], 'err.source_robots')
        self.assertEqual(self.hits('/wykaz/'), [])

    def test_layout_change_is_reported(self):
        Site.list_page = "<p>new layout</p>"
        st = self.run_update()
        self.assertEqual(st['error']['key'], 'err.source_layout')

    def test_budget_stops_update(self):
        self.cfg['fetch']['budget'] = 3
        st = self.run_update()
        self.assertEqual(st['error'], {'key': 'err.source_budget', 'fmt': {'count': '3'}})
        self.assertLessEqual(len(Site.log), 3)

    def test_background_run_and_status(self):
        prog = ford_db.PROGRESS
        self.assertTrue(ford_db.start_update(self.cfg, self.source(), True, True))
        self.assertFalse(ford_db.start_update(self.cfg, self.source(), True, True), 'one at a time')
        for _ in range(200):
            if not prog.snapshot()['running']:
                break
            time.sleep(0.05)
        snap = prog.snapshot()
        self.assertFalse(snap['running'])
        self.assertIsNone(snap['error'])
        self.assertGreater(snap['result']['downloaded'], 0)


class RemoteLogoUrl(unittest.TestCase):
    def test_public_only(self):
        import server
        for url in ('http://127.0.0.1:8000/api/session', 'http://localhost/', 'http://192.168.1.1/',
                    'http://10.0.0.1/x.png', 'http://[::1]/', 'http://169.254.169.254/latest/',
                    'file:///etc/passwd', 'ftp://example.com/x.png', 'not a url'):
            with self.subTest(url=url):
                self.assertFalse(server.public_http_url(url))
        self.assertTrue(server.public_http_url('http://93.184.216.34/logo.png'))

    def test_untyped_answer_must_look_like_an_image(self):
        httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Site)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{httpd.server_address[1]}'
        try:
            with fetcher.Fetcher({'min_interval': 0.5, 'public_only': False}) as f:
                self.assertEqual(f.get(base + '/logo/3.png', kind='image').content, LOGOS['3'])
                with self.assertRaises(fetcher.BadContentType):
                    f.get(base + '/notimage', kind='image')
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_redirect_target_checked(self):
        httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Site)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{httpd.server_address[1]}'
        try:
            with fetcher.Fetcher({'min_interval': 0.5, 'public_only': False}) as f:
                with self.assertRaises(fetcher.FetchError):
                    f.get(base + '/redir', kind='image', url_check=lambda u: u.endswith('/redir'))
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == '__main__':
    unittest.main()
