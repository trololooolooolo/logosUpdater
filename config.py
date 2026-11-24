"""Config layer for LogosUpdater.

Built-in DEFAULTS + YAML load/save + deep-merge + CLI overrides.
Precedence: CLI flag > config.yaml > built-in defaults.

Stdlib + PyYAML only. See docs/CONFIG.md.
"""
import os
import sys
import time

try:
    import yaml
except ImportError:
    # Dependencies are installed by view_stations.py from requirements.txt; a tool
    # importing config directly gets a clear message instead of a silent pip run.
    sys.exit("PyYAML is missing. Run:  pip install -r requirements.txt")


def app_dir():
    """Directory that holds the user's data (config.yaml, work/, inputs/, ...).

    Next to the executable in a PyInstaller bundle, next to this file otherwise.
    Env LOGOSUPDATER_DATA overrides it (Docker). Every relative path in the config
    resolves against it (the launcher chdirs here).
    """
    if os.environ.get('LOGOSUPDATER_DATA'):
        return os.path.abspath(os.environ['LOGOSUPDATER_DATA'])
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


DEFAULTS = {
    'language': 'en',            # ui language; matches a lang/<code>.yaml
    'port': 8000,
    'server': {
        # 127.0.0.1 = only this computer. 0.0.0.0 = every device on your network can
        # open the UI and change the loaded DBs/logos (there is no password).
        'host': '127.0.0.1',
    },
    'update': {                  # what an update does; two independent switches
        'on_startup': False,     # off by default: launch straight into preview of the current img/db set
        'database': True,        # switch 1: refresh the DBs with new/changed frequencies
        'logos': True,           # switch 2: download missing logo files
    },
    'data_source': 'none',       # active source plugin: none | radiopolska (Poland) | csv_file | ...
    'paths': {
        'image': 'work/station_logos.img',
        'image_out': 'work/station_logos_new.img',
        'db_fm': 'work/fm_station_logo.db',
        'db_dab': 'work/dab_station_logo.db',
        'db_ocr': 'logo_ocr.db',
        'logo_dir': 'logos_sync5',
        'mount_dir': 'work/logos_mnt',    # where the loaded image is extracted for the gallery/preview
        'work_dir': 'work/qnx_merged',
        'inputs_dir': 'inputs',      # read-only input sets (img + FM/DAB DBs), one per subdir
        'usb_dir': 'usb_package',    # zipped SyncMyMod USB packages ("Prepare USB package")
        'logo_choices': 'logo_choices.json',  # per-file image/logos_sync5/styled version picks (UI "Logo versions")
        'styled_dir': 'logos_styled',         # logos restyled in the UI ("Logo style"), a third version per file
        'station_names': 'station_names.json',  # display names typed in the editor ({"E0/D312": "Name"}); never written to the DBs
    },
    'ai': {                      # "🖌 Logo style" AI redraw for logos the local filters can't handle (logo_ai.py)
        'provider': 'gemini',    # gemini | kie
        'gemini_api_key': '',    # or env GEMINI_API_KEY
        'gemini_model': 'gemini-nano-banana-2.1',
        'kie_api_key': '',       # or env KIE_API_KEY
        'kie_model': 'google/nano-banana-edit',
    },
    'fetch': {                   # how the app talks to external sites (source updates, logo downloads; fetcher.py)
        # requests in flight per host; capped at 4 (fetcher.HARD_MAX_PARALLEL): station sites are small
        # volunteer-run servers shared by every user of the app - more parallel connections look like a DoS,
        # get the User-Agent blocked for everyone and gain little, since min_interval spaces requests anyway
        'max_parallel': 1,
        'min_interval': 1.0,     # seconds between request starts to one host (min 0.5)
        'timeout': 20,           # seconds per request
        'budget': 2000,          # max requests per update run (304 answers don't count)
        'respect_robots': True,  # skip URLs the site's robots.txt disallows for LogosUpdater
        'public_only': True,     # never fetch from this computer / the local network (SSRF guard)
        'max_bytes': {'page': 2000000, 'list': 10000000, 'image': 2000000},  # response size limit per kind
        # days a cached answer is used without asking the site again (station pages: some sites, like
        # radiopolska.pl, ignore conditional requests, so only this keeps a repeated update light)
        'max_age_days': {'page': 7, 'image': 0},
    },
    'sources': {                 # per-source settings, keyed by source id
        'radiopolska': {
            'base_url': 'https://radiopolska.pl',
            'list_path': '/wykaz/bazapi',
            'changes_path': '/wykaz/zmiany',
            'logo_selector': 'img.wykaz-logotyp',
        },
    },
}


