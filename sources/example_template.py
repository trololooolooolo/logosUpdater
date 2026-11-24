"""TEMPLATE: a skeleton data-source plugin for LogosUpdater.

Copy this file to ``sources/my_source.py``, rename the class, give it a
unique ``id``, and fill in the four methods. This template is fully
importable as-is (its ``fetch_stations`` just returns an empty list), so
the registry loads it under id ``example`` (hidden from the UI by
``listed = False`` - remove that line in your copy).

What a data source does
-----------------------
A data source produces :class:`~sources.base.Station` records. The core
maps those onto the Ford Sync 3.4 QNX6 image + SQLite DB. You never touch
the device side — you only provide station data.

How to register your source (3 steps)
-------------------------------------
1. Drop your ``.py`` file into the ``sources/`` package. The registry in
   ``sources/__init__.py`` auto-discovers every ``DataSource`` subclass.
2. Give your class a unique ``id`` (and a friendly ``name``).
3. Select it in ``config.yaml``::

       data_source: example
       sources:
         example:
           # whatever your __init__ reads from self.settings
           some_option: value

See ``docs/SOURCES.md`` for the full contract and a worked example
(``csv_file.py``).
"""

from __future__ import annotations

from typing import Iterable

from .base import DataSource, FetchPolicy, Setting, SourceError, Station


class ExampleSource(DataSource):
    """Minimal skeleton (plugin API v2) - replace every method body with real logic."""

    # Unique id. Must match config.yaml's `data_source` and the
    # `sources.<id>` settings key. Change this for your own source.
    id = "example"

    # Human-readable label shown in the UI / logs.
    name = "Example (template)"

    # Plugin contract version this file follows (sources.base.API_VERSION).
    api_version = 2

    # The template is not a real source: keep it out of the UI list. Delete in your copy.
    listed = False

    # What you need from the network. The app limits every host to 1 request at a time
    # with >= 1 s between requests, sends an honest User-Agent and honours robots.txt;
    # here you can only ask for STRICTER limits (e.g. min_interval=3.0 for a small site).
    # A source that reads a local file: FetchPolicy(network=False).
    fetch_policy = FetchPolicy(network=True, max_parallel=1, min_interval=1.0)

    # RDS country (ECC) of your stations, e.g. "E0" for Germany, "E2" for Poland.
    # Single-country source: set it here (users can override it with
    # `sources.example.default_ecc`). Multi-country source: leave "" and set
    # Station(ecc=...) on every station instead. Rows without a country are skipped.
    default_ecc = ""

    # Fields shown in ⚙ Settings when this source is selected; values land in config.yaml under
    # sources.example.<key> and arrive in __init__ as self.settings. Labels: plain text,
    # {"en": ..., "pl": ...} or an i18n key. Types: text, path, url, ecc, number, bool, choice, secret.
    settings_schema = (
        Setting("list_url", "url", label={"en": "Station list address", "pl": "Adres listy stacji"},
                help={"en": "Page with the table of stations.", "pl": "Strona z tabelą stacji."},
                required=True, placeholder="https://example.com/stations"),
        Setting("default_ecc", "ecc", label="set.csv_ecc", placeholder="E0"),
        Setting("api_key", "secret", label={"en": "API key", "pl": "Klucz API"}),
    )

    def __init__(self, settings: dict):
        # `settings` is config['sources']['example'] (or {} if absent).
        super().__init__(settings)
        self.list_url = self.settings.get("list_url", "")

    def change_signature(self) -> str | None:
        """Cheap marker of the upstream state, or None (= always fetch).

        E.g. the Last-Modified/ETag of the list, or a "last changed" date on a
        news page. The core skips fetch_stations() while it stays the same.
        """
        return None

    def fetch_stations(self) -> Iterable[Station]:
        """Return/yield every station this source knows about.

        Network: always through self.http (rate limit, timeout, robots.txt, cache)::

            resp = self.http.get(self.list_url, kind="list")
            if resp.status != 200:
                raise SourceError("err.source_http", "list download failed",
                                  status=resp.status, url=self.list_url)
            for i, row in enumerate(parse(resp.text)):
                if self.cancelled():
                    break
                self.progress(i + 1, None, row.name)
                yield Station(
                    id="D312",            # RDS PI code (4 hex digits) - NOT a site record number
                    name="My Radio",
                    freq_raw="98,5",      # "98.5" works too; "DAB" for DAB
                    ecc="E0",             # optional if default_ecc is set
                    link="https://example.com/stations/123",
                    logo_url="",          # or set it directly if you have it
                )
        """
        if not self.list_url:
            raise SourceError("err.source_not_configured", "list_url is not set", source=self.name)
        return []

    def fetch_logo_url(self, station: Station) -> str:
        """Optional: return a logo image URL for one station, or "".

        Called one station at a time (per distinct station.link). Fetch the detail
        page with self.http.get(station.link, kind="page"). Delete if unused.
        """
        return ""
