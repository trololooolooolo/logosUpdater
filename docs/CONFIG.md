# Configuration

LogosUpdater is configured with a single YAML file, `config.yaml`, plus a few CLI
flags. The defaults live in `config.py` (`DEFAULTS`) and a documented template ships
as `config.example.yaml`.

## First run

You don't have to create anything. On startup, if `config.yaml` is missing, the app
writes one from the built-in defaults and prints a one-line notice. Edit it and
restart to customize.

To start from the documented template instead:

```bash
cp config.example.yaml config.yaml
```

`config.yaml` is per-user and gitignored; `config.example.yaml` is the shipped doc.

The **⚙ Settings** window in the UI changes `language` and `data_source` (plus the
`sources.csv_file` path / `default_ecc`) without a restart. It rewrites `config.yaml` (comments are lost)
and the first save keeps the original file as `config.yaml.bak` (later saves don't replace it).

## Precedence

Values are deep-merged in this order (later wins):

```
built-in defaults  <  config.yaml  <  CLI flags
```

Deep-merge means a partial `config.yaml` only overrides the leaves you set — e.g.
setting `paths.image` keeps all other paths at their defaults. Lists and scalars
replace; only nested mappings merge.

## Who can use the app

The app has no password. It only answers requests that come from **this computer's own page**:
the `Host` must be `localhost`, an IP address or this computer's name (DNS-rebinding protection),
and a browser `POST` must carry this app's own `Origin` (another web site open in your browser
cannot drive it). With `server.host: 0.0.0.0` other devices on your network open it by IP
(`http://192.168.x.y:8000`); to use a host name instead, list it in `server.allowed_hosts`:

```yaml
server:
  host: 0.0.0.0
  allowed_hosts: [carpc.lan]
```

## Where your data lives

The app keeps everything it writes — `config.yaml`, `work/` (working copies of the car's image and DBs, extracted
images, `.orig/` snapshot for **③ Review changes**), `inputs/`, `usb_package/`, `logos_sync5/`, `logos_styled/`,
`logo_choices.json`, `station_names.json`, `sync_log.txt` — in one **data directory**. Relative `paths` resolve against it.

| How you run it | Data directory |
|---|---|
| From source (`python view_stations.py`) | the folder with `view_stations.py` |
| Ready-made file (`LogosUpdater-windows-x64.exe` / `LogosUpdater-linux-x64` / `LogosUpdater-macos-arm64`) | the folder with the executable — put it in its own folder, not straight on the Desktop |
| Docker (`docker compose up`) | `/data` in the container = the named volume `logosupdater-data` |
| Any of the above with env `LOGOSUPDATER_DATA=/some/dir` | `/some/dir` |

Setting `LOGOSUPDATER_DATA` before the start:

```bash
LOGOSUPDATER_DATA=~/LogosUpdater-data python view_stations.py        # Linux / macOS
```
```bat
set LOGOSUPDATER_DATA=%USERPROFILE%\LogosUpdater-data
python view_stations.py
```
```powershell
$env:LOGOSUPDATER_DATA="$env:USERPROFILE\LogosUpdater-data"
python view_stations.py
```

(Windows: `set` / `$env:` last until that console window is closed.)

To move to another computer or back up your work, copy the whole data directory.

`logos_sync5/` (new logo files you add, upload or download from a data source; the name is historical
and has nothing to do with any other SYNC version), `logos_styled/` (styles) and `logo_choices.json` (🎨 picks) are the
user's logo library: loading another image + DBs (UI ① or `--load`) does not clear them, and the next
normal package builds them into the image. The undo package (loaded files unchanged) ignores them;
a style is removed with 🖌 → Remove style.

USB sticks are detected on Linux (`/media`, `/run/media`), Windows (removable drive letters) and macOS (`/Volumes`).
Inside Docker no stick is visible: take the package as a `.zip` download and unpack it onto the stick.
Writing straight onto a stick is staged: the package goes to `SyncMyMod.new-<stamp>/` on the stick, is fsync'ed and
md5-checked, and only then swapped in (old `SyncMyMod/` → `SyncMyMod.old-<stamp>`, its `backup/` moved into the new
package). A failure before the swap removes the staging folder and leaves the old `SyncMyMod/` untouched. Required free
space = package + 10 MB, plus the package size again when the stick has no complete `SyncMyMod/backup/` (the car writes
its backup there). `SyncMyMod.old-*` folders are never deleted automatically; the result (`old_folders`) and the
"not enough space" error list them.
The kernel mount check `verify_mount.sh` is an optional extra for Linux only; the app does not need it.