def deep_merge(base, override):
    """Recursively merge override into base (nested dicts only; lists/scalars replace).

    Returns a new dict; inputs are not mutated. None values in override are skipped.
    """
    out = dict(base)
    for k, v in (override or {}).items():
        if v is None:
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path='config.yaml', cli_overrides=None):
    """Load config: DEFAULTS <- yaml file (if any) <- cli_overrides.

    If `path` does not exist, write it from DEFAULTS and print a notice.
    A missing/corrupt yaml falls back to DEFAULTS with a warning (never crashes).
    """
    file_cfg = {}
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                loaded = yaml.safe_load(f)
            if isinstance(loaded, dict):
                file_cfg = loaded
            elif loaded is not None:
                print(f"Warning: {path} is not a mapping; ignoring it and using defaults.")
        except Exception as e:
            print(f"Warning: could not parse {path} ({e}); using defaults.")
    else:
        try:
            with open(path, 'w', encoding='utf-8') as f:
                yaml.safe_dump(DEFAULTS, f, default_flow_style=False, sort_keys=False,
                               allow_unicode=True)
            print(f"No {path} found; wrote one from defaults. Edit it to customize.")
        except Exception as e:
            print(f"Warning: could not write {path} ({e}); using defaults.")

    cfg = deep_merge(DEFAULTS, _migrate_legacy(file_cfg))
    cfg = deep_merge(cfg, cli_overrides)
    cfg.pop('update_on_startup', None)
    return cfg


def _migrate_legacy(file_cfg):
    """Accept pre-refactor keys so an old config.yaml keeps working.

    `update_on_startup: true` -> `update.on_startup: true`. Applied to the FILE
    layer only, so a new-style `update:` block and CLI flags both still win.
    """
    legacy = file_cfg.get('update_on_startup')
    if legacy is None:
        return file_cfg
    out = {k: v for k, v in file_cfg.items() if k != 'update_on_startup'}
    if 'update' not in out:
        out['update'] = {'on_startup': bool(legacy)}
        print("Note: 'update_on_startup' is deprecated; use the 'update:' block "
              "(on_startup / database / logos). See docs/CONFIG.md.")
    return out


def update_flags(cfg):
    """Return the (database, logos) update switches as booleans."""
    u = cfg.get('update') or {}
    return bool(u.get('database', True)), bool(u.get('logos', True))


def add_config_cli_args(parser):
    """Register the standard config-related CLI flags on an argparse parser."""
    parser.add_argument('--config', metavar='PATH', default='config.yaml',
                        help='path to config.yaml (default: config.yaml)')
    parser.add_argument('--lang', metavar='CODE', default=None,
                        help='UI language code, overrides config (e.g. en, pl)')
    parser.add_argument('--port', type=int, default=None,
                        help='HTTP port, overrides config')
    parser.add_argument('--host', default=None,
                        help='interface to listen on, overrides server.host (default 127.0.0.1; '
                             '0.0.0.0 = whole network, no password)')
    parser.add_argument('--debug', action='store_true',
                        help='enable debug logging')
    parser.add_argument('--no-browser', dest='no_browser', action='store_true',
                        help='do not open the UI in the default browser at start')
    grp = parser.add_mutually_exclusive_group()
    grp.add_argument('--update-on-startup', dest='update_on_startup',
                     action='store_true', default=None,
                     help='run an update at launch (overrides update.on_startup)')
    grp.add_argument('--no-update-on-startup', dest='update_on_startup',
                     action='store_false', default=None,
                     help='start in preview mode: no update at launch (default)')
    parser.add_argument('--load', metavar='DIR', default=None,
                        help='load station_logos.img + the FM/DAB .db files from a car '
                             'package directory into the working files (backs up existing), then run')
    return parser


# Files copied by --load, mapped to their config `paths` key. Only what a car
# package's other/ directory contains; logo_ocr.db is generated locally, not loaded.
INPUT_FILES = {
    'station_logos.img': 'image',
    'fm_station_logo.db': 'db_fm',
    'dab_station_logo.db': 'db_dab',
}


