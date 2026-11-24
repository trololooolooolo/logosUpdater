"""fetcher.Fetcher against a local fake server: per-host limits, retries, robots.txt, cache, budget.

Run: python3 -m unittest discover -s tests
The hard minimum interval is lowered for the tests (fetcher.HARD_MIN_INTERVAL) to keep them fast.
"""
import os
import platform
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fetcher  # noqa: E402
from fetcher import (BadContentType, BudgetExceeded, FetchError, Fetcher, FetchPolicy,  # noqa: E402
                     RobotsDisallowed, TooLarge)


class FakeSite:
    """Routes: path -> callable(handler) -> (status, headers dict, body bytes). Records every hit."""

    def __init__(self):
        self.routes = {}
        self.hits = []          # (path, monotonic start, headers dict)
        self.lock = threading.Lock()
        self.in_flight = 0
        self.max_in_flight = 0

    def reset(self):
        self.routes = {}
        self.hits = []
        self.in_flight = self.max_in_flight = 0

    def paths(self):
        return [h[0] for h in self.hits]

    def times(self, path):
        return [h[1] for h in self.hits if h[0] == path]


SITE = FakeSite()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split('?')[0]
        with SITE.lock:
            SITE.hits.append((path, time.monotonic(), dict(self.headers.items())))
            SITE.in_flight += 1
            SITE.max_in_flight = max(SITE.max_in_flight, SITE.in_flight)
        try:
            route = SITE.routes.get(path)
            status, headers, body = route(self) if route else (404, {'Content-Type': 'text/plain'}, b'nf')
            self.send_response(status)
            no_length = headers.pop('X-No-Length', None)
            for k, v in headers.items():
                self.send_header(k, v)
            if not no_length and 'Content-Length' not in headers and body is not None and status != 304:
                self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            if body and status != 304:
                self.wfile.write(body)
        finally:
            with SITE.lock:
                SITE.in_flight -= 1

    def log_message(self, *a):
        pass


def ok(body=b'hello', ctype='text/html; charset=utf-8', **extra):
    return lambda h: (200, dict({'Content-Type': ctype}, **extra), body)


class FetcherTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        SITE.reset()
        self._hard = fetcher.HARD_MIN_INTERVAL
        fetcher.HARD_MIN_INTERVAL = 0.01
        self.tmp = tempfile.mkdtemp()
        self.fetchers = []

    def tearDown(self):
        for f in self.fetchers:
            f.close()
        fetcher.HARD_MIN_INTERVAL = self._hard
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self, cache=False, **settings):
        s = {'min_interval': 0.02, 'timeout': 5, 'public_only': False}   # fake site on 127.0.0.1
        s.update(settings)
        f = Fetcher(s, cache_dir=os.path.join(self.tmp, 'cache') if cache else None)
        self.fetchers.append(f)
        return f

    # (a)
    def test_parallelism_one_per_host(self):
        def slow(h):
            time.sleep(0.1)
            return 200, {'Content-Type': 'text/plain'}, b'x'
        SITE.routes['/slow'] = slow
        f = self.make(respect_robots=False)
        errors = []

        def worker():
            try:
                for _ in range(2):
                    self.assertEqual(f.get(self.base + '/slow').status, 200)
            except Exception as e:      # pragma: no cover - reported below
                errors.append(e)
        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(SITE.times('/slow')), 8)
        self.assertEqual(SITE.max_in_flight, 1)
        self.assertEqual(f.stats['requests'], 8)

    # (b)
    def test_min_interval_between_starts(self):
        SITE.routes['/p'] = ok()
        f = self.make(min_interval=0.3, respect_robots=False)
        for _ in range(3):
            f.get(self.base + '/p')
        t = SITE.times('/p')
        gaps = [b - a for a, b in zip(t, t[1:])]
        self.assertEqual(len(gaps), 2)
        for g in gaps:
            self.assertGreaterEqual(g, 0.28)

    # (c)
    def test_policy_cannot_loosen_config(self):
        f = self.make(max_parallel=2, min_interval=0.2)
        with self.assertLogs('fetcher', 'WARNING') as cm:
            v = f.for_policy(FetchPolicy(max_parallel=8, min_interval=0.05))
        self.assertEqual(v.limits, {'max_parallel': 2, 'min_interval': 0.2})
        self.assertEqual(len(cm.output), 2)
        # stricter policy is applied as-is, shares stats with the parent
        strict = f.for_policy(FetchPolicy(max_parallel=1, min_interval=0.5))
        self.assertEqual(strict.limits, {'max_parallel': 1, 'min_interval': 0.5})
        self.assertIs(strict.stats, f.stats)

    def test_config_clamped_to_hard_minimum(self):
        fetcher.HARD_MIN_INTERVAL = 0.5
        with self.assertLogs('fetcher', 'WARNING'):
            f = Fetcher({'min_interval': 0.1, 'max_parallel': 50})
        self.fetchers.append(f)
        self.assertEqual(f.limits, {'max_parallel': fetcher.HARD_MAX_PARALLEL, 'min_interval': 0.5})
        f2 = Fetcher()      # defaults: 1 parallel, 1 s
        self.fetchers.append(f2)
        self.assertEqual(f2.limits, {'max_parallel': 1, 'min_interval': 1.0})

    def test_policy_network_false(self):
        f = self.make().for_policy(FetchPolicy(network=False))
        with self.assertRaises(FetchError):
            f.get(self.base + '/p')
        self.assertEqual(SITE.hits, [])

    # (d)
    def test_429_retry_after_delays_all_threads(self):
        state = {'n': 0}
        got_429 = threading.Event()

        def limited(h):
            state['n'] += 1
            if state['n'] == 1:
                got_429.set()
                return 429, {'Retry-After': '1', 'Content-Type': 'text/plain'}, b'slow down'
            return 200, {'Content-Type': 'text/plain'}, b'ok'
        SITE.routes['/limited'] = limited
        SITE.routes['/other'] = ok()
        f = self.make(respect_robots=False)
        res = {}
        t = threading.Thread(target=lambda: res.setdefault('a', f.get(self.base + '/limited')))
        t.start()
        got_429.wait(5)
        time.sleep(0.05)
        other = f.get(self.base + '/other')
        t.join()
        self.assertEqual(res['a'].status, 200)
        self.assertEqual(res['a'].content, b'ok')
        self.assertEqual(other.status, 200)
        first, second = SITE.times('/limited')
        self.assertGreaterEqual(second - first, 0.95)
        self.assertGreaterEqual(SITE.times('/other')[0] - first, 0.95)
        self.assertEqual(f.stats['retries'], 1)

    def test_retries_stop_after_three(self):
        SITE.routes['/busy'] = lambda h: (503, {'Retry-After': '0'}, b'')
        f = self.make(respect_robots=False)
        orig = fetcher.BACKOFF_BASE
        fetcher.BACKOFF_BASE = 0.01
        try:
            r = f.get(self.base + '/busy')
        finally:
            fetcher.BACKOFF_BASE = orig
        self.assertEqual(r.status, 503)
        self.assertEqual(len(SITE.times('/busy')), 4)
        self.assertEqual(f.stats['retries'], 3)

    def test_other_errors_not_retried(self):
        SITE.routes['/err'] = lambda h: (500, {}, b'boom')
        f = self.make(respect_robots=False)
        self.assertEqual(f.get(self.base + '/err').status, 500)
        self.assertEqual(len(SITE.times('/err')), 1)

    def test_retry_after_http_date(self):
        from email.utils import formatdate
        self.assertAlmostEqual(fetcher._retry_after(formatdate(time.time() + 30, usegmt=True)), 30, delta=2)
        self.assertEqual(fetcher._retry_after('7'), 7.0)
        self.assertIsNone(fetcher._retry_after('soon'))
        self.assertIsNone(fetcher._retry_after(None))

    # (e)
    def test_robots_disallow(self):
        SITE.routes['/robots.txt'] = ok(b'User-agent: *\nDisallow: /wykaz/\n', 'text/plain')
        SITE.routes['/wykaz/bazapi'] = ok()
        SITE.routes['/public'] = ok()
        f = self.make()
        with self.assertRaises(RobotsDisallowed) as cm:
            f.get(self.base + '/wykaz/bazapi')
        self.assertEqual(cm.exception.url, self.base + '/wykaz/bazapi')
        self.assertEqual(f.get(self.base + '/public').status, 200)
        self.assertNotIn('/wykaz/bazapi', SITE.paths())
        self.assertEqual(SITE.paths().count('/robots.txt'), 1)
        self.assertEqual(f.stats['robots_blocked'], 1)
        self.assertEqual(f.blocked, [self.base + '/wykaz/bazapi'])
        f.new_run()     # robots.txt is fetched again in a new run
        f.get(self.base + '/public')
        self.assertEqual(SITE.paths().count('/robots.txt'), 2)

    def test_robots_agent_specific_and_missing(self):
        SITE.routes['/robots.txt'] = ok(b'User-agent: LogosUpdater\nDisallow: /\n\nUser-agent: *\nAllow: /\n',
                                        'text/plain')
        f = self.make()
        with self.assertRaises(RobotsDisallowed):
            f.get(self.base + '/x')
        SITE.reset()    # no robots.txt (404) = allow all
        SITE.routes['/x'] = ok()
        f.new_run()
        self.assertEqual(f.get(self.base + '/x').status, 200)

    def test_robots_5xx_allows_with_warning(self):
        SITE.routes['/robots.txt'] = lambda h: (500, {}, b'')
        SITE.routes['/x'] = ok()
        f = self.make()
        with self.assertLogs('fetcher', 'WARNING'):
            self.assertEqual(f.get(self.base + '/x').status, 200)

    # (f)
    def test_fresh_cache_skips_the_site(self):
        # like radiopolska.pl station pages: no ETag, Last-Modified = now, ignores If-Modified-Since
        stamp = 'Sat, 21 Nov 2026 09:08:30 GMT'
        SITE.routes['/station/9'] = lambda h: (200, {'Content-Type': 'text/html', 'Last-Modified': stamp}, b'<p>x</p>')
        SITE.routes['/station/8'] = lambda h: (200, {'Content-Type': 'text/html'}, b'<p>no validators</p>')
        f = self.make(cache=True, respect_robots=False)          # default: pages fresh for 7 days
        for path in ('/station/9', '/station/8'):
            self.assertFalse(f.get(self.base + path).from_cache)
            n = len(SITE.hits)
            r = f.get(self.base + path)
            self.assertTrue(r.from_cache)
            self.assertEqual(len(SITE.hits), n, 'no request while the cached page is fresh')
        self.assertEqual(f.stats['fresh'], 2)
        f.get(self.base + '/station/9', kind='list')               # lists are never cached
        self.assertTrue(SITE.hits[-1][0].endswith('/station/9'))
        old = self.make(cache=True, respect_robots=False, max_age_days={'page': 0})
        n = len(SITE.hits)
        old.get(self.base + '/station/9')
        self.assertEqual(len(SITE.hits), n + 1, 'max_age_days 0 = always ask the site')

    def test_conditional_cache_304(self):
        def page(h):
            if h.headers.get('If-None-Match') == '"v1"':
                return 304, {'ETag': '"v1"'}, b''
            return 200, {'Content-Type': 'text/html; charset=utf-8', 'ETag': '"v1"'}, 'zażółć'.encode()
        SITE.routes['/station/1'] = page
        SITE.routes['/station/2'] = ok()
        f = self.make(cache=True, respect_robots=False, max_age_days={'page': 0})   # always revalidate
        f.new_run(budget=2)
        r1 = f.get(self.base + '/station/1')
        self.assertFalse(r1.from_cache)
        self.assertEqual(r1.text, 'zażółć')
        for _ in range(3):      # 304s don't use the budget (2)
            r = f.get(self.base + '/station/1')
            self.assertTrue(r.from_cache)
            self.assertEqual(r.status, 200)
            self.assertEqual(r.content, r1.content)
            self.assertEqual(r.headers['Content-Type'], 'text/html; charset=utf-8')
        self.assertEqual(SITE.hits[1][2].get('If-None-Match'), '"v1"')
        self.assertEqual(f.stats['not_modified'], 3)
        self.assertEqual(f.stats['from_cache'], 3)
        f.get(self.base + '/station/2')     # budget unit 2
        with self.assertRaises(BudgetExceeded):
            f.get(self.base + '/station/2')
        # a fresh Fetcher reuses the disk cache
        f2 = self.make(cache=True, respect_robots=False)
        self.assertTrue(f2.get(self.base + '/station/1').from_cache)
        # conditional=False bypasses the cache
        self.assertFalse(f2.get(self.base + '/station/1', conditional=False).from_cache)

    def test_list_kind_not_cached(self):
        SITE.routes['/list'] = ok(ETag='"l"')
        f = self.make(cache=True, respect_robots=False)
        f.get(self.base + '/list', kind='list')
        f.get(self.base + '/list', kind='list')
        self.assertIsNone(SITE.hits[1][2].get('If-None-Match'))

    # (g)
    def test_budget_exceeded(self):
        SITE.routes['/p'] = ok()
        f = self.make(budget=3, respect_robots=False)
        for _ in range(3):
            f.get(self.base + '/p')
        with self.assertRaises(BudgetExceeded):
            f.get(self.base + '/p')
        self.assertEqual(len(SITE.times('/p')), 3)
        f.new_run(budget=1)
        f.get(self.base + '/p')
        self.assertEqual(f.stats['requests'], 1)

    # (h)
    def test_too_large(self):
        SITE.routes['/big'] = ok(b'x' * 5000)
        f = self.make(respect_robots=False, max_bytes={'page': 1000})
        with self.assertRaises(TooLarge):
            f.get(self.base + '/big')
        self.assertEqual(f.get(self.base + '/big', max_bytes=10000).status, 200)

    def test_too_large_without_content_length(self):
        # HTTP/1.0 body until close, no Content-Length: the stream is cut while reading
        SITE.routes['/stream'] = lambda h: (200, {'Content-Type': 'text/plain', 'X-No-Length': '1'}, b'y' * 300000)
        f = self.make(respect_robots=False)
        with self.assertRaises(TooLarge):
            f.get(self.base + '/stream', max_bytes=100000)

    # (i)
    def test_image_requires_image_type(self):
        SITE.routes['/logo.png'] = ok(b'<html>', 'text/html')
        SITE.routes['/real.png'] = ok(b'\x89PNG....', 'image/png')
        f = self.make(respect_robots=False)
        with self.assertRaises(BadContentType):
            f.get(self.base + '/logo.png', kind='image')
        self.assertEqual(f.get(self.base + '/real.png', kind='image').content, b'\x89PNG....')

    # (j)
    def test_only_http_schemes(self):
        f = self.make()
        for url in ('file:///etc/passwd', 'ftp://127.0.0.1/x', 'gopher://x/', 'http:///nohost'):
            with self.assertRaises(FetchError):
                f.get(url)
        self.assertEqual(SITE.hits, [])

    def test_redirect_to_other_scheme_refused(self):
        SITE.routes['/r'] = lambda h: (302, {'Location': 'file:///etc/passwd'}, b'')
        SITE.routes['/r2'] = lambda h: (301, {'Location': '/final'}, b'')
        SITE.routes['/final'] = ok(b'done')
        f = self.make(respect_robots=False)
        with self.assertRaises(FetchError):
            f.get(self.base + '/r')
        r = f.get(self.base + '/r2')
        self.assertEqual((r.url, r.content), (self.base + '/final', b'done'))

    # (k)
    def test_user_agent(self):
        SITE.routes['/p'] = ok()
        f = self.make(respect_robots=False)
        f.get(self.base + '/p')
        hdrs = SITE.hits[0][2]
        ua = hdrs['User-Agent']
        self.assertTrue(ua.startswith('LogosUpdater/'))
        self.assertEqual(ua, fetcher.default_user_agent())
        self.assertEqual(ua, f"LogosUpdater/{fetcher.VERSION} (+{fetcher.PROJECT_URL})")
        for secret in filter(None, {os.environ.get('USER'), os.environ.get('LOGNAME'),
                                    os.path.expanduser('~'), platform.node(),
                                    socket.gethostname()}):
            self.assertNotIn(secret, ua)
        allowed = {'host', 'user-agent', 'accept-encoding', 'accept', 'connection'}
        self.assertEqual({k.lower() for k in hdrs} - allowed, set())

    def test_defaults_match_config(self):
        try:
            import config
        except SystemExit:      # PyYAML missing
            self.skipTest('PyYAML not installed')
        self.assertEqual(fetcher.DEFAULT_SETTINGS, config.DEFAULTS['fetch'])

    def test_text_charset(self):
        r = fetcher.Response('u', 200, 'ą'.encode('iso-8859-2'), fetcher.Headers({'Content-Type': 'text/html; charset=ISO-8859-2'}), False)
        self.assertEqual(r.text, 'ą')
        r = fetcher.Response('u', 200, b'\xff', fetcher.Headers({}), False)
        self.assertEqual(r.text, '�')



