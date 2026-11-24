# LogosUpdater — Architecture

The app manages radio-station logos for a **Ford Sync 3.4 (QNX6)** head unit.
**Data sources are plugins**; the **device side (Ford DB + QNX6 image + USB
package) is fixed** and shared by all sources. All network traffic of the
plugins goes through one polite HTTP client (`fetcher.py`).

```
                     CLI / config.yaml
                           │
        ┌──────────────────▼──────────────────┐
        │ view_stations.py  ->  server.py     │ entry point -> HTTP handler, REST API,
        │ (deps, data dir)     (web/ UI)      │ /logos, /download, request gate
        └──┬──────────┬──────────┬────────────┘
           │          │          │
       config.py   i18n.py    sources/ ── base.py (DataSource, Station, Setting, SourceError)
                   (lang/)       │        radiopolska.py  csv_file.py  none.py  example_template.py
                                 │ self.http
                                 ▼
                             fetcher.py  (rate limit, robots.txt, cache, budget, public URLs only)
                                 │  normalized Station records
                                 ▼
                             ford_db.py  ── update in the background (PROGRESS), Ford DBs,
                                 │          logos_sync5/, station edits, CSV import/export
          logo_style.py ─┐       │
          logo_ai.py ────┼──►  build_logo_img.py ──► qnx6fs.py   (station_logos.img)
          (styled logos) ┘       │
                                 ▼
                           usb_package.py  (SyncMyMod/ package: autoinstall.sh + img + DBs)
```

## Modules