def _ci_find(directory, name):
    """Case-insensitive lookup of `name` in `directory`; returns the path or None."""
    low = name.lower()
    try:
        for f in os.listdir(directory):
            if f.lower() == low:
                return os.path.join(directory, f)
    except OSError:
        pass
    return None


def _locate_input_dir(src):
    """Accept the package root / USB and find the dir that holds station_logos.img."""
    if _ci_find(src, 'station_logos.img'):
        return src
    for root, _dirs, files in os.walk(src):
        if any(f.lower() == 'station_logos.img' for f in files):
            return root
    return src


def drop_cleared(cfg, changes):
    """Remove the keys that `changes` sets to None (deep_merge skips them). Mutates and returns cfg."""
    for k, v in (changes or {}).items():
        if v is None:
            cfg.pop(k, None)
        elif isinstance(v, dict) and isinstance(cfg.get(k), dict):
            drop_cleared(cfg[k], v)
    return cfg


def save_partial(path, changes):
    """Write only `changes` (nested dict) into the YAML file at `path`, keeping every
    other key; a None value removes that key. Comments are lost (PyYAML), so the first
    save keeps `<path>.bak`."""
    import shutil
    cur = {}
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            cur = yaml.safe_load(f) or {}
        if not isinstance(cur, dict):
            raise ValueError(f"{path} is not a mapping")
        if not os.path.exists(path + '.bak'):
            shutil.copy2(path, path + '.bak')
    out = drop_cleared(deep_merge(cur, changes), changes)
    atomic_write(path, yaml.safe_dump(out, default_flow_style=False, sort_keys=False, allow_unicode=True))
    return out


# Where a car's USB stick gets mounted (Linux desktop auto-mount).
USB_ROOTS = ('/media/', '/run/media/')


def _linux_mounts():
    with open('/proc/mounts', encoding='utf-8') as f:
        rows = [ln.split() for ln in f]
    return [{'path': r[1].replace('\\040', ' '), 'fstype': r[2]}
            for r in rows if len(r) > 2 and r[1].startswith(USB_ROOTS)]


def _windows_mounts():
    import ctypes
    k32 = ctypes.windll.kernel32
    mask, out = k32.GetLogicalDrives(), []
    for i in range(26):
        if not mask & (1 << i):
            continue
        root = f"{chr(65 + i)}:\\"
        if k32.GetDriveTypeW(root) != 2:          # DRIVE_REMOVABLE
            continue
        fs = ctypes.create_unicode_buffer(32)
        ok = k32.GetVolumeInformationW(root, None, 0, None, None, None, fs, 32)
        if ok:                                    # 0 = no medium in the slot
            out.append({'path': root, 'fstype': fs.value.lower()})
    return out


def _mac_mounts():
    import subprocess
    # "/dev/disk4s1 on /Volumes/NO NAME (msdos, local, nodev, ...)"
    txt = subprocess.run(['mount'], capture_output=True, text=True, timeout=5).stdout
    out = []
    for ln in txt.splitlines():
        if ' on /Volumes/' not in ln or ' (' not in ln:
            continue
        mnt, opts = ln.split(' on ', 1)[1].rsplit(' (', 1)
        if os.path.realpath(mnt) == '/':          # the system volume's link
            continue
        out.append({'path': mnt, 'fstype': opts.split(',')[0].strip()})
    return out


def removable_mounts():
    """Mounted removable drives as [{'path', 'fstype'}] on Linux (/media, /run/media),
    Windows (removable drive letters) and macOS (/Volumes). Empty where nothing can be
    detected (e.g. inside Docker) - callers then offer the .zip download only."""
    try:
        if sys.platform.startswith('win'):
            return _windows_mounts()
        if sys.platform == 'darwin':
            return _mac_mounts()
        return _linux_mounts()
    except Exception:  # noqa: BLE001 - detection must never break the UI
        return []


def mount_label(mount):
    """Short name of a stick for the UI: last folder of /media/me/STICK, or the drive "E:" on Windows
    (os.path.basename('E:\\') is '')."""
    return os.path.basename(mount.rstrip('/\\')) or mount.rstrip('/\\') or mount


_SKIP_DIRS = {'$recycle.bin', 'system volume information', '.spotlight-v100', '.trashes', '.fseventsd',
              'lost+found', '.trash-1000'}