class PublicOnly(unittest.TestCase):
    def test_default_refuses_local_and_lan_hosts(self):
        with Fetcher({}) as f:
            for url in ('http://127.0.0.1:9/x', 'http://localhost/', 'http://192.168.1.1/', 'http://[::1]/'):
                with self.subTest(url=url):
                    with self.assertRaises(FetchError):
                        f.get(url)
            self.assertEqual(f.stats['requests'], 0)

    def test_robots_redirect_to_lan_is_not_followed(self):
        """robots.txt of a public host redirecting to the LAN: the hop is refused, nothing is sent there."""
        from unittest import mock
        import fetcher as fm
        sent = []
        real = Fetcher._request

        def fake_request(self, url, extra, limit, timeout, kind, robots=True):
            sent.append(url)
            if url.endswith('/robots.txt'):
                return 302, fm.Headers({'location': 'http://192.168.1.1/admin/reboot'}), b''
            return 200, fm.Headers({'content-type': 'text/html'}), b'ok'
        with mock.patch.object(Fetcher, '_request', fake_request), \
                mock.patch.object(fm, 'public_http_url', lambda u: '192.168.' not in u):
            with Fetcher({'min_interval': 0.5}) as f:
                self.assertEqual(f.get('http://93.184.216.34/page').status, 200)
        self.assertFalse([u for u in sent if '192.168.' in u], sent)
        self.assertIs(Fetcher._request, real)


if __name__ == '__main__':
    unittest.main()
