"""RadioPolska data source — the reference plugin for LogosUpdater.

Scrapes the Polish FM/DAB station list from radiopolska.pl. This is the
original built-in source, refactored to the :class:`DataSource` contract so
other sources can sit alongside it. Configure via ``sources.radiopolska`` in
``config.yaml`` (base_url, list_path, changes_path, logo_selector).

Requires ``beautifulsoup4`` (auto-installed by the app). All requests go through
the shared rate-limited client (``self.http``): one request at a time to the site,
at least ``min_interval`` seconds apart, honest User-Agent, robots.txt respected,
detail pages cached between updates. A full first update therefore takes minutes;
that is deliberate - radiopolska.pl is a volunteer-run site.
Logs stay English; this module prints no user-facing text.
"""

from __future__ import annotations

import logging
import re
from typing import Iterable

from bs4 import BeautifulSoup

from fetcher import BudgetExceeded, FetchError, RobotsDisallowed

from .base import DataSource, FetchPolicy, SourceError, Station

logger = logging.getLogger(__name__)


class RadioPolskaSource(DataSource):
    id = "radiopolska"
    name = "RadioPolska"
    default_ecc = "E2"   # Poland — every station on radiopolska.pl is Polish
    api_version = 2
    fetch_policy = FetchPolicy(network=True, max_parallel=1, min_interval=1.0)

    def __init__(self, settings: dict):
        super().__init__(settings)
        s = self.settings
        self.base_url = (s.get("base_url") or "https://radiopolska.pl").rstrip("/")
        self.list_path = s.get("list_path") or "/wykaz/bazapi"
        self.changes_path = s.get("changes_path") or "/wykaz/zmiany"
        self.logo_selector = s.get("logo_selector") or "img.wykaz-logotyp"

    # ------------------------------------------------------------------ helpers
    def _abs_url(self, href: str) -> str:
        """Normalize an href to an absolute URL.

        RadioPolska started returning ABSOLUTE links (https://...) instead of
        relative (/...); the old startswith('/') check produced empty links and
        zero downloaded logos, so handle both forms.
        """
        if not href:
            return ""
        if href.startswith("http"):
            return href
        if href.startswith("/"):
            return self.base_url + href
        return ""

    def _parse_logo_selector(self):
        """Turn 'img.wykaz-logotyp' into (tag, {'class': 'wykaz-logotyp'}) for bs4."""
        sel = self.logo_selector.strip()
        tag, cls = sel, None
        if "." in sel:
            tag, cls = sel.split(".", 1)
        return (tag or "img"), ({"class_": cls} if cls else {})

    # ------------------------------------------------------------------ contract
    def change_signature(self) -> str | None:
        """First row of the 'what changed' page.

        The page is a long table (~2 MB in 2026), hence kind="list" (10 MB limit). Without a
        signature every update re-reads all station pages, so failures are logged as warnings.
        """
        try:
            resp = self.http.get(self.base_url + self.changes_path, kind="list")
        except (RobotsDisallowed, BudgetExceeded):
            raise
        except FetchError as e:
            logger.warning("RadioPolska: no change signature (%s); this update re-reads every station page", e)
            return None
        if resp.status != 200:
            logger.warning("RadioPolska: changes page answered HTTP %s; no change signature", resp.status)
            return None
        table = BeautifulSoup(resp.text, "html.parser").find("table")
        rows = table.find_all("tr") if table else []
        if len(rows) < 2:
            logger.warning("RadioPolska: no table on the changes page (layout changed?); no change signature")
            return None
        return "|".join(c.get_text(strip=True) for c in rows[1].find_all("td"))

    def fetch_stations(self) -> Iterable[Station]:
        """Scrape the station/frequency list (one Station per frequency row)."""
        url = self.base_url + self.list_path
        resp = self.http.get(url, kind="list")
        if resp.status != 200:
            raise SourceError("err.source_http", f"station list: HTTP {resp.status}", status=resp.status, url=url)
        table = BeautifulSoup(resp.text, "html.parser").find("table")
        if not table:
            raise SourceError("err.source_layout", "no station table on the list page", source=self.name)
        stations: list[Station] = []
        for row in table.find_all("tr")[1:]:
            cols = row.find_all("td")
            if len(cols) < 3:
                continue
            sid = cols[0].get_text(strip=True)
            link_tag = cols[1].find("a")
            prog = link_tag.get_text(strip=True) if link_tag else cols[1].get_text(strip=True)
            link = self._abs_url(link_tag.get("href")) if link_tag else ""
            if cols[2].find("ul"):
                freqs = [li.get_text(strip=True) for li in cols[2].find_all("li")]
            else:
                freqs = [cols[2].get_text(strip=True)]
            for fr in freqs:
                stations.append(Station(id=sid, name=prog, freq_raw=fr, link=link))
        return stations

    def fetch_logo_url(self, station: Station) -> str:
        """Open the station page and read the logo <img> src."""
        if not station.link:
            return ""
        try:
            r = self.http.get(station.link, kind="page")
        except (RobotsDisallowed, BudgetExceeded):
            raise                           # stop the whole update, the core reports it
        except FetchError as e:
            logger.debug("RadioPolska.fetch_logo_url(%s): %s", station.link, e)
            return ""
        if r.status == 200:
            tag, kw = self._parse_logo_selector()
            img = BeautifulSoup(r.text, "html.parser").find(tag, **kw)
            if img and img.get("src"):
                return self._abs_url(img.get("src"))
        return ""

    def classify_freq(self, raw: str) -> tuple[bool, str | None]:
        """(is_dab, freq_db). Polish format: '98,5' -> '098500'; DAB -> (True, None).

        The freq text may be '100,2 / Bialystok *Krynice (h332)*' (location +
        digits), so extract the bare frequency with a regex rather than gluing
        all digits together (which gave wrong values).
        """
        s = str(raw)
        is_dab = ("DAB" in s.upper()) or (not any(c.isdigit() for c in s))
        freq_db = None
        if not is_dab:
            m = re.search(r"(\d{2,3})[.,](\d)", s)
            if m:
                mhz = float(f"{m.group(1)}.{m.group(2)}")
                freq_db = f"{int(round(mhz * 1000)):06d}"
        return is_dab, freq_db