def usb_input_dirs(max_depth=4, max_dirs=2000):
    """Mounted removable drives that hold a station_logos.img (a SyncMyMod package or
    a plain copy): [{'path': dir with the files, 'mount': mount point}]."""
    out = []
    for m in removable_mounts():
        mnt = m['path']
        visited = 0
        for root, dirs, files in os.walk(mnt):
            visited += 1
            if visited > max_dirs:              # a big data stick: don't freeze the ① dialog
                break
            rel = os.path.relpath(root, mnt)
            if rel != '.' and rel.count(os.sep) + 1 >= max_depth:
                dirs[:] = []
            # system / trash folders never hold a car package; SyncMyMod first, the rest after
            dirs[:] = sorted((d for d in dirs if d.lower() not in _SKIP_DIRS and not d.startswith('.')),
                             key=lambda d: (d.lower() != 'syncmymod', d.lower()))
            if any(f.lower() == 'station_logos.img' for f in files):
                out.append({'path': root, 'mount': mnt})
                dirs[:] = []
    return out


def _orig_dir(cfg):
    """Pristine copies of the DBs as loaded (for "Review changes")."""
    return os.path.join(os.path.dirname(os.path.abspath(cfg['paths']['image'])), '.orig')


def loaded_original(cfg, name):
    """Path of the as-loaded copy of `name` (fm/dab .db), or None."""
    p = os.path.join(_orig_dir(cfg), name)
    if os.path.exists(p):
        return p
    prev = active_input_dir(cfg)
    return _ci_find(prev, name) if prev else None


def loaded_info(cfg):
    """{'kind': set|usb|upload|cli, 'label', 'path', 'time'} of the last load, or {}."""
    import json
    marker = _active_marker(cfg)
    try:
        with open(marker + '.json', encoding='utf-8') as f:
            info = json.load(f)
        if isinstance(info, dict) and info.get('path') == active_input_dir(cfg):
            return info
    except (OSError, ValueError):
        pass
    path = active_input_dir(cfg)
    if not path:
        return {}
    inputs = os.path.abspath(cfg['paths'].get('inputs_dir') or 'inputs')
    kind = ('set' if path.startswith(inputs + os.sep) else
            'usb' if path.startswith(USB_ROOTS) else 'cli')
    try:
        t = time.strftime('%Y-%m-%d %H:%M', time.localtime(os.path.getmtime(marker)))
    except OSError:
        t = ''
    return {'kind': kind, 'label': os.path.basename(path), 'path': path, 'time': t}


class InputError(ValueError):
    """The car files are incomplete or damaged. `code`: missing | bad_img | bad_db; `file` names it."""

    def __init__(self, code, file, table=''):
        super().__init__(f"{code}: {file}" + (f" (table {table})" if table else ''))
        self.code, self.file, self.table = code, file, table


def atomic_write(path, data):
    """Write bytes/str to `path` atomically: a unique temp file in the same folder (two requests
    saving at once never share one "<path>.tmp"), fsync, then os.replace. On Windows a reader that
    has the old file open makes os.replace fail for a moment, so it is retried briefly."""
    import tempfile
    import time as _time
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix='.' + os.path.basename(path) + '.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data.encode('utf-8') if isinstance(data, str) else data)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(10):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 9:
                    raise
                _time.sleep(0.05 * (attempt + 1))
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def connect_ro(path):
    """Open an SQLite DB read-only. The file: URI is built with proper escaping: a raw
    f"file:{path}?mode=ro" breaks on '#' or '%XX' in the path (the rest, including ?mode=ro,
    is cut off - SQLite then even creates an empty file under the cut name)."""
    import pathlib
    import sqlite3
    return sqlite3.connect(pathlib.Path(os.path.abspath(path)).as_uri() + '?mode=ro', uri=True)


def prune_backups(path, keep=2):
    """Keep only the `keep` newest `<path>.bak-<stamp>` files (each image backup is ~150 MB)."""
    d, base = os.path.split(os.path.abspath(path))
    try:
        baks = sorted(n for n in os.listdir(d)              # .bak-<YYYYMMDD-HHMMSS>, not .bak-del-*
                      if n.startswith(base + '.bak-') and n[len(base) + 5:len(base) + 6].isdigit())
    except OSError:
        return
    for n in baks[:-keep]:
        try:
            os.remove(os.path.join(d, n))
        except OSError:
            pass


