"""Polite shared HTTP client for source plugins and logo downloads.

One exit point to the network with limits a plugin cannot bypass by accident:

* per-host limits: at most ``max_parallel`` requests in flight and at least ``min_interval`` seconds
  between request *starts* to one host (thread-safe, shared by every view of one Fetcher);
* honest User-Agent (``default_user_agent()``), no user data;
* timeout on every request, response size limit per kind, ``kind='image'`` requires ``image/*``;
* retries only for 429/503 (max 3, exponential backoff, ``Retry-After`` honoured for all threads);
* robots.txt (urllib.robotparser), fetched through the same limits once per host per run;
* request budget per run (304 answers don't count);
* conditional disk cache (ETag / Last-Modified) for ``page`` and ``image`` answers.

Redirects are followed manually (max 5 hops) so every hop goes through the scheme check, robots.txt
and the limits of its own host.

``requests`` is imported lazily, so ``import fetcher`` works without it.
"""
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

logger = logging.getLogger(__name__)

VERSION = '0.9.0'
# Sent in the User-Agent of every request: site operators can see who is fetching and how to reach us.
PROJECT_URL = 'https://github.com/trololooolooolo/logosUpdater'
ROBOTS_AGENT = 'LogosUpdater'      # token matched against robots.txt User-agent lines

HARD_MIN_INTERVAL = 0.5            # seconds; nothing (config, plugin) may go below this
HARD_MAX_PARALLEL = 4              # requests in flight per host; nothing may go above this (see config.py 'fetch' for why)

# Content types that say nothing about the file: an 'image' answer with one of these (or none) is
# accepted when its first bytes are an image signature (see sniff_image).
GENERIC_TYPES = ('', 'application/octet-stream', 'binary/octet-stream', 'application/x-download')
_IMAGE_MAGIC = (b'\x89PNG\r\n\x1a\n', b'\xff\xd8\xff', b'GIF87a', b'GIF89a', b'BM', b'\x00\x00\x01\x00')


def sniff_image(data):
    """True when `data` starts like a PNG / JPEG / GIF / BMP / ICO / WebP file."""
    head = bytes(data[:16])
    return head.startswith(_IMAGE_MAGIC) or (head[:4] == b'RIFF' and head[8:12] == b'WEBP')
MAX_RETRIES = 3                    # for 429 / 503 only
BACKOFF_BASE = 1.0                 # seconds; 1, 2, 4 when the server sends no Retry-After
MAX_RETRY_AFTER = 300              # longer Retry-After = give up on that host for this long
MAX_REDIRECTS = 5
ROBOTS_MAX_BYTES = 512000          # RFC 9309: parse at least 500 KiB
KINDS = ('page', 'list', 'image')
CACHED_KINDS = ('page', 'image')

# Mirror of config.DEFAULTS['fetch'] (config is not imported: it needs PyYAML). A test keeps them equal.
DEFAULT_SETTINGS = {
    'max_parallel': 1,
    'min_interval': 1.0,
    'timeout': 20,
    'budget': 2000,
    'respect_robots': True,
    'public_only': True,       # refuse loopback/LAN/link-local hosts (every redirect hop included)
    'max_bytes': {'page': 2000000, 'list': 10000000, 'image': 2000000},
    # Freshness: a cached answer younger than this is used without asking the site at all.
    # Needed for sites that ignore conditional requests (radiopolska.pl station pages send
    # Last-Modified = now and max-age=0, so 304 never happens). 0 = always revalidate.
    'max_age_days': {'page': 7, 'image': 0},
}


def public_http_url(url):
    """True for an http(s) URL whose host resolves only to public addresses.

    Use as ``url_check`` for URLs that come from data or from the browser (logo URLs in output.csv /
    plugin data, "Set this logo" from a URL), so they cannot reach this computer or the local network.
    Every resolved address must be global and not multicast.
    """
    import ipaddress
    import socket
    try:
        u = urlsplit(str(url))
        if u.scheme not in ('http', 'https') or not u.hostname:
            return False
        infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == 'https' else 80),
                                   proto=socket.IPPROTO_TCP)
    except (ValueError, OSError):
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0].split('%', 1)[0])
        except ValueError:
            return False
        if not ip.is_global or ip.is_multicast:
            return False
    return bool(infos)


def default_user_agent():
    return f"LogosUpdater/{VERSION} (+{PROJECT_URL})"


# --- public types ----------------------------------------------------------------------------------