## CLI flags

| Flag            | Overrides            | Example                  |
|-----------------|----------------------|--------------------------|
| `--config PATH` | which file to load   | `--config /etc/lu.yaml`  |
| `--lang CODE`   | `language`           | `--lang pl`              |
| `--port N`      | `port`               | `--port 8080`            |
| `--host ADDR`   | `server.host`        | `--host 0.0.0.0`         |
| `--debug`       | enables debug logs   | `--debug`                |
| `--no-browser`  | don't open the UI in the default browser at start | `--no-browser` |
| `--update-on-startup` | `update.on_startup: true` | `--update-on-startup` |
| `--no-update-on-startup` | `update.on_startup: false` | `--no-update-on-startup` |

```bash
python view_stations.py --config config.yaml --lang pl --port 8080 --debug
```

(Inside an activated venv the command is `python` on every system; outside it, `python3` on
Linux/macOS and `py` on Windows.)

**Port.** If the configured port is taken, the app says so in the console and uses the next free
one (`port`+1 … `port`+20); on Windows it binds the port exclusively, so it never shares a port with
another program. The console line `SERVER: http://127.0.0.1:<port>` shows the real address, and the
browser is opened there. To pin a port, use `--port N` or `port: N` in `config.yaml`. The console
shows only that line, warnings and errors (not every HTTP request).

## Settings