def check_input_files(paths):
    """Refuse anything that is not a complete QNX6 logo image + the two Ford station DBs.

    `paths` = {'image': ..., 'fm': ..., 'dab': ...}. The same check guards browser uploads, USB
    sticks, input sets and --load: a truncated copy (pulled stick, interrupted download) must never
    become the working set or, worse, the "unchanged original" of an undo package.
    """
    import sqlite3
    names = {'image': 'station_logos.img', 'fm': 'fm_station_logo.db', 'dab': 'dab_station_logo.db'}
    for field, fname in names.items():
        p = paths.get(field)
        if not p or not os.path.isfile(p) or not os.path.getsize(p):
            raise InputError('missing', fname)
    try:
        import qnx6fs
        if hasattr(qnx6fs, 'validate_image'):
            qnx6fs.validate_image(paths['image'])          # complete, sane and not empty
        else:
            with qnx6fs.QNX6Reader(paths['image']) as r:
                if not r.checksum_ok:
                    raise ValueError('checksum')
    except Exception as e:  # noqa: BLE001 - any failure = not a usable image
        raise InputError('bad_img', names['image']) from e
    for field, table in (('fm', 'fm_station_logo'), ('dab', 'dab_station_logo')):
        try:
            c = connect_ro(paths[field])
            try:
                res = c.execute('PRAGMA integrity_check').fetchone()[0]
                has = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            finally:
                c.close()
        except sqlite3.DatabaseError:
            res, has = 'not a database', None
        if res != 'ok' or not has:
            raise InputError('bad_db', names[field], table)


def load_inputs(cfg, src, origin=None):
    """Copy editable inputs (image + FM/DAB DBs) from a car-package dir into the
    configured working paths, backing up any existing targets first (skipped
    for a target still identical to the file it was loaded from).

    Case-insensitive; `src` may be the package root, the USB mount, or the
    `other/` dir itself. Returns the list of copied filenames. Prints an English
    summary. Forces the image gallery to re-extract on next start.
    `origin` = (kind, label) recorded for the UI ("Editing: …"); default from the path.
    The loaded DBs are also kept in `.orig/` next to the working image, so the
    changes can be reviewed even after the stick is gone.
    """
    import shutil
    import time
    src = os.path.abspath(os.path.expanduser(src))
    real = _locate_input_dir(src)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    copied, missing = [], []
    prev = active_input_dir(cfg)
    found = {name: _ci_find(real, name) for name in INPUT_FILES}
    missing = [n for n, p in found.items() if not p]
    if missing:
        # never mix one car's image with another car's databases
        raise InputError('missing', ', '.join(missing))
    check_input_files({'image': found['station_logos.img'], 'fm': found['fm_station_logo.db'],
                       'dab': found['dab_station_logo.db']})
    # 1) copy everything next to the targets first (a read error half way leaves the working set as it was)
    staged = []
    try:
        for name, key in INPUT_FILES.items():
            sp, target = found[name], cfg['paths'][key]
            if os.path.abspath(sp) == os.path.abspath(target):
                continue
            os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
            # copyfile, not copy2: inputs/ sets are read-only, the working copy must stay writable
            shutil.copyfile(sp, target + '.new')
            st = os.stat(sp)
            os.utime(target + '.new', (st.st_atime, st.st_mtime))
            staged.append((name, target))
    except BaseException:
        for _n, target in staged:
            try:
                os.remove(target + '.new')
            except OSError:
                pass
        raise
    # 2) back up what is replaced, then swap the three in quickly
    for name, target in staged:
        if os.path.exists(target):
            # back up the working file unless it is still identical to the set it came from
            # (named prev_src: `origin` is the (kind, label) parameter used below)
            prev_src = _ci_find(prev, name) if prev else None
            if not (prev_src and _same_file(prev_src, target)):
                shutil.copy2(target, f"{target}.bak-{stamp}")
                prune_backups(target)
    for name, target in staged:
        os.replace(target + '.new', target)
        copied.append(name)
    if copied:
        import json
        try:
            with open(_active_marker(cfg), 'w', encoding='utf-8') as f:
                f.write(real)
            kind, label = origin or (None, None)
            info = loaded_info(cfg) if not kind else {}
            info.update({'path': real, 'time': time.strftime('%Y-%m-%d %H:%M')})
            if kind:
                info.update({'kind': kind, 'label': label or os.path.basename(real)})
            with open(_active_marker(cfg) + '.json', 'w', encoding='utf-8') as f:
                json.dump(info, f, ensure_ascii=False)
        except OSError:
            pass
        od = _orig_dir(cfg)
        os.makedirs(od, exist_ok=True)
        for name in ('fm_station_logo.db', 'dab_station_logo.db'):
            sp = _ci_find(real, name)
            if sp:
                shutil.copyfile(sp, os.path.join(od, name))
            elif os.path.exists(os.path.join(od, name)):
                os.remove(os.path.join(od, name))
    # drop the mount marker so the swapped image is re-extracted for the gallery
    marker = os.path.join(cfg['paths']['mount_dir'], '.extracted_from')
    if os.path.exists(marker):
        try:                    # mark stale (not delete: the marker also says "this folder is ours")
            with open(marker, 'w', encoding='utf-8') as f:
                f.write('stale')
        except OSError:
            pass
    msg = f"Loaded {len(copied)} file(s) from {real}: {', '.join(copied) or '(none)'}"
    if missing:
        msg += f"  [not found: {', '.join(missing)}]"
    print(msg)
    return copied


