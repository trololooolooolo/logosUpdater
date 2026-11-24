"""Data-source plugin contract for LogosUpdater.

This module defines the two things every station-data plugin builds on:

* :class:`Station` — the normalized record a plugin produces for one
  station/frequency entry. The core maps these onto the Ford Sync 3.4
  QNX6 image + the ``ECC, STATION_ID, RECEIVER_TYPE, FREQUENCY,
  LOGO_FILENAME`` SQLite schema. Plugins do NOT touch the device side;
  they only produce data.

* :class:`DataSource` — the abstract base class a plugin subclasses. The
  registry in ``sources/__init__.py`` auto-discovers every subclass and
  the core instantiates the configured one.

Plugin authors: subclass :class:`DataSource`, set a unique ``id`` and a
human-readable ``name``, and implement the abstract methods. Drop your
module into the ``sources/`` package and add a ``sources.<id>`` block to
``config.yaml``. See ``docs/SOURCES.md`` and ``example_template.py``.

This module is stdlib-only on purpose: the base contract and registry
must import without ``requests`` / ``beautifulsoup4`` installed. A
concrete plugin may of course depend on whatever it likes.
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Iterable

from fetcher import FetchPolicy  # stdlib-only at import time (requests is loaded lazily)

#: Version of the plugin contract this module defines. Plugins declare the version
#: they were written for in :attr:`DataSource.api_version`.
API_VERSION = 2

logger = logging.getLogger(__name__)


class SourceError(Exception):
    """An error the user should see (shown in the UI, translated).

    ``key`` is an i18n key from ``lang/*.yaml`` (e.g. ``"err.source_list_missing"``),
    ``fmt`` its placeholders. Plain-English ``str(exc)`` is used in logs and as a
    fallback when the key is unknown.
    """

    def __init__(self, key: str, message: str = "", **fmt):
        super().__init__(message or key)
        self.key = key
        self.fmt = fmt


SETTING_TYPES = ("text", "path", "url", "ecc", "number", "bool", "choice", "secret")


@dataclass(frozen=True)
class Setting:
    """One user setting of a plugin, shown as a field in ⚙ Settings and stored in
    ``config.yaml`` under ``sources.<plugin id>.<key>``.

    ``label`` / ``help``: plain text, ``{"en": ..., "pl": ...}`` or an i18n key from
    ``lang/*.yaml``. ``type``: text | path | url | ecc (2 hex digits) | number | bool |
    choice (one of ``choices``) | secret (API key: never sent back to the browser;
    saving it empty keeps the stored value).
    """
    key: str
    type: str = "text"
    label: object = ""
    help: object = ""
    default: object = None
    required: bool = False
    choices: tuple = ()
    min: float | None = None
    max: float | None = None
    placeholder: str = ""


def validate_setting(field: Setting, raw):
    """Clean one submitted value; raises SourceError('err.setting_invalid', field=..., reason=...)."""
    def bad(reason, **detail):
        # reason = a code; the UI shows lang key set.reason_<code> with `detail` as placeholders
        raise SourceError("err.setting_invalid", f"{field.key}: {reason} {detail or ''}".strip(),
                          field=field.key, reason=reason, **detail)
    t = field.type
    if t == "bool":
        return bool(raw) if not isinstance(raw, str) else raw.strip().lower() in ("1", "true", "yes", "on")
    v = "" if raw is None else str(raw).strip()
    if not v:
        if field.required:
            bad("required")
        return ""
    if len(v) > 500 or "\n" in v or "\r" in v:
        bad("text")
    if t == "ecc":
        v = v.upper()
        if not re.fullmatch(r"[0-9A-F]{2}", v):
            bad("ecc")
    elif t == "url":
        if not re.match(r"https?://[^\s/]+", v):
            bad("url")
    elif t == "number":
        try:
            n = float(v.replace(",", "."))
        except ValueError:
            bad("number")
        if (field.min is not None and n < field.min) or (field.max is not None and n > field.max):
            bad("range", min=field.min, max=field.max)
        v = int(n) if n.is_integer() and not isinstance(field.default, float) else n
    elif t == "choice":
        if v not in [str(c) for c in field.choices]:
            bad("choice", choices=", ".join(map(str, field.choices)))
    elif t not in SETTING_TYPES:
        bad("type", type=t)
    return v


def validate_settings(schema, data: dict, current: dict | None = None) -> dict:
    """Clean the submitted values of one plugin against its schema.

    Unknown keys are dropped; an empty ``secret`` keeps the ``current`` value (it is never
    shown to the browser, so an empty field means "unchanged"). Returns only keys with a value.
    """
    data, current = data or {}, current or {}
    out = {}
    for f in schema:
        if f.type == "secret" and not str(data.get(f.key) or "").strip():
            if current.get(f.key):
                out[f.key] = current[f.key]
            elif f.required:
                validate_setting(f, "")
            continue
        if f.key not in data and f.type != "bool":
            if f.required and not current.get(f.key):
                validate_setting(f, "")
            continue
        v = validate_setting(f, data.get(f.key))
        if v != "" or f.type == "bool":
            out[f.key] = v
    return out


@dataclass
class Station:
    """One station/frequency entry produced by a data source.

    A real-world station broadcasting on several frequencies typically
    yields several :class:`Station` records that share ``id`` and
    ``name`` but differ in ``freq_raw``.

    Fields
    ------
    id:
        The station's RDS **PI code** (FM) or DAB **service id**: 4 hex
        digits, e.g. ``"3D01"``. This is what the radio matches logos by and
        what the core writes to ``STATION_ID``; rows with anything else are
        rejected by ``ford_db.validate_station`` and counted as skipped.
        Do not put a website's own record number here.
    name:
        Human-readable program/station name.
    freq_raw:
        Raw frequency text exactly as the source provides it, e.g.
        ``"98,5"`` or ``"100,2 / Bialystok"`` or ``"DAB"``. The core
        never parses this directly — it calls
        :meth:`DataSource.classify_freq` on it.
    link:
        Optional detail/station page URL (used to fetch the logo, shown
        in the UI). Empty string when unknown.
    logo_url:
        Optional logo image URL. May be left empty here and filled later
        by :meth:`DataSource.fetch_logo_url`.
    is_dab:
        ``True`` for DAB, ``False`` for FM, ``None`` if not yet
        classified. When ``None`` the core calls
        :meth:`DataSource.classify_freq` to fill it.
    freq_db:
        6-digit FM frequency string used by the Ford DB, e.g. ``98,5`` ->
        ``"098500"``. ``None`` for DAB. Also filled by
        :meth:`DataSource.classify_freq` when left ``None``.
    ecc:
        RDS Extended Country Code of the station's country, two hex
        characters (``"E2"`` = Poland, ``"E0"`` = Germany, ...). Leave
        empty to use the source's :attr:`DataSource.default_ecc`. A
        station with neither is skipped — the core never guesses a country.
    """

    id: str
    name: str
    freq_raw: str                 # raw frequency text from the source
    link: str = ""                # detail/station page url (optional)
    logo_url: str = ""            # logo image url (optional; may be filled by fetch_logo_url)
    is_dab: bool | None = None    # filled by classify_freq if None
    freq_db: str | None = None    # 6-digit FM string e.g. "098500", None for DAB
    ecc: str = ""                 # RDS country code, e.g. "E2"; "" -> source default_ecc


class DataSource(ABC):
    """Abstract base class for a station-data plugin (contract version 2).

    Lifecycle
    ---------
    The core reads ``config.yaml``, looks up ``data_source`` (an id),
    finds the matching subclass via the registry, instantiates it with that
    source's settings and binds the shared HTTP client and progress hooks::

        src = registry.get_active(cfg)          # -> YourSource(settings), bound
        sig = src.change_signature()            # cheap; None = unknown
        if sig is None or sig != stored_signature:
            for st in src.fetch_stations():
                if not st.logo_url:
                    st.logo_url = src.fetch_logo_url(st)    # called one by one
                # ... core validates, writes the Ford DBs, downloads logos ...

    Subclass contract
    ------------------
    * Set :attr:`id`, :attr:`name`, :attr:`api_version` ``= 2`` and either
      :attr:`default_ecc` (single-country source) or :attr:`Station.ecc`
      on every station.
    * Implement :meth:`fetch_stations`; implement :meth:`change_signature`
      when there is a cheap "what changed" marker.
    * Network access **only** through :attr:`http` (``self.http.get(url, kind=...)``):
      it applies the per-host rate limit, timeouts, size limits, robots.txt and the
      response cache. Declare what you need in :attr:`fetch_policy` (you may only make
      the limits stricter). Never create your own ``requests.Session``.
    * Report progress with :meth:`progress` and stop early when :meth:`cancelled`.
    * Raise :class:`SourceError` for problems the user must see (page layout
      changed, file not found); return what you have for per-station misses.
    * Optionally override :meth:`fetch_logo_url` (default returns ``""``)
      and :meth:`classify_freq` (default handles ``98,5``/``98.5``/DAB).
    * Keep logs/console output in English; never print user-facing text.

    Version 1 plugins (no ``api_version``; ``needs_update()`` instead of
    ``change_signature()``) keep working: the core logs a deprecation warning
    and applies the default fetch policy.
    """

    #: Unique source id. Must match ``config.yaml``'s ``data_source`` and
    #: the ``sources.<id>`` settings key. Override in every subclass.
    id: str = ""

    #: Human-readable label shown in the UI / logs. Override in subclass.
    name: str = ""

    #: Contract version the plugin was written for (see :data:`API_VERSION`).
    #: The base class says 1 so that plugins which don't set it are treated as v1.
    api_version: int = 1

    #: Shown in the UI source list. ``False`` for templates/test helpers.
    listed: bool = True

    #: User settings shown in ⚙ Settings for this source (a tuple of :class:`Setting`);
    #: stored in ``config.yaml`` under ``sources.<id>`` and passed to ``__init__``.
    settings_schema: tuple = ()

    #: What the plugin needs from the network. ``FetchPolicy(network=False)`` for
    #: offline sources (a local file); limits here can only make the global ones stricter.
    fetch_policy: FetchPolicy = FetchPolicy()

    #: ECC used for stations that don't set :attr:`Station.ecc` (e.g. ``"E2"``
    #: for a Poland-only source). ``settings['default_ecc']`` overrides it.
    #: Leave empty when every station carries its own ECC.
    default_ecc: str = ""

    #: Shared HTTP client (``fetcher.Fetcher`` limited by :attr:`fetch_policy`),
    #: set by the core through :meth:`bind`. ``None`` until bound.
    http = None

    def __init__(self, settings: dict):
        """Store this source's settings dict.

        ``settings`` is ``config['sources'][self.id]`` (an empty dict if
        the user provided no block). Subclasses may read it in
        ``__init__`` or lazily; calling ``super().__init__(settings)`` is
        recommended so :attr:`settings` is always available.
        """
        self.settings: dict = settings or {}
        if self.settings.get("default_ecc"):
            self.default_ecc = str(self.settings["default_ecc"]).strip().upper()

    # ------------------------------------------------------------------ core hooks
    def bind(self, http=None, progress: Callable | None = None, cancelled: Callable | None = None):
        """Called by the core: attach the shared HTTP client and the progress/cancel hooks."""
        self.http = http
        self._progress = progress
        self._cancelled = cancelled
        return self

    def progress(self, done: int, total: int | None = None, message: str = "") -> None:
        """Report progress of a long step (shown in the UI). Cheap; call it often."""
        cb = getattr(self, "_progress", None)
        if cb:
            cb(done, total, message)

    def cancelled(self) -> bool:
        """True when the user pressed Stop: return what you have as soon as possible."""
        cb = getattr(self, "_cancelled", None)
        return bool(cb and cb())

    # ------------------------------------------------------------------ contract
    def change_signature(self) -> str | None:
        """Return a cheap marker of the upstream data's state, or ``None``.

        The core stores it after a successful update and skips the expensive
        :meth:`fetch_stations` while the marker stays the same. Use a "last
        changed" date, an ETag, a file's mtime... ``None`` = unknown, always fetch.
        Must be cheap (one small request at most).
        """
        if type(self).needs_update is not DataSource.needs_update:   # v1 plugin
            try:
                return self.needs_update()[1]
            except Exception as exc:  # noqa: BLE001 - v1 contract: unknown = update
                logger.debug("%s.needs_update failed: %s", self.id, exc)
        return None

    def needs_update(self) -> tuple[bool, str | None]:
        """Deprecated (contract v1): ``(update?, signature)``. Override
        :meth:`change_signature` instead; the boolean was never used by the core."""
        return True, None

    @abstractmethod
    def fetch_stations(self) -> Iterable[Station]:
        """Yield/return the full list of :class:`Station` records.

        This is the expensive call. It should return every station the
        source knows about (the core diffs against the DB itself). You
        may leave ``logo_url``/``is_dab``/``freq_db`` unset — the core
        fills them via :meth:`fetch_logo_url` and :meth:`classify_freq`.

        Return any iterable (a list, or a generator that ``yield``s).
        """
        raise NotImplementedError

    def fetch_logo_url(self, station: Station) -> str:
        """Return the logo image URL for one station, or ``""``.

        Optional. The default returns ``""`` (no per-station logo
        lookup). Override when logos live on a separate page that must be
        scraped/queried lazily. The core calls it once per distinct
        ``station.link``, one call at a time, with the full :class:`Station`;
        fetch through :attr:`http` so the rate limit applies.
        """
        return ""

    def classify_freq(self, raw: str) -> tuple[bool, str | None]:
        """Classify a raw frequency string.

        Returns ``(is_dab, freq_db)`` where ``is_dab`` is ``True`` for
        DAB and ``freq_db`` is the 6-digit FM string (e.g. ``"098500"``)
        or ``None`` for DAB / an unparsable value.

        The default accepts a decimal comma or dot (``98,5`` / ``98.5``,
        also inside longer text such as ``"100,2 / City"``) and treats text
        containing "DAB" or no digits as DAB. Override for other wording.
        """
        return classify_freq_default(raw)


def classify_freq_default(raw) -> tuple[bool, str | None]:
    """Shared FM/DAB classifier: ``"98,5"``/``"98.5"`` -> ``(False, "098500")``.

    Extracts the bare frequency with a regex rather than concatenating all
    digits (which produced wrong values on strings like ``"100,2 / City"``).
    """
    s = str(raw or "")
    is_dab = ("DAB" in s.upper()) or (not any(c.isdigit() for c in s))
    if is_dab:
        return True, None
    m = re.search(r"(\d{2,3})[.,](\d{1,2})", s) or re.search(r"\b(\d{2,3})\b()", s)
    if not m:
        return False, None
    mhz = float(f"{m.group(1)}.{m.group(2) or 0}")
    return False, f"{int(round(mhz * 1000)):06d}"
