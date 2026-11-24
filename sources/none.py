"""The "no data source" choice: preview and manual editing only.

Default for new installations (``data_source: none``). The app loads, previews
and edits the car's image + databases and builds the USB package, but never
fetches station data, so nothing country-specific is assumed and no network
request is made. The UI hides the update button and explains how to enable a
real source (``radiopolska`` for Poland, ``csv_file`` for your own list).
"""

from __future__ import annotations

from typing import Iterable

from .base import DataSource, FetchPolicy, Station


class NoSource(DataSource):
    id = "none"
    name = "None (preview / manual editing)"
    api_version = 2
    fetch_policy = FetchPolicy(network=False)

    def change_signature(self) -> str | None:
        return "none"          # never changes: the core never fetches

    def fetch_stations(self) -> Iterable[Station]:
        return []