def _db_content(path):
    """Sorted rows of every table (SQLite bytes change on a mere write-probe)."""
    import sqlite3
    c = connect_ro(path)
    try:
        tables = [r[0] for r in c.execute(
            "select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name")]
        return [(t, sorted(c.execute(f'select * from "{t}"').fetchall(), key=repr)) for t in tables]
    finally:
        c.close()


def _same_file(a, b):
    """True if both files hold the same data (for .db: the same rows)."""
    import filecmp
    try:
        if filecmp.cmp(a, b, shallow=False):
            return True
        if a.lower().endswith('.db'):
            return _db_content(a) == _db_content(b)
        return False
    except Exception:
        return False


def _active_marker(cfg):
    """File next to the working image that records which input dir was loaded last."""
    return os.path.join(os.path.dirname(os.path.abspath(cfg['paths']['image'])), '.loaded_from')


def active_input_dir(cfg):
    """Absolute path of the input dir loaded last (or '' if unknown)."""
    try:
        with open(_active_marker(cfg), encoding='utf-8') as f:
            return f.read().strip()
    except OSError:
        return ''


_SET_INFO_CACHE = {}


def _image_version(img):
    """'Version Number' from the image's Version.inf (cached per path+mtime+size)."""
    st = os.stat(img)
    key = (img, st.st_mtime, st.st_size)
    if key not in _SET_INFO_CACHE:
        version = ''
        try:
            from qnx6fs import QNX6Reader, ROOT_INO
            r = QNX6Reader(img)
            try:
                for name, ino in r.read_dir(r.get_inode(ROOT_INO)):
                    if name == 'Version.inf':
                        txt = r.read_inode_data(r.get_inode(ino)).decode('latin-1')
                        for line in txt.splitlines():
                            if line.lower().startswith('version number'):
                                version = line.split('=', 1)[1].strip()
            finally:
                r.close()
        except Exception as e:
            version = f'? ({e.__class__.__name__})'
        _SET_INFO_CACHE[key] = version
    return _SET_INFO_CACHE[key]


def _db_rows(db):
    import sqlite3
    try:
        c = connect_ro(db)
        try:
            names = [r[0] for r in c.execute("select name from sqlite_master where type='table'")]
            t = next((n for n in names if n in ('fm_station_logo', 'dab_station_logo')), None)
            return c.execute(f'select count(*) from "{t}"').fetchone()[0] if t else None
        finally:
            c.close()
    except Exception:
        return None


def list_input_sets(cfg):
    """Subdirs of paths.inputs_dir that hold a station_logos.img, newest name last.

    Each entry: name, path, version (from Version.inf), image size/mtime, FM/DAB
    row counts, first line of INFO.txt, and whether it is the last loaded set.
    """
    base = os.path.abspath(cfg['paths'].get('inputs_dir') or 'inputs')
    active = active_input_dir(cfg)
    sets = []
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return base, sets
    for name in names:
        d = os.path.join(base, name)
        img = _ci_find(d, 'station_logos.img') if os.path.isdir(d) else None
        if not img:
            continue
        fm, dab = _ci_find(d, 'fm_station_logo.db'), _ci_find(d, 'dab_station_logo.db')
        info = ''
        info_f = _ci_find(d, 'INFO.txt')
        if info_f:
            with open(info_f, encoding='utf-8', errors='replace') as f:
                info = f.read().strip()
        st = os.stat(img)
        sets.append({'name': name, 'path': d, 'version': _image_version(img),
                     'img_mb': round(st.st_size / 1048576, 1),
                     'img_date': time.strftime('%Y-%m-%d', time.localtime(st.st_mtime)),
                     'fm_rows': _db_rows(fm) if fm else None,
                     'dab_rows': _db_rows(dab) if dab else None,
                     'info': info, 'active': os.path.abspath(d) == active})
    return base, sets