@dataclass(frozen=True)
class FetchPolicy:
    """A plugin's declaration. It may only make the limits stricter than the config."""
    network: bool = True
    max_parallel: int = 1
    min_interval: float = 1.0   # seconds between request starts to one host


class FetchError(Exception):
    """Base for every fetch failure; ``url`` = the URL that failed."""

    def __init__(self, message, url=''):
        super().__init__(message)
        self.url = url


class RobotsDisallowed(FetchError):
    """robots.txt of the host disallows the URL for LogosUpdater; no request was sent."""


class BudgetExceeded(FetchError):
    """The request budget of the current run is used up; no request was sent. ``count`` = the budget."""

    def __init__(self, msg, url=None, count=None):
        super().__init__(msg, url)
        self.count = count


class TooLarge(FetchError):
    """The answer is bigger than ``max_bytes``; the body was dropped."""


class BadContentType(FetchError):
    """``kind='image'`` and the answer is not ``image/*``."""


class Headers(dict):
    """Plain dict with case-insensitive keys (stored lower-case)."""

    def __init__(self, data=None):
        super().__init__()
        for k, v in (data or {}).items():
            self[k] = v

    def __setitem__(self, k, v):
        super().__setitem__(k.lower(), v)

    def __getitem__(self, k):
        return super().__getitem__(k.lower())

    def __contains__(self, k):
        return isinstance(k, str) and super().__contains__(k.lower())

    def get(self, k, default=None):
        return super().get(k.lower(), default)


@dataclass
class Response:
    """Answer of ``Fetcher.get``. A 304 served from the disk cache has ``status=200, from_cache=True``."""
    url: str
    status: int
    content: bytes
    headers: dict = field(default_factory=Headers)
    from_cache: bool = False

    @property
    def ok(self):
        return 200 <= self.status < 300

    @property
    def text(self):
        m = re.search(r'charset\s*=\s*["\']?([^;"\'\s]+)', self.headers.get('content-type', '') or '', re.I)
        if m:
            try:
                return self.content.decode(m.group(1))
            except (LookupError, UnicodeDecodeError):
                pass
        return self.content.decode('utf-8', 'replace')


# --- internals -------------------------------------------------------------------------------------

class _Host:
    """Per-host gate: in-flight counter + earliest next start, shared by all threads and views."""

    def __init__(self):
        self.cond = threading.Condition()
        self.in_flight = 0
        self.next_start = 0.0       # time.monotonic()
        self.blocked_until = 0.0    # Retry-After longer than MAX_RETRY_AFTER

    def acquire(self, parallel, interval):
        with self.cond:
            while True:
                now = time.monotonic()
                if self.in_flight < parallel and now >= self.next_start:
                    self.in_flight += 1
                    self.next_start = max(self.next_start, now + interval)
                    return
                wait = self.next_start - now if self.in_flight < parallel else None
                self.cond.wait(wait if wait is None or wait > 0 else 0.001)

    def release(self):
        with self.cond:
            self.in_flight -= 1
            self.cond.notify_all()

    def delay(self, seconds):
        """No request to this host starts before now + seconds (all threads)."""
        with self.cond:
            self.next_start = max(self.next_start, time.monotonic() + seconds)
            self.cond.notify_all()


def _new_stats():
    return {'requests': 0, 'not_modified': 0, 'from_cache': 0, 'fresh': 0, 'robots_blocked': 0, 'retries': 0, 'bytes': 0}


class _Shared:
    """State shared by a Fetcher and all its ``for_policy`` views."""

    def __init__(self, settings, cache_dir, user_agent):
        self.settings = settings
        self.cache_dir = cache_dir
        self.user_agent = user_agent
        self.lock = threading.Lock()
        self.hosts = {}
        self.session = None
        self.robots = {}            # origin -> RobotFileParser | None (None = allow all)
        self.robots_locks = {}
        self.stats = _new_stats()
        self.blocked = []           # robots-blocked URLs of the current run
        self.used = 0               # budget units used in the current run
        self.budget = settings['budget']

    def host(self, key):
        with self.lock:
            h = self.hosts.get(key)
            if h is None:
                h = self.hosts[key] = _Host()
            return h

    def count(self, key, n=1):
        with self.lock:
            self.stats[key] += n


def _num(value, default, name, cast=float):
    try:
        return cast(value)
    except (TypeError, ValueError):
        logger.warning("fetch.%s=%r is not a number; using %r", name, value, default)
        return default