| Module | Role |
|---|---|
| `view_stations.py` | Thin entry point. Installs missing dependencies (from source), changes into the data directory (`config.app_dir()`), loads config, language and the active data source, hands off to `server.run()`. |
| `server.py` | HTTP handler: request gate (Host / Origin / body size), REST API, `/web/*` static assets, `/logos`, `/download`, `render_index()`. Holds `SET_LOCK` (set swap / build = writer). |
| `web/index.html`, `app.css`, `app.js` | Static UI (guided steps ①–④). `data-i18n` chrome + injected `const T`. |
| `config.py` | Built-in `DEFAULTS`, YAML load/save, deep-merge, CLI overrides; data directory (`app_dir()`), read-only SQLite (`connect_ro()`), atomic writes, USB stick detection, loading an input set (`load_inputs()`). Exits with a message if PyYAML is missing. |
| `i18n.py` + `lang/` | Translation. `t(key, **kw)` with `active → en → key` fallback. `lang/en.yaml` is canonical. |
| `sources/base.py` | Plugin contract v2: `DataSource`, `Station`, `FetchPolicy`, `Setting` (settings form), `SourceError`. |
| `sources/__init__.py` | Registry: discovers every `DataSource` subclass under `sources/` (plus `<data dir>/sources/`), binds `http`/`progress`/`cancelled`; `get_active(cfg)`. |
| `sources/radiopolska.py`, `csv_file.py`, `none.py`, `example_template.py` | Reference scraper, local CSV, "no source" (edit only), commented skeleton. |
| `fetcher.py` | The only way plugins reach the network: per-host rate limit, robots.txt, conditional cache in `work/http_cache`, size limits, request budget, public http(s) only (checked on every redirect). |
| `ford_db.py` | **Device side.** `start_update()` / `run_update()` (two switches: database / logos), mapping `Station` records onto the Ford SQLite DBs, logo download + resize into `logos_sync5/`, station add/edit, CSV import/export. |
| `build_logo_img.py`, `qnx6fs.py` | Pure-Python QNX6 image read / merge / build (see below). |
| `logo_style.py`, `logo_ai.py` | Logo restyling with Pillow (presets) and the optional AI redraw (user's own API key, cached). |
| `usb_package.py` | The car package (`SyncMyMod/`, `autoinstall.sh` with backup and rollback), checks before packing, writing to a `.zip` or straight to a stick, the package status report. |
| `compare_sets.py`, `ocr_*.py`, `verify_mount.sh` | Optional helpers: compare two input sets, read logo text (OCR) for gallery suggestions, mount-test an image with the Linux kernel driver. |

## Data flow (end to end)

1. The active `DataSource` is selected from `config.data_source` and built by the registry.
2. The Update button starts `ford_db.start_update(cfg, source, database=..., logos=...)` in a
   background thread; the UI polls `/api/update_status` (phase, ETA) and can stop it.
   With `update.on_startup: false` (the default) nothing runs at launch and the UI is a
   pure **preview** of the loaded img/db set.
3. `ford_db.refresh_station_data(cfg, source)`:
   - `source.change_signature()` → compared with `last_change.txt`; unchanged = no fetching.
   - `source.fetch_stations()` → normalized `Station` records.
   - logo URLs filled via `source.fetch_logo_url()`, **sequentially** through `fetcher` (rate-limited,
     each URL once); records cached to `output.csv`.
4. `ford_db.sync_database()` writes the Ford DBs (`ECC, STATION_ID, RECEIVER_TYPE, FREQUENCY, LOGO_FILENAME`), using `source.classify_freq()` for FM/DAB + 6-digit frequency, plus OCR-assisted logo matching. **Only the app's own rows (`RECEIVER_TYPE` 1/2) are updated — never the Ford originals (`F`/`D`).**
5. `ford_db.download_missing_logos()` downloads missing images (through `fetcher`), resizes to 160×120, writes `logos_sync5/{ECC}/{name}.PNG`.
6. Building calls `build_logo_img`/`qnx6fs` to produce `station_logos_new.img`;
   `usb_package` packs it with the DBs for the car.

The **boundary** is strict: plugins produce *station data*; `ford_db` owns the
*device mapping*; `qnx6fs`/`build_logo_img` own the *image format*.

## Config flow

Precedence: **CLI flag > `config.yaml` > built-in defaults** (deep-merged in
`config.py`). On first run `config.yaml` is written from defaults. Keys are read
by: `view_stations.py` (port, language, paths, update, data_source), `server.py`
(the `update` switches),
`ford_db.py` (`cfg['paths']`), and the active source (`cfg['sources'][id]`).

## i18n flow

`i18n.load(cfg.language)` sets the active catalog. The server builds a merged
flat catalog `T = i18n.catalog()` and:
- ships **static UI** with `data-i18n` attributes that `app.js` fills from the
  injected `const T` on load (`applyI18n()`);
- exposes a JS `tr(key, vars)` helper for **dynamic** toast/confirm messages.
Adding a language = drop `lang/<code>.yaml` (same keys) + set `language: <code>`.
**Logs stay English.**

## Source-plugin discovery

`sources/__init__.py` imports each `sources/*.py`, collects `DataSource`
subclasses by their `id`, and skips any that fail to import (logged, non-fatal).
`config.data_source` chooses the active id; its `sources.<id>` block is passed to
the constructor. See `docs/SOURCES.md`.

## Extension points

- **Add a data source** → `docs/SOURCES.md` (+ `sources/example_template.py`).
- **Add a language** → `docs/I18N.md`.
- **Settings** → `docs/CONFIG.md`.

## Intentionally NOT pluggable

The **QNX6 on-disk format** and the **Ford DB schema** are device-specific and
hard-won; do not abstract them. What matters when you touch them:

- **QNX6 image** (`qnx6fs.py`, pure Python): boot block 0x2000 B, superblock #1 at 0x2000,
  data block N at `0x3000 + N * blocksize`, superblock #2 in the **last 0x1000 bytes of the image**
  (the car's `fs-qnx6` looks for it there); superblock checksum = big-endian CRC-32 (poly
  0x04C11DB7, init 0, no final xor) over bytes 8..512; bitmap bit set = block used; names over
  27 bytes go through the long-file tree. The car's driver is stricter than Linux's: directory
  inodes need the factory `ext_mode`/`status` values, so `QNX6Writer` copies inode metadata per
  path from the loaded image (the template) and builds with block size and allocation group taken
  from it. Never trust an image you did not build: `QNX6Reader` validates sizes, pointers and
  levels, and `extract_to` keeps files inside the target folder.
- **Ford DBs** `fm_station_logo.db` / `dab_station_logo.db`: one table each, columns
  `ECC, STATION_ID, RECEIVER_TYPE, FREQUENCY, LOGO_FILENAME`. `RECEIVER_TYPE` `F`/`D` = Ford's
  rows, `1`/`2` = rows added by this app; `FREQUENCY` = kHz as 6 digits (`098500`), DAB `0`/`000000`;
  `LOGO_FILENAME` = upper-case name without `.PNG`. The radio looks a logo up by frequency + PI.
- **Tests that guard this**: `tests/test_qnx6_roundtrip.py`, `tests/test_qnx6_hostile.py`,
  `tests/test_security_image.py`, `tests/test_car_install.py` (the car-side `autoinstall.sh`
  run in `sh` on a fake car). The final proof is a real car.

## Web layer

`view_stations.py` is now a thin entry point; the HTTP handler lives in
`server.py`, and the UI is static under `web/`:
- `web/index.html` — markup with `data-i18n`/`data-i18n-ph`/`data-i18n-title`
  attributes for chrome, plus `__LANG__` / `__T_JSON__` / `__ROWS__` tokens the
  server substitutes per request (`render_index()`); table rows are built by
  `_build_rows()`.
- `web/app.css`, `web/app.js` — served from `/web/*`. `app.js` applies the
  injected `const T` to `data-i18n` elements on load (`applyI18n()`) and uses
  `tr(key, vars)` for dynamic toast/dialog text.