def cli_overrides_from_args(args):
    """Map an argparse Namespace to the nested override dict (unset values omitted)."""
    overrides = {}
    lang = getattr(args, 'lang', None)
    if lang is not None:
        overrides['language'] = lang
    port = getattr(args, 'port', None)
    if port is not None:
        overrides['port'] = port
    host = getattr(args, 'host', None)
    if host:
        overrides['server'] = {'host': host}
    on_startup = getattr(args, 'update_on_startup', None)
    if on_startup is not None:
        overrides['update'] = {'on_startup': on_startup}
    return overrides


if __name__ == '__main__':
    import tempfile

    tmpdir = tempfile.mkdtemp(prefix='logos_cfg_')
    cfg_path = os.path.join(tmpdir, 'config.yaml')

    # First load: file absent -> written from defaults.
    cfg1 = load_config(cfg_path)
    assert os.path.exists(cfg_path), "config.yaml should be written on first run"
    assert cfg1 == DEFAULTS, "fresh load should equal DEFAULTS"

    # Deep-merge: a partial file overrides only the named leaves.
    with open(cfg_path, 'w', encoding='utf-8') as f:
        yaml.safe_dump({'port': 9001, 'paths': {'image': 'custom.img'}}, f)
    cfg2 = load_config(cfg_path)
    assert cfg2['port'] == 9001, "file should override port"
    assert cfg2['paths']['image'] == 'custom.img', "file should override one path"
    assert cfg2['paths']['db_fm'] == DEFAULTS['paths']['db_fm'], "other paths keep defaults"
    assert cfg2['language'] == 'en', "untouched keys keep defaults"

    # CLI override precedence: CLI > file.
    cfg3 = load_config(cfg_path, cli_overrides={'language': 'pl', 'port': 7000})
    assert cfg3['language'] == 'pl', "CLI should override language"
    assert cfg3['port'] == 7000, "CLI should override file port"
    assert cfg3['paths']['image'] == 'custom.img', "file value survives unrelated CLI override"

    # Server host: loopback by default, --host overrides.
    assert cfg3['server']['host'] == '127.0.0.1', "server must default to loopback"
    import argparse as _ap
    _ns = add_config_cli_args(_ap.ArgumentParser()).parse_args(['--host', '0.0.0.0'])
    assert load_config(cfg_path, cli_overrides_from_args(_ns))['server']['host'] == '0.0.0.0'

    # None overrides are ignored.
    cfg4 = load_config(cfg_path, cli_overrides={'language': None})
    assert cfg4['language'] == 'en', "None override must be ignored"

    # Update switches: preview by default, both sub-switches on.
    assert cfg4['update'] == {'on_startup': False, 'database': True, 'logos': True}
    assert update_flags(cfg4) == (True, True)

    # A partial update block only overrides the named switch.
    with open(cfg_path, 'w', encoding='utf-8') as f:
        yaml.safe_dump({'update': {'on_startup': True, 'logos': False}}, f)
    cfg5 = load_config(cfg_path)
    assert cfg5['update']['on_startup'] is True and cfg5['update']['logos'] is False
    assert cfg5['update']['database'] is True, "unset switch keeps its default"
    assert update_flags(cfg5) == (True, False)

    # Legacy key migrates to update.on_startup and disappears.
    with open(cfg_path, 'w', encoding='utf-8') as f:
        yaml.safe_dump({'update_on_startup': True}, f)
    cfg6 = load_config(cfg_path)
    assert cfg6['update']['on_startup'] is True, "legacy update_on_startup should migrate"
    assert 'update_on_startup' not in cfg6, "legacy key must be dropped"
    # ...and CLI still wins over it.
    cfg7 = load_config(cfg_path, cli_overrides={'update': {'on_startup': False}})
    assert cfg7['update']['on_startup'] is False, "CLI must override the legacy key"

    print("Merged config:")
    print(yaml.safe_dump(cfg3, default_flow_style=False, sort_keys=False, allow_unicode=True))
    print("config.py self-test OK")
