# Data Sources (plugin interface)

LogosUpdater gets its station data from a **data source plugin**. RadioPolska
is one such plugin; you can add your own (a CSV file, an API, another scraper)
without touching the core. The core takes the `Station` records a source
produces and maps them onto the Ford Sync 3.4 QNX6 image + SQLite DB. **Plugins
provide station data; they never touch the device side.**

All of this lives in the `sources/` package:

| File | Role |
|------|------|
| `sources/base.py` | `Station` dataclass + `DataSource` ABC (the contract) |
| `sources/__init__.py` | registry: auto-discovers plugins, `available()`, `get_active(cfg)` |
| `sources/none.py` | `none` — no source: preview and manual editing only (the default) |
| `sources/radiopolska.py` | the built-in RadioPolska scraper (Poland) |
| `sources/csv_file.py` | a real CSV-file source (worked example below) |
| `sources/example_template.py` | commented skeleton to copy |

The base module and registry are **stdlib-only** — they import fine without
`requests`/`beautifulsoup4`. Individual plugins may depend on whatever they
need; if a plugin fails to import, the registry logs an English warning, skips
it, and **⚙ Settings** shows "Plugin X did not load: …".

This page describes **plugin API version 2** (`sources.base.API_VERSION`).

---

## The `Station` record

`fetch_stations()` produces `Station` objects. One station broadcasting on
several frequencies usually yields several records sharing `id`/`name`.

| Field | Type | Required | Meaning |
|-------|------|----------|---------|
| `id` | `str` | yes | The station's RDS **PI code**, 4 hex digits (`"D312"`). The radio matches logos by it. |
| `name` | `str` | yes | Program/station display name. |
| `freq_raw` | `str` | yes | Raw frequency text from the source (`"98,5"`, `"DAB"`, `"100,2 / City"`). |
| `link` | `str` | no | Detail/station page URL. Default `""`. |
| `logo_url` | `str` | no | Logo image URL. May be filled later by `fetch_logo_url`. Default `""`. |
| `is_dab` | `bool \| None` | no | `True`/`False`; `None` -> core fills it via `classify_freq`. |
| `freq_db` | `str \| None` | no | 6-digit FM string (`"098500"`) or `None` for DAB; filled by `classify_freq`. |
| `ecc` | `str` | see below | RDS Extended Country Code, 2 hex digits (`"E0"` Germany, `"E2"` Poland, ...). |

You only need to set `id`, `name`, `freq_raw` — plus the **country**: either
`ecc` on each station or `default_ecc` on the source. Leave `is_dab`/`freq_db`
as `None` and the core fills them by calling `classify_freq`.

The core validates every record before writing: ECC = 2 hex digits, PI = 4 hex
digits, FM between 87.5 and 108.0 MHz. Invalid records (and records with no
country) are **skipped, never guessed**; the update result lists how many and why.

---

## The `DataSource` contract

Subclass `DataSource`, set the class attributes, implement `fetch_stations` (and
`change_signature` when you can).

### Class attributes

* `id: str` — unique source id. **Must** match `config.yaml`'s `data_source`
  and the `sources.<id>` settings key.
* `name: str` — human-readable label for UI/logs.
* `api_version = 2` — the contract version you wrote the plugin for. Plugins
  without it are treated as version 1: they keep working, with a warning in the
  log and the default network limits.