def _normalize_settings(settings):
    """Merge with DEFAULT_SETTINGS and clamp to the hard limits (with a warning)."""
    s = dict(DEFAULT_SETTINGS)
    s['max_bytes'] = dict(DEFAULT_SETTINGS['max_bytes'])
    s['max_age_days'] = dict(DEFAULT_SETTINGS['max_age_days'])
    for k, v in (settings or {}).items():
        if v is None:
            continue
        if k in ('max_bytes', 'max_age_days') and isinstance(v, dict):
            s[k].update({kk: vv for kk, vv in v.items() if vv is not None})
        else:
            s[k] = v
    par = _num(s['max_parallel'], DEFAULT_SETTINGS['max_parallel'], 'max_parallel', int)
    if par < 1 or par > HARD_MAX_PARALLEL:
        clamped = min(max(par, 1), HARD_MAX_PARALLEL)
        logger.warning("fetch.max_parallel=%s is outside 1..%s; using %s", par, HARD_MAX_PARALLEL, clamped)
        par = clamped
    s['max_parallel'] = par
    iv = _num(s['min_interval'], DEFAULT_SETTINGS['min_interval'], 'min_interval')
    if iv < HARD_MIN_INTERVAL:
        logger.warning("fetch.min_interval=%s is below the hard minimum %s s; using %s",
                       iv, HARD_MIN_INTERVAL, HARD_MIN_INTERVAL)
        iv = HARD_MIN_INTERVAL
    s['min_interval'] = iv
    to = _num(s['timeout'], DEFAULT_SETTINGS['timeout'], 'timeout')
    if to <= 0:
        logger.warning("fetch.timeout=%s must be > 0; using %s", to, DEFAULT_SETTINGS['timeout'])
        to = DEFAULT_SETTINGS['timeout']
    s['timeout'] = to
    s['budget'] = _num(s['budget'], DEFAULT_SETTINGS['budget'], 'budget', int)
    s['respect_robots'] = bool(s['respect_robots'])
    s['public_only'] = bool(s.get('public_only', True))
    for kind in KINDS:
        s['max_bytes'][kind] = _num(s['max_bytes'].get(kind), DEFAULT_SETTINGS['max_bytes'][kind],
                                    f'max_bytes.{kind}', int)
    for kind in CACHED_KINDS:
        s['max_age_days'][kind] = max(0.0, _num(s['max_age_days'].get(kind, 0),
                                                DEFAULT_SETTINGS['max_age_days'].get(kind, 0),
                                                f'max_age_days.{kind}'))
    return s