| Key                 | Meaning |
|---------------------|---------|
| `language`          | UI language code; must match a `lang/<code>.yaml` catalog (e.g. `en`, `pl`). Unknown keys fall back to English. |
| `port`              | HTTP port for the local server (`http://127.0.0.1:<port>`, default 8000). If it is taken, the next free port up to `port`+20 is used — see the `SERVER:` console line. |
| `server.host`       | Network interface the server listens on. Default `127.0.0.1` — only this computer can open the UI. Set `0.0.0.0` to open it from a phone/another PC on your network; the app then logs a warning, because **anyone on that network can change your loaded databases and logos** (there is no password). Use it only on a network you trust. |
| `update`            | The update switches — see [`update`](#update) below. |
| `data_source`       | Id of the active source plugin: `none` (default — no downloads, preview and manual editing), `radiopolska` (Poland), `csv_file` (your own list) or a plugin you added. |

### `update`

Nothing is fetched unless you ask for it. Three keys:

| Key          | Default | Meaning |
|--------------|---------|---------|
| `on_startup` | `false` | Run an update in the background at launch. Off by default, so the app opens as a **preview** of the frequencies/logos already in your `img` + `db` set. |
| `database`   | `true`  | **Switch 1** — pull station data from the source and write new/changed frequencies into the FM/DAB DBs. |
| `logos`      | `true`  | **Switch 2** — download the logo files missing locally. Reuses the cached `output.csv`, so it does not re-scrape the station list (it only fetches data if that cache is missing). |

`database` and `logos` are independent: either can run alone. They apply to both the
startup update and the **🔄 Update** button, whose two tickboxes are seeded from these
values (your last choice is then remembered per browser).

```yaml
update:
  on_startup: false   # preview mode (default)
  database: true
  logos: true
```

Pre-refactor configs with a top-level `update_on_startup: true` still work — it is
migrated to `update.on_startup` with a one-line notice.

An update runs **in the background**: the **Update from the internet** group shows the
phase, `done/total` and an estimated time left, with a **Stop** button (the API is
`POST /api/update` → `GET /api/update_status`, `POST /api/update_cancel`). What was
downloaded before Stop is kept; the next update continues from the cache.

### `fetch`

How the app talks to external sites (station lists, station pages, logo files). The
defaults are deliberately slow — station sites are small and every user of the app
hits them — so a first full update takes minutes; later ones are fast thanks to the
cache in `work/http_cache/`. Plugins can only make these limits stricter.

| Key | Default | Meaning |
|-----|---------|---------|
| `max_parallel` | `1` | Requests in flight per host (allowed 1–4). The cap is deliberate: station sites are small, every user of the app hits the same ones, and more parallel connections look like an attack, get the app blocked for everyone and gain little because `min_interval` spaces the requests anyway. |
| `min_interval` | `1.0` | Seconds between request starts to one host (minimum 0.5). |
| `timeout` | `20` | Seconds per request. |
| `budget` | `2000` | Max requests per update; the update then stops and says so. `304 Not Modified` answers don't count. |
| `respect_robots` | `true` | Skip URLs the site's `robots.txt` disallows for LogosUpdater. |
| `max_bytes` | page 2 MB, list 10 MB, image 2 MB | Size limit per kind of answer; a logo must be `image/*` (or have no/generic type but image bytes). |
| `max_age_days` | page 7, image 0 | A cached answer younger than this is used without asking the site. Station pages: some sites (radiopolska.pl) ignore conditional requests, so this is what keeps a repeated update light; a station's new logo address is then noticed within a week. |

Requests carry the User-Agent `LogosUpdater/<version> (+<project URL>)` and nothing
about you. Details for plugin authors: [SOURCES.md → Network rules](SOURCES.md#network-rules).

### `paths`

All paths may be relative (to the data directory, see [Where your data lives](#where-your-data-lives)) or absolute.
By default the working copies of the car's files and everything derived from them sit in `work/`. Point these at
custom locations to run against a different image or database set:

| Key         | Meaning |
|-------------|---------|
| `image`     | Source QNX6 image read for the gallery and used as the build base. |
| `image_out` | Output image produced by the build. |
| `db_fm`     | SQLite DB mapping FM stations to logos. |
| `db_dab`    | SQLite DB mapping DAB stations to logos. |
| `db_ocr`    | SQLite DB of OCR results for gallery matching. |
| `logo_dir`  | Directory of new/updated PNG logos (`{ECC}/{NAME}.PNG`). |
| `mount_dir` | Where the loaded image is extracted for the gallery and previews (`<mount_dir>_built` = the built image). |
| `work_dir`  | Scratch directory for building the output image. |
| `inputs_dir` | Input sets: each subdir with a `station_logos.img` (+ FM/DAB DBs, optional `INFO.txt`) is listed under **📂 Input sets** and can be loaded into the working paths without a restart. |
| `logo_choices` | JSON with the per-file version picks from **🎨 Logo versions** (`"E2/NAME.PNG": "image"\|"sync5"\|"styled"`); the build follows it, files without a pick follow the overwrite switch. |
| `station_names` | Station names you type in **➕ Add station** / **✎** (JSON `{"E0/D312": "Radio Test"}`). The car's databases have no name column — the radio shows the RDS name — so these names are only for finding stations in this app. They take precedence over names from a data source. Back it up together with your working files. |
| `styled_dir` | Logos restyled with **🖌 Logo style** (`{ECC}/{NAME}.PNG`, plus `_meta.json` with the source version, parameters and saved presets). Applying a style picks `styled` for those files. |
| `usb_dir` | Where **💾 Prepare USB package** writes the SyncMyMod `.zip` (the 3 newest are kept). |

### `ai` — AI redraw (🖌 Logo style → 🤖)

For logos the local style cannot turn white (marked ⚠). The logo image is sent to the provider
(a third party: its terms of use and privacy policy apply; nothing is sent unless you press 🤖), the
white-on-black answer is keyed locally and styled like the rest. Answers are cached in
`styled_dir/_ai/`, so a repeated preview or apply costs nothing.

| Key | Meaning |
|---|---|
| `provider` | `gemini` (Gemini API, Interactions endpoint) or `kie` (kie.ai: upload → `jobs/createTask` → poll; the upload stays on kie.ai's host for 24 h). |
| `gemini_api_key` / `kie_api_key` | API keys; env `GEMINI_API_KEY` / `KIE_API_KEY` win. Never sent to the browser. |
| `gemini_model` / `kie_model` | Model ids (`gemini-nano-banana-2.1`, `google/nano-banana-edit`). |
| `price_per_image` | `{gemini: 0.034, kie: null}` — USD per image, only for the estimate shown before a run. |
| `workers` | Parallel requests (default 3). |

Example — run against images and DBs kept elsewhere:

```yaml
paths:
  image:     /data/sync3/station_logos.img
  image_out: /data/sync3/station_logos_new.img
  db_fm:     /data/sync3/fm_station_logo.db
  db_dab:    /data/sync3/dab_station_logo.db
```

### `language` and `lang/*.yaml`

`language` selects a translation catalog `lang/<code>.yaml`. `lang/en.yaml` is the
canonical key set (the default); `lang/pl.yaml` reproduces the Polish UI. Lookup order
is *active language -> en -> the key itself*, so a missing translation never crashes —
it just shows English (or the raw key). Switching `language` swaps the whole UI.

### `data_source` and `sources/*`

`data_source` names the active station-data plugin (a `DataSource` under `sources/`).
Its per-source settings live under `sources.<id>`; every source also accepts
`default_ecc` (the RDS country for stations that don't carry one).

| Id            | For whom | Settings |
|---------------|----------|----------|
| `none`        | default; anyone — the update button is hidden | — |
| `radiopolska` | Poland (`default_ecc: E2` built in) | below |
| `csv_file`    | any country — your own station list | `path`, `default_ecc`, `columns` — see `docs/SOURCES.md` and `docs/examples/stations.example.csv` |

The `radiopolska` source exposes:

| Key             | Meaning |
|-----------------|---------|
| `base_url`      | Site root. |
| `list_path`     | Path to the station list (bazapi). |
| `changes_path`  | Path used to detect upstream changes. |
| `logo_selector` | CSS selector for the logo `<img>` on a station page. |

To add your own source, ship a plugin under `sources/` and add a matching
`sources.<your_id>` block, then set `data_source: <your_id>`. See `docs/SOURCES.md`.

## Copy-paste example

```yaml
language: pl
port: 8080
update:
  on_startup: true
  database: true
  logos: false
data_source: radiopolska
paths:
  image:     work/station_logos.img
  image_out: work/station_logos_new.img
  db_fm:     work/fm_station_logo.db
  db_dab:    work/dab_station_logo.db
  db_ocr:    logo_ocr.db
  logo_dir:  logos_sync5
  mount_dir: work/logos_mnt
  work_dir:  work/qnx_merged
  inputs_dir: inputs
sources:
  radiopolska:
    base_url: https://radiopolska.pl
    list_path: /wykaz/bazapi
    changes_path: /wykaz/zmiany
    logo_selector: img.wykaz-logotyp
```

## Loading a fresh image + DBs to edit

The editor works on your car's `station_logos.img` + `fm_station_logo.db` +
`dab_station_logo.db` (found in your mod package under `SyncMyMod/.../other/`).
Load them in one step:

```bash
python view_stations.py --load /path/to/SyncMyMod   # USB / package root / other/ dir
```
```bat
python view_stations.py --load "E:\SyncMyMod\backup"
```

On Windows a quoted path may also end in a backslash (`--load "C:\cars\backup\"`): Windows turns the
closing `\"` into a literal quote and glues the following options to the path; the app splits them
back, so `--load "C:\cars\backup\" --port 8080` works as expected.

`--load DIR` finds `station_logos.img` anywhere under `DIR` (case-insensitive),
copies it and the two DBs into the configured `paths:` targets, and **backs up any
existing targets** as `*.bak-<timestamp>`. It also clears the mount marker so the
gallery re-extracts the new image. `logo_ocr.db` is generated locally and is never
overwritten by `--load`. (Of the optional OCR helpers, `ocr_api_scanner.py` sends logo images to
OCR.space, whose terms apply; `ocr_scanner.py` runs offline.)

Manual alternative: copy the three files into the app folder under their default
names, or set `paths.image` / `paths.db_fm` / `paths.db_dab` to point at them
directly (note: editing then writes those files in place).

## Building the ready-made file

The single-file executables are built with PyInstaller from `packaging/logosupdater.spec`
(as `.github/workflows/release.yml` does). Use **Python 3.12** for this: the pinned
`pyinstaller==6.11.1` has no wheels for newer Pythons (e.g. 3.14), so `pip` reports
*No matching distribution found*. Running the app from source works on Python 3.10+.

```bash
python3.12 -m venv .venv-build && . .venv-build/bin/activate         # Windows: py -3.12 -m venv .venv-build
# no Python 3.12 yet? Windows: winget install Python.Python.3.12 · macOS: brew install python@3.12 · Ubuntu: deadsnakes PPA or uv
pip install -r requirements.txt pyinstaller==6.11.1                  #          .venv-build\Scripts\activate
pyinstaller --noconfirm --clean packaging/logosupdater.spec          # -> dist/
```
