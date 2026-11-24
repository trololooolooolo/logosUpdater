# LogosUpdater

## What is this

LogosUpdater changes the radio-station logos (FM/DAB) shown by a **Ford SYNC 3.4** head unit.
You load three files from your car's USB mod package (`station_logos.img`,
`fm_station_logo.db`, `dab_station_logo.db`), change stations and logos in your web browser,
and get a **SyncMyMod USB package** that FMods Tools 2.9+ installs on the car — with a backup
of the car's files made first. It runs on Windows, macOS and Linux; it reads and builds the
car's QNX6 logo image itself (no QNX tools, no virtual machine).

> **Disclaimer.** LogosUpdater is an independent community project. It is not
> affiliated with, endorsed by or supported by Ford Motor Company. *Ford* and
> *SYNC* are trademarks of their respective owners and are used here only to
> describe compatibility. Station logos are trademarks of their broadcasters and
> are **not** distributed with this project. The repository contains no Ford software,
> firmware or data; the app works only on files from your own car.
> Modifying your head unit is entirely at your own risk and may affect your
> warranty. Always keep the backup the USB package makes.

License: [MIT](LICENSE). The release executables also bundle Python and open-source libraries;
their licences are in `THIRD_PARTY_NOTICES.txt`, attached to every release.

## Download

Ready-made files for each system are attached to the [**Releases**](https://github.com/trololooolooolo/logosUpdater/releases) of this GitHub repository:
`LogosUpdater-windows-x64.exe`, `LogosUpdater-linux-x64` and, not in every release, `LogosUpdater-macos-arm64`.
Put the file in its own folder and start it — the user guide explains the first start on
each system. Docker: `docker compose up -d`, then open <http://localhost:8000>.
No file for your system there yet? Use the green **Code → Download ZIP** button and run it
from source (see the user guide).

## For users

- **[User guide (English)](docs/USER_GUIDE.md)** — what you need, install, the four steps,
  putting it on the car, how to undo, FAQ.
- **[Instrukcja (polski)](docs/USER_GUIDE.pl.md)** — to samo po polsku.
- [Glossary / Słowniczek](docs/GLOSSARY.md) — ECC, PI, RDS, SyncMyMod, FMods… in plain words.

## For developers

```bash
python3 -m venv .venv && . .venv/bin/activate   # Python 3.10+; Ubuntu 23.04+/Debian 12+ need a venv
pip install -r requirements.txt   # also auto-installed on first run (inside a venv)
python view_stations.py           # serves http://127.0.0.1:8000 (next free port if taken) and opens the browser
python view_stations.py --load /path/to/SyncMyMod   # load a car's files from the CLI, then edit
python -m unittest discover -s tests
```

Windows (Command Prompt; in PowerShell the same lines work):

```bat
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python view_stations.py
python -m unittest discover -s tests
```

The console line `SERVER: http://127.0.0.1:<port>` shows the real address; choose the port with
`--port N` or `port:` in `config.yaml`.

**Building the .exe / single-file executables** (`packaging/logosupdater.spec`, PyInstaller 6.11.1)
needs **Python 3.12**, as in `.github/workflows/release.yml` — PyInstaller 6.11.1 has no wheels for
Python 3.14. See [CONFIG.md — Building the ready-made file](docs/CONFIG.md#building-the-ready-made-file).

On first run `config.yaml` is created from defaults (precedence: CLI > `config.yaml` > defaults).
Nothing is downloaded at startup unless `update.on_startup: true`. Station data comes from an
optional **data-source plugin** (`none` by default; built in: `radiopolska`, `csv_file`).

`source.fetch_stations()` → `ford_db` writes the Ford DBs + downloads logos →
`build_logo_img`/`qnx6fs` build `station_logos_new.img` → `usb_package` packs it.

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — modules + data flow
- [`docs/CONFIG.md`](docs/CONFIG.md) — every setting, CLI flags, `--load`, where data lives
- [`docs/SOURCES.md`](docs/SOURCES.md) — write a data-source plugin (CSV format, validation)
- [`docs/I18N.md`](docs/I18N.md) — add a UI language (`lang/<code>.yaml`)
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — how to report bugs and contribute