* `fetch_policy = FetchPolicy(network=True, max_parallel=1, min_interval=1.0)` —
  what you need from the network (see [Network rules](#network-rules)). An offline
  source (a local file) sets `FetchPolicy(network=False)` and gets no HTTP client.
* `listed = True` — `False` hides the plugin from the UI list (the template uses this).
* `settings_schema = (Setting(...), ...)` — the fields shown in **⚙ Settings** when
  your source is selected (see [Settings form](#settings-form)).
* `default_ecc: str` — country for stations without `Station.ecc` (e.g. `"E2"` for a
  Poland-only source). Users can override it with `sources.<id>.default_ecc`.
  Leave `""` if every station carries its own `ecc`.

### `__init__(self, settings: dict)`

`settings` is `config['sources'][self.id]` (an empty dict if the user gave no
block). Call `super().__init__(settings)` so `self.settings` is available, then
read your options. Example: `self.path = settings.get("path", "stations.csv")`.
Don't touch the network here.

### Settings form

Declare the options a user should be able to set without editing `config.yaml`:

```python
from .base import Setting

settings_schema = (
    Setting("list_url", "url", label={"en": "Station list address", "pl": "Adres listy stacji"},
            help={"en": "Page with the table of stations.", "pl": "Strona z tabelą stacji."},
            required=True, placeholder="https://example.com/stations"),
    Setting("default_ecc", "ecc", label="set.csv_ecc", placeholder="E0"),
    Setting("api_key", "secret", label={"en": "API key", "pl": "Klucz API"}),
)
```

* Types: `text`, `path`, `url` (http/https), `ecc` (2 hex digits, upper-cased), `number`
  (`min`/`max`), `bool`, `choice` (`choices=(...)`), `secret`.
* `label` / `help`: plain text, a `{"en": …, "pl": …}` dict, or an i18n key from `lang/*.yaml`.
* The server validates every value against the schema before it writes
  `config.yaml` (`sources.<id>.<key>`); a bad value is refused with a message naming
  the field. Keys not in the schema are never written from the UI (they can still be
  set in `config.yaml`, e.g. the CSV `columns` map), and a field cleared in the form
  is removed from the file.
* A `secret` value is never sent to the browser; the form shows that one is saved,
  and leaving the field empty keeps it.
* The values reach your plugin as `self.settings` in `__init__`.

### What the core gives you

After creating the plugin the core calls `bind()`, which sets:

* `self.http` — the shared HTTP client (`fetcher.Fetcher`), already limited by
  your `fetch_policy`. Use `self.http.get(url, kind="page" | "list" | "image")`;
  it returns a `Response` (`status`, `content`, `text`, `headers`, `from_cache`)
  and raises `fetcher.FetchError` subclasses on failure. `None` when
  `network=False`.
* `self.progress(done, total=None, message="")` — report progress of a long loop;
  the UI shows it with an estimated time left.
* `self.cancelled()` — `True` after the user pressed **Stop**: return what you have.

### `change_signature(self) -> str | None`  *(recommended)*

A cheap marker of the upstream state: a "last changed" date, an ETag, a file's
mtime. The core stores it after a successful update and skips `fetch_stations()`
while it stays the same. `None` means "unknown — always fetch". At most one small
request. (Version 1 plugins implemented `needs_update() -> (bool, signature)`
instead; the boolean was never used and the method is deprecated.)

### Errors

Raise `SourceError(key, message, **fmt)` for problems the **user** has to see —
the list page could not be downloaded, its layout changed, the file is missing.
`key` is an i18n key (`lang/*.yaml`, e.g. `err.source_layout`), `fmt` its
placeholders, `message` an English text for the log. Don't raise for a single
station's missing logo — just return `""` for it. Let `fetcher.RobotsDisallowed`
and `fetcher.BudgetExceeded` propagate: the core stops the update and tells the user.

### `fetch_stations(self) -> Iterable[Station]`  *(abstract)*

The expensive call. Return/yield **every** station the source knows about (the
core diffs against the DB). Return a list or be a generator.

### `fetch_logo_url(self, station: Station) -> str`  *(optional)*

Return one station's logo URL, or `""`. Default returns `""`. Override only if
logos must be looked up per station (e.g. scraped from a detail page). The core
calls it once per distinct `station.link`, one call at a time, with the full
`Station`; fetch through `self.http`.

### `classify_freq(self, raw: str) -> tuple[bool, str | None]`  *(optional)*

Return `(is_dab, freq_db)`. `freq_db` is the 6-digit FM string (`98,5` ->
`"098500"`) or `None` for DAB. The inherited default (`classify_freq_default` in
`sources/base.py`) accepts a decimal comma or dot, also inside longer text
(`"100,2 / City"`), and treats text containing "DAB" or no digits as DAB.
Override only if your source spells things differently.

> Keep logs/console output in English; never print user-facing text from a
> plugin.

---

## Network rules

Every request a plugin makes goes through `self.http`, and the logo files the core
downloads go through the same client. It is deliberately polite, because station
sites are often small and volunteer-run, and every user of the app hits them:

* **One request at a time per host, at least 1 s apart** (`fetch.max_parallel`,
  `fetch.min_interval` in `config.yaml`; hard floor 0.5 s, at most 4 in parallel).
  Your `fetch_policy` can only make this **stricter** — ask for `min_interval=3.0`
  for a fragile site. A looser request is ignored with a warning.
* **Honest User-Agent** `LogosUpdater/<version> (+<project URL>)`, no user data.
* **robots.txt** is respected; a disallowed URL is never requested.
* **Timeouts and size limits** on every request (`fetch.timeout`, `fetch.max_bytes`);
  `kind="image"` also requires an `image/*` answer.
* **429 / 503** are retried at most 3 times, honouring `Retry-After`; other errors are not retried.
* **Cache:** pages and images with an `ETag`/`Last-Modified` are kept in
  `work/http_cache/` and re-requested conditionally, so a second update downloads
  almost nothing.
* **Budget:** at most `fetch.budget` requests (default 2000) per update; then the
  update stops and says so.

Never create your own `requests.Session` or call `urllib` directly — the contract
tests (`tests/test_source_contract.py`) check the bundled plugins for this. Check
the site's terms of use before you publish a scraper; prefer an official API or a
data file when there is one.

---

## Config block

Select the active source and pass it settings in `config.yaml`:

```yaml
data_source: csv_file         # the active plugin's id
sources:                      # per-source settings, keyed by id
  csv_file:
    path: stations.csv
    default_ecc: E0           # country for rows without an ECC value
    columns:
      id:   ID
      name: Name
      freq: Freq
      link: Link
      logo: Logo
      ecc:  ECC
```

The core resolves this via `sources.get_active(cfg)`, which instantiates the
class whose `id == cfg['data_source']` with `cfg['sources'][id]` as settings.
An unknown id raises a clear error listing the available ids.

---

## Worked example: the CSV source

`sources/csv_file.py` (`id = "csv_file"`) reads stations from a local CSV. It
needs no network, so it's the simplest end-to-end proof of the interface.

Expected CSV (header row required):

```csv
ID,Name,Freq,Link,Logo,ECC
D312,Example FM,98.5,,,E0
D312,Example FM,"101,3",,,E0
D3A0,Example DAB,DAB,,,E0
D401,Example Regional,104.2,,,
```

(also in [`examples/stations.example.csv`](examples/stations.example.csv)). `ID` is
the RDS PI code; a frequency with a comma must be quoted; the last row has no ECC
and gets `default_ecc`.

* `fetch_policy = FetchPolicy(network=False)` — it reads a local file (logo URLs
  from the CSV are downloaded by the core, with the same limits).
* `change_signature()` returns the CSV's mtime, so work only happens when the file changed.
* A missing file raises `SourceError("err.csv_not_found", ..., path=...)`.
* `fetch_stations()` yields one `Station` per row, mapping headers via the
  `columns` config (defaults `ID/Name/Freq/Link/Logo/ECC`). Rows missing `id` or
  `name` are skipped.
* `fetch_logo_url()` returns `""` — logos come straight from the optional
  `logo` column.
* `classify_freq()` is the inherited default.

---

## Add your own source in 3 steps

1. **Copy the template.** `cp sources/example_template.py sources/my_source.py`.
   Rename the class and give it a unique `id` and a friendly `name`.
2. **Implement the methods.** Fill in `fetch_stations` and `change_signature`
   (and `fetch_logo_url` / `classify_freq` if needed), and set `default_ecc` or
   `Station.ecc`. Read your options from `self.settings`; fetch with `self.http`.
   Delete the `listed = False` line. Run `python3 -m unittest tests.test_source_contract`.
3. **Wire it in `config.yaml`.** Set `data_source: my_source` and add a
   `sources: { my_source: { ... } }` block. Restart — the registry
   auto-discovers the new file; no code changes elsewhere.

Verify discovery quickly:

```python
import sources
print(sources.available().keys())          # should include your id
src = sources.get_active({"data_source": "my_source",
                          "sources": {"my_source": {}}})
print(src.id, src.name)
```