def _retry_after(value):
    """Seconds from a Retry-After header (delta-seconds or HTTP date); None if absent/invalid."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:                       # HTTP dates are GMT
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, dt.timestamp() - time.time())


def _atomic_write(path, data):
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix='.tmp-')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --- the client ------------------------------------------------------------------------------------

class Fetcher:
    """Shared polite HTTP client. ``settings`` = ``cfg['fetch']``; ``cache_dir`` None = no disk cache.

    Thread-safe. ``for_policy`` returns a view with stricter limits that shares host state, budget,
    stats, robots cache and disk cache with this one. Call ``close()`` (or use ``with``) when done.
    """

    def __init__(self, settings=None, cache_dir=None, user_agent=None):
        s = _normalize_settings(settings)
        self._shared = _Shared(s, cache_dir, user_agent or default_user_agent())
        self._network = True
        self._parallel = s['max_parallel']
        self._interval = s['min_interval']
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)

    # -- views / run --

    def for_policy(self, policy):
        """View with effective limits = stricter of (config, policy)."""
        par = int(policy.max_parallel)
        if par > self._parallel:
            logger.warning("fetch policy asks for max_parallel=%s, above the configured %s; using %s",
                           par, self._parallel, self._parallel)
        par = max(1, min(par, self._parallel))
        iv = float(policy.min_interval)
        if iv < self._interval:
            logger.warning("fetch policy asks for min_interval=%s s, below the configured %s s; using %s",
                           iv, self._interval, self._interval)
        iv = max(iv, self._interval, HARD_MIN_INTERVAL)
        view = object.__new__(Fetcher)
        view._shared = self._shared
        view._network = self._network and bool(policy.network)
        view._parallel = par
        view._interval = iv
        return view

    @property
    def stats(self):
        return self._shared.stats

    @property
    def blocked(self):
        """URLs skipped because of robots.txt in the current run."""
        return self._shared.blocked

    @property
    def user_agent(self):
        return self._shared.user_agent

    @property
    def limits(self):
        return {'max_parallel': self._parallel, 'min_interval': self._interval}

    def new_run(self, budget=None):
        """Start a new update: reset stats, budget counter and the robots.txt cache."""
        sh = self._shared
        with sh.lock:
            sh.stats = _new_stats()
            sh.blocked = []
            sh.used = 0
            sh.budget = sh.settings['budget'] if budget is None else int(budget)
            sh.robots = {}
            sh.robots_locks = {}

    def close(self):
        sh = self._shared
        with sh.lock:
            sess, sh.session = sh.session, None
        if sess is not None:
            sess.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- main entry --

    def get(self, url, *, kind='page', max_bytes=None, conditional=True, timeout=None, url_check=None):
        """GET ``url`` politely. Non-2xx answers come back as a Response; failures raise FetchError.

        kind: 'page' | 'list' | 'image'. Only 200 answers of 'page'/'image' are cached; with a cached copy
        the request carries If-None-Match / If-Modified-Since and a 304 returns the cached body with
        ``status=200, from_cache=True``. ``conditional=False`` bypasses the disk cache entirely.
        ``url_check(url) -> bool`` is applied to the URL and to every redirect target (e.g. "public
        hosts only" for URLs that come from the browser); False raises FetchError before sending.
        """
        if kind not in KINDS:
            raise ValueError(f"unknown kind {kind!r}")
        if not self._network:
            raise FetchError("network access disabled by the source's fetch policy", url)
        sh = self._shared
        limit = sh.settings['max_bytes'][kind] if max_bytes is None else int(max_bytes)
        timeout = sh.settings['timeout'] if timeout is None else float(timeout)
        use_cache = bool(sh.cache_dir) and conditional and kind in CACHED_KINDS
        current = url
        max_age = sh.settings['max_age_days'].get(kind, 0) * 86400 if use_cache else 0
        for _ in range(MAX_REDIRECTS + 1):
            self._check_url(current)
            if url_check is not None and not url_check(current):
                raise FetchError("address not allowed", current)
            if max_age:
                fresh = self._cache_fresh(current, max_age)
                if fresh is not None:
                    meta, body = fresh
                    hdrs = Headers({'content-type': meta.get('content_type', '')})
                    self._check_type(kind, hdrs, current, body)
                    sh.count('fresh')
                    return Response(current, 200, body, hdrs, True)
            cached = self._cache_load(current) if use_cache else None
            extra = {}
            if cached:
                if cached.get('etag'):
                    extra['If-None-Match'] = cached['etag']
                if cached.get('last_modified'):
                    extra['If-Modified-Since'] = cached['last_modified']
            status, headers, body = self._request(current, extra, limit, timeout, kind)
            if status in (301, 302, 303, 307, 308) and headers.get('location'):
                current = urljoin(current, headers['location'])
                continue
            if status == 304 and cached:
                body = self._cache_body(current)
                if body is not None:
                    sh.count('from_cache')
                    self._cache_refresh(current, cached, headers)
                    hdrs = Headers(headers)
                    hdrs['content-type'] = cached.get('content_type', '')
                    self._check_type(kind, hdrs, current, body)
                    return Response(current, 200, body, hdrs, True)
            if status == 200 and use_cache:
                self._cache_store(current, headers, body, keep=bool(max_age))
            return Response(current, status, body, Headers(headers), False)
        raise FetchError(f"too many redirects (>{MAX_REDIRECTS})", url)

    # -- steps --

    def _check_url(self, url):
        """Every URL and every redirect hop (robots.txt too): http(s) only and, unless
        fetch.public_only is off (tests with a local fake site), public internet hosts only -
        URLs from source data or the browser must not reach this computer or the LAN."""
        parts = urlsplit(url)
        if parts.scheme.lower() not in ('http', 'https') or not parts.hostname:
            raise FetchError(f"only http(s) URLs are fetched: {url!r}", url)
        if self._shared.settings.get('public_only', True) and not public_http_url(url):
            raise FetchError(f"not a public internet address: {url!r}", url)

    @staticmethod
    def _host_key(url):
        p = urlsplit(url)
        return f"{p.scheme.lower()}://{p.netloc.lower()}"

    def _session(self):
        sh = self._shared
        with sh.lock:
            if sh.session is None:
                import requests
                sess = requests.Session()
                sess.headers['User-Agent'] = sh.user_agent
                sh.session = sess
            return sh.session

    def _take_budget(self, url):
        sh = self._shared
        with sh.lock:
            if sh.budget is not None and sh.budget > 0 and sh.used >= sh.budget:
                raise BudgetExceeded(f"request budget of {sh.budget} for this run is used up", url, count=sh.budget)
            sh.used += 1

    def _refund_budget(self):
        sh = self._shared
        with sh.lock:
            sh.used -= 1

    @staticmethod
    def _check_type(kind, headers, url, body=None):
        """kind='image' needs an image. Some sites (radiopolska.pl thumbnails) send images without a
        Content-Type or as application/octet-stream: then the file's signature decides (needs `body`;
        without it the check is deferred until the body is read)."""
        if kind != 'image':
            return
        ctype = (headers.get('content-type') or '').split(';')[0].strip().lower()
        if ctype.startswith('image/'):
            return
        if ctype not in GENERIC_TYPES:
            raise BadContentType(f"expected image/*, got {ctype}", url)
        if body is not None and not sniff_image(body):
            raise BadContentType(f"expected an image, got {ctype or 'no Content-Type'} that is not one", url)

    def _request(self, url, extra_headers, limit, timeout, kind, robots=True):
        """One logical request (with 429/503 retries). Returns (status, headers, body)."""
        sh = self._shared
        if robots and sh.settings['respect_robots']:
            self._check_robots(url, timeout)
        key = self._host_key(url)
        host = sh.host(key)
        attempt = 0
        while True:
            with host.cond:
                blocked = host.blocked_until - time.monotonic()
            if blocked > 0:
                raise FetchError(f"{key} asked to wait {blocked:.0f} s more (Retry-After)", url)
            self._take_budget(url)
            host.acquire(self._parallel, self._interval)
            try:
                status, headers, body = self._send(url, extra_headers, limit, timeout, kind)
                wait = None
                if status in (429, 503):
                    # Applied before release, so no other thread slips in meanwhile.
                    ra = _retry_after(headers.get('retry-after'))
                    if ra is not None and ra > MAX_RETRY_AFTER:
                        with host.cond:
                            host.blocked_until = time.monotonic() + ra
                        logger.warning("%s answered %s with Retry-After %.0f s; not retrying", key, status, ra)
                    elif attempt < MAX_RETRIES:
                        wait = max(ra or 0.0, BACKOFF_BASE * (2 ** attempt))
                        host.delay(wait)
                    elif ra:
                        host.delay(ra)      # out of retries; still honour it for the next request
            finally:
                host.release()
            if status == 304:
                self._refund_budget()
                sh.count('not_modified')
            if wait is None:
                return status, headers, body
            attempt += 1
            sh.count('retries')
            logger.info("%s answered %s; retry %s/%s in %.1f s", url, status, attempt, MAX_RETRIES, wait)

    def _send(self, url, extra_headers, limit, timeout, kind):
        import requests
        sh = self._shared
        sh.count('requests')
        deadline = time.monotonic() + timeout
        try:
            resp = self._session().get(url, headers=extra_headers or None, timeout=timeout,
                                       stream=True, allow_redirects=False)
        except requests.RequestException as e:
            raise FetchError(f"request failed: {e}", url) from e
        try:
            headers = Headers(resp.headers)
            status = resp.status_code
            if status in (301, 302, 303, 304, 307, 308):
                return status, headers, b''
            if 200 <= status < 300:
                self._check_type(kind, headers, url)
            clen = headers.get('content-length')
            if clen and clen.isdigit() and int(clen) > limit:
                raise TooLarge(f"answer is {clen} bytes, limit {limit}", url)
            chunks, size = [], 0
            for chunk in resp.iter_content(65536):
                size += len(chunk)
                if size > limit:
                    raise TooLarge(f"answer exceeds the limit of {limit} bytes", url)
                if time.monotonic() > deadline:
                    raise FetchError(f"download took longer than {timeout} s", url)
                chunks.append(chunk)
            sh.count('bytes', size)
            body = b''.join(chunks)
            if 200 <= status < 300:
                self._check_type(kind, headers, url, body)
            return status, headers, body
        except requests.RequestException as e:
            raise FetchError(f"download failed: {e}", url) from e
        finally:
            resp.close()

    # -- robots.txt --

    def _check_robots(self, url, timeout):
        """Raise RobotsDisallowed if robots.txt of the URL's host forbids it.

        Fetched once per host per run, through the same limits and budget. A missing robots.txt or any
        4xx (incl. 401/403, as RFC 9309 says) = allow all. 5xx / network error = allow all with a warning:
        a broken robots.txt must not stop an update; the rate limits still apply.
        """
        sh = self._shared
        origin = self._host_key(url)
        with sh.lock:
            lock = sh.robots_locks.setdefault(origin, threading.Lock())
        with lock:
            with sh.lock:
                known = origin in sh.robots
                parser = sh.robots.get(origin)
            if not known:
                parser = self._load_robots(origin, timeout)
                with sh.lock:
                    sh.robots[origin] = parser
        if parser is not None and not parser.can_fetch(ROBOTS_AGENT, url):
            with sh.lock:
                sh.stats['robots_blocked'] += 1
                sh.blocked.append(url)
            logger.info("robots.txt disallows %s; skipped", url)
            raise RobotsDisallowed(f"robots.txt of {origin} disallows {url}", url)

    def _load_robots(self, origin, timeout):
        from urllib.robotparser import RobotFileParser
        robots_url = origin + '/robots.txt'
        current = robots_url
        try:
            for _ in range(MAX_REDIRECTS + 1):
                self._check_url(current)
                status, headers, body = self._request(current, {}, ROBOTS_MAX_BYTES, timeout, 'page',
                                                      robots=False)
                if status in (301, 302, 303, 307, 308) and headers.get('location'):
                    current = urljoin(current, headers['location'])
                    continue
                break
            else:
                logger.warning("robots.txt of %s: too many redirects; allowing all", origin)
                return None
        except TooLarge:
            logger.warning("robots.txt of %s is too large; allowing all", origin)
            return None
        except BudgetExceeded:
            raise
        except FetchError as e:
            logger.warning("robots.txt of %s could not be fetched (%s); allowing all", origin, e)
            return None
        if status >= 500:
            logger.warning("robots.txt of %s answered %s; allowing all", origin, status)
            return None
        if status != 200:
            return None
        parser = RobotFileParser(robots_url)
        parser.parse(body.decode('utf-8', 'replace').splitlines())
        return parser

    # -- disk cache --

    def _cache_paths(self, url):
        h = hashlib.sha256(url.encode('utf-8')).hexdigest()
        base = os.path.join(self._shared.cache_dir, h[:2])
        return base, os.path.join(base, h + '.json'), os.path.join(base, h + '.body')

    def _cache_fresh(self, url, max_age):
        """(meta, body) of a cached answer saved less than `max_age` seconds ago, else None."""
        _, meta_p, body_p = self._cache_paths(url)
        try:
            with open(meta_p, 'r', encoding='utf-8') as f:
                meta = json.load(f)
            if meta.get('url') != url or time.time() - float(meta.get('saved') or 0) > max_age:
                return None
            with open(body_p, 'rb') as f:
                return meta, f.read()
        except (OSError, ValueError):
            return None

    def _cache_load(self, url):
        _, meta_p, body_p = self._cache_paths(url)
        try:
            with open(meta_p, 'r', encoding='utf-8') as f:
                meta = json.load(f)
        except (OSError, ValueError):
            return None
        if meta.get('url') != url or not os.path.exists(body_p):
            return None
        if not (meta.get('etag') or meta.get('last_modified')):
            return None
        return meta

    def _cache_body(self, url):
        try:
            with open(self._cache_paths(url)[2], 'rb') as f:
                return f.read()
        except OSError:
            return None

    def _cache_store(self, url, headers, body, keep=False):
        etag, lm = headers.get('etag'), headers.get('last-modified')
        if not (etag or lm or keep):
            return      # nothing to revalidate with and no freshness window: don't keep the body
        base, meta_p, body_p = self._cache_paths(url)
        meta = {'url': url, 'etag': etag, 'last_modified': lm,
                'content_type': headers.get('content-type', ''), 'saved': time.time()}
        try:
            os.makedirs(base, exist_ok=True)
            _atomic_write(body_p, body)
            _atomic_write(meta_p, json.dumps(meta).encode('utf-8'))
        except OSError as e:
            logger.warning("http cache: could not store %s (%s)", url, e)

    def _cache_refresh(self, url, meta, headers):
        etag, lm = headers.get('etag'), headers.get('last-modified')
        if (etag and etag != meta.get('etag')) or (lm and lm != meta.get('last_modified')):
            meta = dict(meta, etag=etag or meta.get('etag'), last_modified=lm or meta.get('last_modified'))
            try:
                _atomic_write(self._cache_paths(url)[1], json.dumps(meta).encode('utf-8'))
            except OSError as e:
                logger.warning("http cache: could not update %s (%s)", url, e)
