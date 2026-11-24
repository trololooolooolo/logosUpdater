"""CSV-file data source for LogosUpdater.

A real, working second source that proves the plugin interface without
any network access: it reads stations from a local CSV file whose path
and column mapping come from ``config.yaml``.

Config block (``sources.csv_file``)
-----------------------------------
::

    data_source: csv_file
    sources:
      csv_file:
        path: stations.csv          # path to the CSV (absolute or cwd-relative)
        default_ecc: E0             # RDS country for rows without an `ecc` value
        columns:                    # map CSV header -> Station field
          id:   ID                  # required
          name: Name                # required
          freq: Freq                # required (raw frequency text)
          link: Link                # optional
          logo: Logo                # optional (per-row logo URL)
          ecc:  ECC                 # optional (per-row RDS country, e.g. E0)

``id`` is the station's RDS PI code (4 hex digits, e.g. ``D312``) — that is
what the radio matches logos by. Frequencies may use a dot or a comma
(quote a comma value). Every row needs a country: an ``ecc`` value or
``default_ecc``; rows with neither are skipped and reported.

Expected CSV (header row required)::

    ID,Name,Freq,Link,Logo,ECC
    D312,My Radio,98.5,https://example.com/1,https://example.com/1/logo.png,E0
    D3A0,DAB Station,DAB,https://example.com/2,,E0

See ``docs/examples/stations.example.csv``.

``change_signature`` is the file's mtime; the core compares it with the stored one, so
re-running only triggers work when the CSV actually changed.

Stdlib-only (``csv``, ``os``).
"""

from __future__ import annotations

import csv
import logging
import os
from typing import Iterable

from .base import DataSource, FetchPolicy, Setting, SourceError, Station

logger = logging.getLogger(__name__)

# Defaults used when the config omits `columns` (or individual keys).
_DEFAULT_COLUMNS = {
    "id": "ID",
    "name": "Name",
    "freq": "Freq",
    "link": "Link",
    "logo": "Logo",
    "ecc": "ECC",
}


class CsvFileSource(DataSource):
    """Read stations from a local CSV file."""

    id = "csv_file"
    name = "CSV file"
    api_version = 2
    # Reads a local file; logo URLs from the CSV are downloaded by the core (rate-limited).
    fetch_policy = FetchPolicy(network=False)
    settings_schema = (
        Setting("path", "path", label="set.csv_path", help="set.csv_path_help", required=True,
                placeholder="stations.csv"),
        Setting("default_ecc", "ecc", label="set.csv_ecc", help="set.csv_ecc_help", placeholder="E0"),
    )

    def __init__(self, settings: dict):
        super().__init__(settings)
        self.path: str = self.settings.get("path", "stations.csv")
        # Merge user column map over defaults so partial maps still work.
        self.columns: dict = {**_DEFAULT_COLUMNS, **(self.settings.get("columns") or {})}

    def change_signature(self) -> str | None:
        """The CSV's modification time: re-reading only happens when the file changed."""
        try:
            return str(os.path.getmtime(self.path))
        except OSError as exc:
            logger.debug("csv_file: cannot stat %s: %s", self.path, exc)
            return None

    def fetch_stations(self) -> Iterable[Station]:
        """Yield one :class:`Station` per CSV row.

        Missing optional columns (link/logo) just stay empty. Rows
        lacking required fields are skipped with a debug log.
        """
        col = self.columns
        if not os.path.exists(self.path):
            raise SourceError("err.csv_not_found", f"CSV not found: {self.path}", path=self.path)
        with open(self.path, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                sid = (row.get(col["id"]) or "").strip()
                name = (row.get(col["name"]) or "").strip()
                freq_raw = (row.get(col["freq"]) or "").strip()
                if not sid or not name:
                    logger.debug("csv_file: skipping row without id/name: %r", row)
                    continue
                yield Station(
                    id=sid,
                    name=name,
                    freq_raw=freq_raw,
                    link=(row.get(col["link"]) or "").strip(),
                    logo_url=(row.get(col["logo"]) or "").strip(),
                    ecc=(row.get(col["ecc"]) or "").strip().upper(),
                )

    def fetch_logo_url(self, station: Station) -> str:
        """No separate lookup — logos (if any) come from the CSV column.

        ``fetch_stations`` already fills ``logo_url`` from the optional
        ``logo`` column, so there is nothing to fetch here.
        """
        return ""
