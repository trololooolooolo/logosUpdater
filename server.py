"""server.py — HTTP server + request handler for LogosUpdater.

Serves the web UI (web/index.html|app.css|app.js), the REST API, logo images and
the built .img download. Uses the runtime context (config, active data source,
i18n catalog) set by `run()`. Device/data work is delegated to ford_db.py.
"""

import http.server
import os
import sys
import html
import json
import csv
import re
import time
import shutil
import hashlib
import base64
import threading
import sqlite3
import logging

import requests
import config
import i18n
import ford_db
import sources
import usb_package
import logo_style
import logo_ai
from PIL import Image
from ford_db import safe_join, resize_image

try:
    from qnx6fs import QNX6Reader, QNX6Writer
    from build_logo_img import merge as merge_logos
    QNX6_AVAILABLE = True
except Exception as _e:
    QNX6_AVAILABLE = False
    _QNX6_IMPORT_ERR = _e

logger = logging.getLogger(__name__)

# --- runtime context (populated by run()) ---
CFG = {}
SOURCE = None
T = {}                       # merged i18n catalog (+ computed _btn_update), injected into the page
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web')
INDEX_TEMPLATE = ''
# config-derived path constants (set in run())
PORT = 8000
LOGO_DIR_NAME = 'logos_sync5'
LOGO_DIR = MOUNT_DIR = DB_FM = DB_DAB = DB_OCR = IMG_ORIG = IMG_OUT = WORK_DIR = ''
# extracted-image state: cache-busting token for logo URLs + {ECC: {NAME.PNG upper: real name}}
IMG_VER = ''
IMG_INDEX = {}
# held while the working set is swapped (/api/load_inputs) or an image is built
class SetLock:
    """Writer lock for swapping the working set / building the image, plus shared readers.

    Writers (`acquire(blocking=False)` -> False if another writer runs) wait for the
    running readers to finish; readers (`try_read()`) are refused while a writer runs,
    but never block each other (the page asks pkg_status and changes at the same time)."""
    def __init__(self):
        self._w = threading.Lock()
        self._cv = threading.Condition()
        self._readers = 0

    def acquire(self, blocking=True):
        if not self._w.acquire(blocking):
            return False
        with self._cv:
            while self._readers:
                self._cv.wait()
        return True

    def release(self):
        self._w.release()

    def locked(self):
        return self._w.locked()

    def try_read(self):
        with self._cv:
            if self._w.locked():
                return False
            self._readers += 1
            return True

    def end_read(self):
        with self._cv:
            self._readers -= 1
            self._cv.notify_all()


SET_LOCK = SetLock()
# built image (paths.image_out) extracted for previews: (stamp, dir, index); see built_preview()
BUILT = (None, '', {})
BUILT_LOCK = threading.Lock()


def tr(key, **kw):
    return i18n.t(key, **kw)


def db_write_error():
    """None if both DBs are writable, otherwise an error message."""
    for db in [DB_FM, DB_DAB]:
        if os.path.exists(db):
            # check permissions without writing: a create/drop test table changed the DB's bytes,
            # so a package made right after loading was no longer byte-identical to the car's file
            folder = os.path.dirname(os.path.abspath(db))
            if not os.access(db, os.W_OK):
                return f"no write permission for database {db}"
            if not os.access(folder, os.W_OK):      # SQLite writes its journal next to the DB
                return f"no write permission for the folder of {db}"
    return None


def check_db_writable():
    err = db_write_error()
    if err:
        logger.error(f"CRITICAL: {err}")
        sys.exit(1)

def _mount_readable(d):
    """True only if `d` holds at least one readable subdir (catches empty/dead mounts)."""
    try:
        entries = os.listdir(d)
    except OSError:
        return False
    for e in entries:
        p = os.path.join(d, e)
        try:
            if os.path.isdir(p):
                os.listdir(p)   # probe — a dead mount raises OSError(EIO) here
                return True
        except OSError:
            return False
    return False                # empty / no readable subdir -> treat as unusable


def ensure_mounted(fresh=False):
    """Expose station_logos.img contents under MOUNT_DIR.

    Instead of `sudo mount -t qnx6` (needs password/TTY) it uses the pure-Python
    QNX6 reader to extract the image to MOUNT_DIR, re-extracting only when the
    image changed. An existing manual mount is used only if it is actually
    readable; a stale/broken mount falls back to a local extract dir.
    `fresh=True` (after swapping the image) never reuses a manual mount and
    always re-extracts.
    """
    global MOUNT_DIR
    img_path = os.path.abspath(IMG_ORIG)
    if not os.path.exists(img_path):
        logger.warning("No station_logos.img — image gallery unavailable.")
        return False
    if os.path.ismount(MOUNT_DIR):
        if fresh:
            logger.info(f"{MOUNT_DIR} is a manual mount of the old image — extracting the new one to a local dir.")
            MOUNT_DIR = os.path.abspath('.image_cache')
        elif _mount_readable(MOUNT_DIR):
            logger.info(f"Using existing mount at {MOUNT_DIR}")
            return True
        logger.warning(f"{MOUNT_DIR} is a stale/broken mount — extracting to a local dir instead "
                       f"(run `sudo umount {MOUNT_DIR}` or set paths.mount_dir to silence this).")
        MOUNT_DIR = os.path.abspath('.image_cache')
    if not QNX6_AVAILABLE:
        logger.error(f"qnx6fs module unavailable: {_QNX6_IMPORT_ERR}")
        return False
    os.makedirs(MOUNT_DIR, exist_ok=True)
    marker = os.path.join(MOUNT_DIR, '.extracted_from')
    st = os.stat(img_path)
    stamp = f"{img_path}|{st.st_mtime}|{st.st_size}|{config.active_input_dir(CFG)}"
    try:
        if not fresh and os.path.exists(marker) and os.listdir(MOUNT_DIR):
            with open(marker) as f:
                if f.read().strip() == stamp:
                    logger.info(f"Image contents already extracted to {MOUNT_DIR}")
                    _set_image_state(stamp)
                    return True
        # clear and re-extract - only a folder this app made (marker) or an empty one:
        # a mis-set paths.mount_dir must never cost the user files
        if os.listdir(MOUNT_DIR) and not os.path.exists(marker):
            logger.error("paths.mount_dir %s is not empty and was not made by LogosUpdater; "
                         "not clearing it. Point paths.mount_dir to an empty folder.", MOUNT_DIR)
            return False
        for n in os.listdir(MOUNT_DIR):
            p = os.path.join(MOUNT_DIR, n)
            shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
        logger.info(f"Extracting {img_path} -> {MOUNT_DIR} (pure-Python QNX6)...")
        with QNX6Reader(img_path) as r:
            nfiles = r.extract_to(MOUNT_DIR)
        with open(marker, 'w') as f:
            f.write(stamp)
        logger.info(f"Extracted {nfiles} files from image (checksum_ok={r.checksum_ok}).")
        _set_image_state(stamp)
        return True
    except Exception as e:
        logger.error(f"Image extraction failed: {e}")
        return False

def parse_multipart_form(headers, body):
    """multipart/form-data parser (replaces the cgi module removed in Py3.13).

    Returns (fields: dict[str,str], files: dict[str,bytes]).
    """
    ctype = headers.get('Content-Type', '') or ''
    if 'multipart/form-data' not in ctype.lower():
        return {}, {}
    m = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', ctype)
    if not m:
        return {}, {}
    boundary = (m.group(1) or m.group(2)).strip()
    delim = b'--' + boundary.encode('latin-1')
    fields, files = {}, {}
    for part in body.split(delim):
        if part.startswith(b'\r\n'): part = part[2:]
        if part.endswith(b'\r\n'): part = part[:-2]
        if not part or part == b'--':
            continue
        if b'\r\n\r\n' not in part:
            continue
        head, data = part.split(b'\r\n\r\n', 1)
        dispo = ''
        for line in head.decode('utf-8', 'replace').split('\r\n'):
            if line.lower().startswith('content-disposition'):
                dispo = line; break
        nm = re.search(r'name="([^"]*)"', dispo)
        if not nm:
            continue
        name = nm.group(1)
        if re.search(r'filename="', dispo):
            files[name] = data
        else:
            fields[name] = data.decode('utf-8', 'replace')
    return fields, files


def get_local_ip():
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0)
        s.connect(('10.254.254.254', 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
        return '127.0.0.1'


def _index_dir(base):
    """{ECC: {FILENAME.UPPER(): real filename}} for the PNGs under base/<ECC>/."""
    idx = {}
    try:
        for ecc in os.listdir(base):
            d = os.path.join(base, ecc)
            if os.path.isdir(d):
                idx[ecc.upper()] = {f.upper(): f for f in os.listdir(d) if f.upper().endswith('.PNG')}
    except OSError:
        pass
    return idx


def _set_image_state(stamp):
    global IMG_VER, IMG_INDEX
    IMG_VER = hashlib.md5(stamp.encode()).hexdigest()[:8]
    IMG_INDEX = _index_dir(MOUNT_DIR)


def built_preview(during_build=False):
    """(dir, index) of the built image when a USB package would take it, else None.

    The built image is extracted next to MOUNT_DIR once per build, so previews
    show exactly what goes to the car (a build with overwrite replaces image
    files with their logos_sync5 versions, which can look different).
    """
    global BUILT
    # mid-build/set swap image_out may be half-written: keep what was extracted before
    if SET_LOCK.locked() and not during_build:
        return (BUILT[1], BUILT[2]) if BUILT[0] else None
    if usb_package.pick_image(CFG)[0] != 'built' or not QNX6_AVAILABLE:
        BUILT = (None, '', {})
        return None
    st = os.stat(IMG_OUT)
    stamp = f"{os.path.abspath(IMG_OUT)}|{st.st_mtime}|{st.st_size}"
    with BUILT_LOCK:
        if BUILT[0] != stamp:
            d = os.path.normpath(MOUNT_DIR) + '_built'      # normpath: a trailing / or \\ would put it INSIDE mount_dir
            marker = os.path.join(d, '.extracted_from')
            try:
                with open(marker) as f:
                    fresh = f.read().strip() != stamp
            except OSError:
                fresh = True
            if fresh:
                try:
                    if os.path.exists(d) and os.listdir(d) and not os.path.exists(marker):
                        raise RuntimeError(f"{d} is not empty and was not made by LogosUpdater; not clearing it")
                    if os.path.exists(d): shutil.rmtree(d)
                    os.makedirs(d)
                    with QNX6Reader(IMG_OUT) as r:
                        n = r.extract_to(d)
                    with open(marker, 'w') as f:
                        f.write(stamp)
                    logger.info(f"Extracted built image for previews: {n} files -> {d}")
                except Exception as e:
                    logger.error(f"Built image extraction failed: {e}")
                    return None
            BUILT = (stamp, d, _index_dir(d))
        return BUILT[1], BUILT[2]


def logo_ver():
    """Cache-busting token for logo URLs: changes with the loaded set and with each build."""
    b = built_preview()
    return hashlib.md5(f"{IMG_VER}|{BUILT[0] if b else ''}".encode()).hexdigest()[:8]


def logo_source(ecc, fname, choices=None):
    """Where the logo `fname` for `ecc` is served from: ('built'|'img'|'sync5'|'styled'|None, path).

    Shows what the USB package will hold after a build: a version picked in 🎨 wins
    (the build puts it in), then the image a package would take — the built one if it
    matches the loaded one, else the loaded one — and logos_sync5/ only fills in files
    that image lacks (a build adds them).
    """
    ecc, key = (ecc or '').upper(), (fname or '').upper()
    pick = (_choices() if choices is None else choices).get(f"{ecc}/{key}")
    if pick == 'styled':
        p = _style_store().path(f"{ecc}/{key}")
        if os.path.isfile(p):
            return 'styled', p
    elif pick == 'image':
        real = IMG_INDEX.get(ecc, {}).get(key)
        p = real and safe_join(MOUNT_DIR, ecc, real)
        if p and os.path.isfile(p):
            return 'img', p
    elif pick == 'sync5':
        p = safe_join(LOGO_DIR, ecc, fname)
        if p and os.path.isfile(p):
            return 'sync5', p
    b = built_preview()
    base, idx, where = (b[0], b[1], 'built') if b else (MOUNT_DIR, IMG_INDEX, 'img')
    real = idx.get(ecc, {}).get(key)
    if real:
        p = safe_join(base, ecc, real)
        if p and os.path.isfile(p):
            return where, p
    p = safe_join(LOGO_DIR, ecc, fname)
    if p and os.path.isfile(p):
        return 'sync5', p
    return None, None


def build_image(overwrite):
    """Build IMG_OUT = IMG_ORIG + logos_sync5/ in pure Python; caller holds SET_LOCK."""
    if not QNX6_AVAILABLE:
        raise RuntimeError(f"qnx6fs module unavailable: {_QNX6_IMPORT_ERR}")
    orig = IMG_ORIG; out = IMG_OUT; work = WORK_DIR
    if not os.path.exists(orig):
        raise RuntimeError(f"Missing {orig}")
    t0 = time.time()
    # 1) extract the original
    if os.path.exists(work): shutil.rmtree(work)
    with QNX6Reader(orig) as r:
        n = r.extract_to(work)
    # 2) dogranie nowych logo z logos_sync5/
    added, updated, skipped = merge_logos(LOGO_DIR_NAME, work, overwrite, _choices(), CFG['paths']['styled_dir'])
    # 3) build (the previous build is derived data: keep just one backup of it).
    # Drop the "built from" sidecar first: a build that dies half way must not count as valid.
    try:
        os.remove(out + '.src')
    except FileNotFoundError:
        pass
    if os.path.exists(out):
        shutil.move(out, out + '.bak-' + time.strftime('%Y%m%d-%H%M%S'))
        config.prune_backups(out, keep=1)
    w = QNX6Writer(work, template_img=orig); info = w.write(out)
    usb_package.mark_built(orig, out)
    # 4) auto-weryfikacja: odczyt z powrotem
    with QNX6Reader(out) as r2:
        files = sum(1 for _, _, k in r2.walk() if k == 'file')
        ok = r2.checksum_ok
    resp = {'status': 'ok', 'extracted': n, 'added': added, 'updated': updated,
            'skipped': skipped, 'image': os.path.basename(out), 'files': files,
            'size': info['size'], 'used_blocks': info['used_blocks'],
            'free_blocks': info['free_blocks'], 'checksum_ok': ok,
            'seconds': round(time.time() - t0, 1)}
    logger.info(f"Image rebuild: {resp}")
    built_preview(during_build=True)
    return resp


def pkg_report(which='auto'):
    """logo_report() for the image a USB package would take (`which` as in pick_image)."""
    kind, img = usb_package.pick_image(CFG, which)
    rep = usb_package.logo_report(img, DB_FM, DB_DAB, LOGO_DIR, _choices(), CFG['paths']['styled_dir'])
    rep.pop('in_sync5'); rep.pop('in_styled')
    # the table previews the pick (what a build puts in), so it needs to know which one
    ch = _choices()
    rep['picks'] = {k: ch[k] for k in rep['pending'] + rep['missing'] if k in ch}
    rep.update({'kind': kind, 'image': os.path.basename(img)})
    return rep


def _choices():
    return usb_package.load_choices(CFG['paths']['logo_choices'])


# ---- guided workflow: session state, review of changes, settings, loading ----

def session_state():
    """What is being edited (for the step bar and "Editing: …"); the step is derived, not stored."""
    files = {'station_logos.img': IMG_ORIG, 'fm_station_logo.db': DB_FM, 'dab_station_logo.db': DB_DAB}
    missing = [n for n, p in files.items() if not os.path.exists(p)]
    rows = {k: (config._db_rows(p) if os.path.exists(p) else None) for k, p in (('fm', DB_FM), ('dab', DB_DAB))}
    fw = ''
    if os.path.exists(IMG_ORIG):
        try:
            fw = config._image_version(IMG_ORIG)
        except Exception:
            fw = ''
    return {'status': 'ok', 'has_data': not missing, 'missing': missing, 'loaded': config.loaded_info(CFG),
            'firmware': fw, 'rows': rows, 'step': 2 if not missing else 1,
            'source': {'id': getattr(SOURCE, 'id', 'none'), 'name': getattr(SOURCE, 'name', '')}}


_ROW_COLS = 'upper(ECC), upper(STATION_ID), RECEIVER_TYPE, FREQUENCY, upper(LOGO_FILENAME)'


def _db_tuples(db, table):
    from collections import Counter
    c = config.connect_ro(db)
    try:
        return Counter(tuple('' if v is None else str(v) for v in r)
                       for r in c.execute(f'SELECT {_ROW_COLS} FROM {table}'))
    finally:
        c.close()


def _row_item(typ, t, old_logo=None):
    ecc, pi, rt, freq, logo = t
    item = {'type': typ, 'ecc': ecc, 'pi': pi, 'rt': rt, 'logo': logo,
            'freq': ford_db._mhz(freq) if typ == 'FM' and freq.strip('0') else '',
            'country': f"{ecc}-{pi[:1]}" if pi else ''}
    if old_logo is not None:
        item['old_logo'] = old_logo
    return item


def changes_summary(limit=300):
    """Working DBs + what a build would put in the image vs. the set as loaded.

    rows: added / deleted / logo (same row, other logo file); an edited frequency or PI
    shows up as one deleted + one added row. logos: files a build adds to the loaded
    image, replaces in it, or replaces with a styled version (the basic-mode build:
    picked versions win, undecided files keep the image's version)."""
    out = {'status': 'ok', 'rows': None, 'logos': None}
    if not all(os.path.exists(p) for p in (IMG_ORIG, DB_FM, DB_DAB)):
        return dict(out, status='error', error=tr('err.nothing_loaded'))
    rows = {'added': [], 'deleted': [], 'logo': []}
    have_orig = True
    for typ, db, table, name in (('FM', DB_FM, 'fm_station_logo', 'fm_station_logo.db'),
                                 ('DAB', DB_DAB, 'dab_station_logo', 'dab_station_logo.db')):
        orig = config.loaded_original(CFG, name)
        if not orig:
            have_orig = False
            continue
        cur, old = _db_tuples(db, table), _db_tuples(orig, table)
        added, deleted = cur - old, old - cur
        by_key = {}
        for t, n in deleted.items():
            by_key.setdefault(t[:4], []).extend([t] * n)
        for t, n in added.items():
            for _ in range(n):
                prev = by_key.get(t[:4])
                if prev:
                    rows['logo'].append(_row_item(typ, t, prev.pop()[4]))
                else:
                    rows['added'].append(_row_item(typ, t))
        for lst in by_key.values():
            rows['deleted'].extend(_row_item(typ, t) for t in lst)
    if have_orig:
        out['rows'] = {k: {'count': len(v), 'items': v[:limit]} for k, v in rows.items()}
    ch = _choices()
    rep = usb_package.logo_report(IMG_ORIG, DB_FM, DB_DAB, LOGO_DIR, ch, CFG['paths']['styled_dir'])
    styled = [k for k in rep['pending'] if ch.get(k) == 'styled']
    replaced = [k for k in rep['pending'] if ch.get(k) != 'styled']
    styled += [k for k in rep['missing'] if ch.get(k) == 'styled']
    added = [k for k in rep['missing'] if ch.get(k) != 'styled']
    # a country to open in the table for each file: the first row that shows it
    where = {}
    for db, table in ((DB_FM, 'fm_station_logo'), (DB_DAB, 'dab_station_logo')):
        c = config.connect_ro(db)
        try:
            for ecc, sid, logo in c.execute(f'SELECT upper(ECC), upper(STATION_ID), upper(LOGO_FILENAME) FROM {table}'):
                if logo:
                    k = f"{ecc}/{logo if logo.endswith('.PNG') else logo + '.PNG'}"
                    where.setdefault(k, f"{ecc}-{(sid or '')[:1]}")
        finally:
            c.close()
    item = lambda k: {'key': k, 'country': where.get(k, ''), 'q': k.split('/')[-1][:-4]}  # noqa: E731
    # "no file anywhere" only for logo names the user brought in, not the car's old gaps
    origs = [config.loaded_original(CFG, n) for n in ('fm_station_logo.db', 'dab_station_logo.db')]
    absent = rep['absent']
    if all(origs):
        old_refs = {f'{e}/{f}' for e, f in usb_package._db_logos(*origs)}
        absent = [k for k in absent if k not in old_refs]
    out['logos'] = {name: {'count': len(v), 'items': [item(k) for k in v[:limit]]}
                    for name, v in (('added', added), ('replaced', replaced), ('styled', styled),
                                    ('absent', absent), ('undecided', rep['differs']))}
    n_rows = sum(len(v) for v in rows.values()) if have_orig else 0
    out['identical'] = have_orig and not n_rows and not (added or replaced or styled)
    out['total'] = n_rows + len(added) + len(replaced) + len(styled)
    return out


CONFIG_PATH = 'config.yaml'

BODY_MAX = 32 * 1024 * 1024           # any POST body except /api/load_upload (streams the car files to disk)
REMOTE_LOGO_MAX = 2 * 1024 * 1024     # "Set this logo" from a URL: images only, at most 2 MB


def public_http_url(url):
    """True for an http(s) URL whose host resolves only to public addresses (see fetcher.public_http_url).

    Keeps "Set this logo" from a URL away from this computer and the local network (the URL comes
    from the browser, which anyone on the LAN may use with server.host 0.0.0.0).
    """
    import fetcher
    return fetcher.public_http_url(url)


def _setting_text(v):
    """Label/help of a plugin setting: {"en":..,"pl":..}, an i18n key, or plain text."""
    if isinstance(v, dict):
        return v.get(CFG.get('language', 'en')) or v.get('en') or next(iter(v.values()), '')
    v = str(v or '')
    return T.get(v, v) if v else ''


def _source_fields(cls):
    """The plugin's settings_schema as form fields, with the current values (secrets masked)."""
    cur = (CFG.get('sources') or {}).get(cls.id) or {}
    out = []
    for f in getattr(cls, 'settings_schema', ()) or ():
        val = cur.get(f.key, f.default)
        out.append({'key': f.key, 'type': f.type, 'label': _setting_text(f.label) or f.key,
                    'help': _setting_text(f.help), 'required': f.required, 'placeholder': f.placeholder,
                    'choices': [str(c) for c in f.choices], 'min': f.min, 'max': f.max,
                    'value': '' if f.type == 'secret' else ('' if val is None else val),
                    'is_set': bool(cur.get(f.key)) if f.type == 'secret' else None})
    return out


def settings_info():
    import sources
    srcs = [{'id': sid, 'name': getattr(cls, 'name', sid), 'fields': _source_fields(cls)}
            for sid, cls in sorted(sources.available(listed_only=True).items())]
    broken = [tr('set.plugin_failed', module=m, error=e) for m, e in sorted(sources.import_errors().items())]
    return {'status': 'ok', 'language': CFG.get('language', 'en'), 'languages': i18n.available_languages(),
            'data_source': CFG.get('data_source', 'none'), 'sources': srcs, 'plugin_errors': broken}


def save_settings(data):
    """Validate, write to config.yaml (only these keys) and switch language/source live."""
    import sources
    global SOURCE, T, CFG
    changes = {}
    lang = data.get('language')
    if lang is not None:
        if lang not in i18n.available_languages():
            raise ValueError(f"unknown language: {lang}")
        changes['language'] = lang
    src = data.get('data_source')
    if src is not None:
        if src not in sources.available(listed_only=True):
            raise ValueError(f"unknown data source: {src}")
        changes['data_source'] = src
    # settings of the selected source, validated against its settings_schema (other keys are ignored)
    sid = src if src is not None else CFG.get('data_source', 'none')
    submitted = (data.get('sources') or {}).get(sid)
    cls = sources.available(listed_only=True).get(sid)
    if submitted is not None and cls is not None and getattr(cls, 'settings_schema', ()):
        from sources.base import SourceError
        cur = dict((CFG.get('sources') or {}).get(sid) or {})
        try:
            clean = sources.base.validate_settings(cls.settings_schema, submitted, cur)
        except SourceError as e:
            label = next((_setting_text(f.label) or f.key for f in cls.settings_schema
                          if f.key == e.fmt.get('field')), e.fmt.get('field'))
            code = e.fmt.get('reason', '')
            detail = {k: v for k, v in e.fmt.items() if k not in ('field', 'reason')}
            reason = tr('set.reason_' + code, **detail) if ('set.reason_' + code) in T else code
            raise ValueError(tr('err.setting_invalid', field=label, reason=reason))
        # schema keys that were cleared are removed from config.yaml (None = delete in save_partial)
        for f in cls.settings_schema:
            if f.key in cur and f.key not in clean:
                clean[f.key] = None
        changes['sources'] = {sid: clean}
    if not changes:
        return settings_info()
    config.save_partial(CONFIG_PATH, changes)
    new_cfg = config.drop_cleared(config.deep_merge(CFG, changes), changes)
    new_src = sources.get_active(new_cfg)
    # rebind instead of clear()+update(): a request or thread holding the old dict never sees it empty
    CFG = new_cfg
    SOURCE = new_src
    i18n.load(CFG.get('language', 'en'))
    T = i18n.catalog()
    T['_btn_update'] = i18n.t('btn.update_source', source=SOURCE.name)
    logger.info("Settings saved to %s: %s", CONFIG_PATH, changes)
    return dict(settings_info(), saved=list(changes))


UPLOAD_MAX = 1 << 30          # 1 GiB: the image is ~150 MB
UPLOAD_FIELDS = {'image': 'station_logos.img', 'fm': 'fm_station_logo.db', 'dab': 'dab_station_logo.db'}


def stream_multipart(rfile, length, ctype, out_dir):
    """Write the file parts of a multipart body straight to out_dir (never the whole body
    in memory). Only the fields in UPLOAD_FIELDS are kept, under their canonical names.
    Returns {field: path}."""
    m = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', ctype or '')
    if not m:
        raise ValueError('no multipart boundary')
    delim = b'\r\n--' + (m.group(1) or m.group(2)).strip().encode('latin-1')
    left = length
    buf = b'\r\n'               # so the first boundary looks like every other one
    saved, out = {}, None

    def fill(n=1 << 20):
        nonlocal buf, left
        if left <= 0:
            return False
        chunk = rfile.read(min(n, left))
        if not chunk:
            left = 0
            return False
        left -= len(chunk)
        buf += chunk
        return True

    # skip the preamble up to the first boundary
    while delim not in buf:
        if not fill():
            raise ValueError('malformed multipart body')
    buf = buf[buf.index(delim) + len(delim):]
    while True:
        while len(buf) < 2 and fill():
            pass
        if buf[:2] == b'--':
            break                                    # closing boundary
        while b'\r\n\r\n' not in buf:
            if len(buf) > 16384 or not fill(4096):
                raise ValueError('malformed multipart headers')
        head, buf = buf.split(b'\r\n\r\n', 1)
        nm = re.search(rb'name="([^"]*)"', head)
        name = nm.group(1).decode('utf-8', 'replace') if nm else ''
        target = UPLOAD_FIELDS.get(name) if b'filename="' in head else None
        out = open(os.path.join(out_dir, target), 'wb') if target else None
        try:
            while True:
                i = buf.find(delim)
                if i >= 0:
                    if out: out.write(buf[:i])
                    buf = buf[i + len(delim):]
                    break
                keep = len(delim)
                if len(buf) > keep:
                    if out: out.write(buf[:-keep])
                    buf = buf[-keep:]
                if not fill():
                    raise ValueError('truncated upload')
        finally:
            if out:
                out.close()
        if target:
            saved[name] = os.path.join(out_dir, target)
    return saved


def input_error_text(e):
    """config.InputError -> the translated message that names the file."""
    if e.code == 'missing':
        return tr('err.upload_missing', file=e.file)
    if e.code == 'bad_img':
        return tr('err.upload_bad_img', file=e.file)
    return tr('err.upload_bad_db', file=e.file, table=e.table)


def check_upload(paths):
    """Refuse anything that is not a complete QNX6 image + the two Ford station DBs (message names the file)."""
    try:
        config.check_input_files(paths)
    except config.InputError as e:
        raise ValueError(input_error_text(e)) from e


def swap_set(src_dir, origin):
    """load_inputs + fresh gallery extraction; caller holds SET_LOCK. Returns (copied, mounted, err)."""
    copied = config.load_inputs(CFG, src_dir, origin=origin)
    mounted = ensure_mounted(fresh=True)
    return copied, mounted, db_write_error()


def _style_store():
    return logo_style.Store(CFG['paths']['styled_dir'])


def _db_file_countries():
    """{"ECC/NAME.PNG": {"E2-3", ...}} for the logo files the FM/DAB DBs point to.

    RDS country = ECC + first hex digit of the PI code (STATION_ID); one ECC spans
    several countries, so the ECC folder alone is not a country.
    """
    out = {}
    for db, table in ((DB_FM, 'fm_station_logo'), (DB_DAB, 'dab_station_logo')):
        if not os.path.exists(db): continue
        conn = config.connect_ro(db)
        for ecc, sid, fn in conn.execute(f"SELECT ECC, STATION_ID, LOGO_FILENAME FROM {table}"):
            if fn:
                ecc = (ecc or '').upper()
                out.setdefault(f"{ecc}/{fn.upper()}.PNG", set()).add(f"{ecc}-{(sid or '?')[:1].upper()}")
        conn.close()
    return out


def _station_names(keys):
    """{key: [rows, sorted station names]} for the given logo files (names from output.csv)."""
    names = {}
    if os.path.exists('output.csv'):
        with open('output.csv', encoding='utf-8') as f:
            for r in csv.reader(f):
                if len(r) > 1: names.setdefault(r[0].strip().upper(), r[1].strip())
    used = {k: [0, set()] for k in keys}
    for db, table in ((DB_FM, 'fm_station_logo'), (DB_DAB, 'dab_station_logo')):
        if not os.path.exists(db): continue
        conn = config.connect_ro(db)
        for ecc, sid, fn in conn.execute(f"SELECT ECC, STATION_ID, LOGO_FILENAME FROM {table}"):
            k = f"{(ecc or '').upper()}/{(fn or '').upper()}.PNG"
            if k in used:
                used[k][0] += 1; used[k][1].add(names.get((sid or '').upper(), sid))
        conn.close()
    return {k: [n, sorted(s)] for k, (n, s) in used.items()}


def logo_versions():
    """Files with more than one version to pick from: the loaded image and logos_sync5/
    hold different versions, or a styled version exists (🖌 Logo style).

    The build starts from the loaded image, so this list is stable across builds;
    each entry says which version was picked and which one the package image has.
    """
    choices = _choices()
    store = _style_store()
    base = usb_package.logo_report(IMG_ORIG, DB_FM, DB_DAB, LOGO_DIR)['differs']
    styled = {k for k in store.load()['files'] if store.exists(k)}
    kind, img = usb_package.pick_image(CFG)
    rep = usb_package.logo_report(img, DB_FM, DB_DAB, LOGO_DIR, choices, store.root)
    in_sync5, in_styled, missing = set(rep['in_sync5']), set(rep['in_styled']), set(rep['missing'])
    used = _station_names(set(base) | styled)
    s_idx, ver = _index_dir(LOGO_DIR), logo_ver()
    items = []
    for k in sorted(set(base) | styled):
        if not used[k][0]:
            continue                       # styled earlier, no longer in the DBs
        ecc, fn = k.split('/')
        real = IMG_INDEX.get(ecc, {}).get(fn)
        s5 = s_idx.get(ecc, {}).get(fn)
        items.append({'key': k, 'choice': choices.get(k),
                      'in_pkg': 'styled' if k in in_styled else 'sync5' if k in in_sync5
                                else None if k in missing else 'image',
                      'rows': used[k][0], 'stations': used[k][1][:6],
                      'img_url': real and f"/img_gallery/{ecc}/{real}?v={IMG_VER}",
                      'sync5_url': s5 and f"/logos_sync5/{ecc}/{s5}?v={ver}",
                      'styled_url': f"/logos_styled/{ecc}/{fn}?v={ver}" if k in styled else None})
    return {'status': 'ok', 'kind': kind, 'image': os.path.basename(img), 'items': items}


def style_base(key, choices, meta, s_idx):
    """(version, path) a style starts from.

    The version the file is picked as (for a styled pick: the one it was made
    from), else the loaded image's copy, else logos_sync5's — never the built
    image or a styled file, so restyling never compounds.
    """
    ecc, fn = key.split('/')
    want = choices.get(key)
    if want == 'styled':
        want = (meta['files'].get(key) or {}).get('base')
    real, s5 = IMG_INDEX.get(ecc, {}).get(fn), s_idx.get(ecc, {}).get(fn)
    paths = {'image': real and safe_join(MOUNT_DIR, ecc, real), 'sync5': s5 and safe_join(LOGO_DIR, ecc, s5)}
    for v in ([want] if want in paths else []) + ['image', 'sync5']:
        if paths[v] and os.path.isfile(paths[v]):
            return v, paths[v]
    return None, None


def _style_keys(data):
    """Files a style request covers: `country` ("E2-3") or explicit `keys`, limited to DB logos."""
    files = _db_file_countries()
    c = str(data.get('country') or '').upper()
    if c:
        return sorted(k for k, cs in files.items() if c in cs)
    return sorted({str(k).upper() for k in data.get('keys') or []} & set(files))


def style_info():
    """Presets, countries (with file counts) and how many files are picked styled."""
    cnt = {}
    for cs in _db_file_countries().values():
        for c in cs: cnt[c] = cnt.get(c, 0) + 1
    store = _style_store()
    return {'status': 'ok', 'presets': store.presets(), 'builtin': sorted(logo_style.PRESETS),
            'defaults': logo_style.DEFAULTS,
            'countries': [{'code': c, 'name': logo_style.country_name(c), 'files': n}
                          for c, n in sorted(cnt.items(), key=lambda x: (-x[1], x[0]))],
            'styled': sum(1 for v in _choices().values() if v == 'styled'),
            'ai': _ai_info()}


def _ai_info():
    a = logo_ai.settings(CFG)
    return {'available': logo_ai.available(a), 'provider': a['provider'],
            'models': {p: logo_ai.model_of(a, p) for p in ('gemini', 'kie')}, 'price': a['price_per_image']}


def _data_url(png):
    return 'data:image/png;base64,' + base64.b64encode(png).decode()


def style_preview(data):
    """Before/after for a page of the files in scope (nothing is written)."""
    keys = _style_keys(data)
    off = max(0, int(data.get('offset') or 0)); lim = max(1, min(100, int(data.get('limit') or 40)))
    params = logo_style.normalize(data.get('params'))
    choices, meta, s_idx = _choices(), _style_store().load(), _index_dir(LOGO_DIR)
    used = _station_names(keys[off:off + lim])
    items = []
    for k in keys[off:off + lim]:
        base, src = style_base(k, choices, meta, s_idx)
        it = {'key': k, 'base': base, 'choice': choices.get(k), 'stations': used[k][1][:4], 'warn': []}
        if src:
            try:
                with open(src, 'rb') as f:
                    it['before'] = _data_url(f.read())
                it['after'] = _data_url(logo_style.render(src, params, it['warn']))
            except Exception as e:
                it['error'] = str(e)
        else:
            it['error'] = 'no file'
        items.append(it)
    return {'status': 'ok', 'total': len(keys), 'offset': off, 'items': items}


def style_apply(data):
    """Write styled versions and pick them (or with `action: "clear"` drop the styled pick).

    Body: scope (`country` | `keys`), `params`, `preset`, `exclude` (keys left as
    they are), `skip_warn` (leave files whose result got a warning, unless in `force`).
    Caller holds SET_LOCK (a build reads styled_dir).
    """
    keys = _style_keys(data)
    path = CFG['paths']['logo_choices']
    choices = _choices()
    if data.get('action') == 'clear':
        ch = {k: None for k in keys if choices.get(k) == 'styled'}
        usb_package.save_choices(path, ch)
        return {'status': 'ok', 'cleared': len(ch)}
    excl = {str(k).upper() for k in data.get('exclude') or []}
    force = {str(k).upper() for k in data.get('force') or []}
    # files to take from the cached AI redraw (🤖) instead of the local filters; never calls the AI
    ai_keys = {str(k).upper() for k in data.get('ai') or []}
    provider = data.get('provider') or logo_ai.settings(CFG)['provider']
    params = logo_style.normalize(data.get('params'))
    preset = str(data.get('preset') or '')[:40]
    store = _style_store(); meta = store.load(); s_idx = _index_dir(LOGO_DIR)
    ai_cfg, ai_cache = logo_ai.settings(CFG), logo_ai.Cache(store.root)
    ch, skipped, warned, failed = {}, 0, 0, []
    for k in keys:
        if k in excl:
            skipped += 1; continue
        base, src = style_base(k, choices, meta, s_idx)
        if not src:
            failed.append(k); continue
        try:
            w = []
            if k in ai_keys:
                raw, _ = logo_ai.redraw(ai_cfg, provider, k, logo_ai.prepare(src), ai_cache,
                                       prompt=logo_ai.prompt_for(params), allow_call=False)
                if raw is None:
                    raise logo_ai.AIError('no cached AI result')
                with Image.open(src) as im0:
                    size = im0.size
                im, w = logo_ai.postprocess(raw, size, params)
                png = logo_ai.to_png(im)
            else:
                png = logo_style.render(src, params, w)
        except Exception as e:
            logger.warning(f"style {k}: {e}"); failed.append(k); continue
        if w and data.get('skip_warn') and k not in force:
            warned += 1; continue
        store.write(k, png, meta, base, preset + (f'+ai:{provider}' if k in ai_keys else ''), params)
        ch[k] = 'styled'
    store.save(meta)
    usb_package.save_choices(path, ch)
    logger.info(f"Logo style '{preset}': {len(ch)} applied, {skipped} excluded, {warned} skipped (warning), "
                f"{len(failed)} failed; {params}")
    return {'status': 'ok', 'applied': len(ch), 'excluded': skipped, 'warned': warned, 'failed': failed[:20]}


def style_ai_prepare(data):
    """Under SET_LOCK: what an AI request covers, with the model inputs already read.

    `scan: true` -> the files in scope whose local result gets a warning (⚠) — or all of
    them with `all: true` — with how many are already cached (no cost). Otherwise `keys` (max 12) -> [(key, base, src size,
    before data url, model input png)] for style_ai_run().
    """
    a = logo_ai.settings(CFG)
    provider = data.get('provider') or a['provider']
    params = logo_style.normalize(data.get('params'))
    choices, meta, s_idx = _choices(), _style_store().load(), _index_dir(LOGO_DIR)
    cache = logo_ai.Cache(CFG['paths']['styled_dir'])
    if data.get('scan'):
        # all: every file in scope (picked by hand), else only the ⚠ ones
        keys, warned, cached = _style_keys(data), [], 0
        for k in keys:
            base, src = style_base(k, choices, meta, s_idx)
            if not src:
                continue
            w = ['all'] if data.get('all') else []
            try:
                w or logo_style.render(src, params, w)
            except Exception:
                continue
            if w:
                warned.append(k)
                if logo_ai.available(a).get(provider) and \
                        logo_ai.redraw(a, provider, k, logo_ai.prepare(src), cache,
                                       prompt=logo_ai.prompt_for(params), allow_call=False)[0] is not None:
                    cached += 1
        return {'status': 'ok', 'scope': len(keys), 'keys': warned, 'cached': cached,
                'provider': provider, 'available': logo_ai.available(a).get(provider, False),
                'price': (a['price_per_image'] or {}).get(provider)}
    keys = _style_keys(data)[:12]
    work = []
    for k in keys:
        base, src = style_base(k, choices, meta, s_idx)
        if src:
            with open(src, 'rb') as f:
                before = f.read()
            with Image.open(src) as im0:
                size = im0.size
            work.append((k, base, size, before, logo_ai.prepare(src)))
    return {'provider': provider, 'params': params, 'work': work, 'used': _station_names(keys), 'cfg': a,
            'cached_only': bool(data.get('cached_only'))}


def style_ai_run(job):
    """AI redraws for prepared work (outside SET_LOCK: network). Returns preview items."""
    from concurrent.futures import ThreadPoolExecutor
    a, provider, params = job['cfg'], job['provider'], job['params']
    cache = logo_ai.Cache(CFG['paths']['styled_dir'])

    def one(w):
        k, base, size, before, png = w
        it = {'key': k, 'base': base, 'ai': provider, 'stations': job['used'][k][1][:4], 'warn': [],
              'before': _data_url(before)}
        try:
            raw, it['cached'] = logo_ai.redraw(a, provider, k, png, cache, prompt=logo_ai.prompt_for(params),
                                               allow_call=not job['cached_only'])
            if raw is None:
                raise logo_ai.AIError('not cached')
            im, it['warn'] = logo_ai.postprocess(raw, size, params)
            it['after'] = _data_url(logo_ai.to_png(im))
        except Exception as e:
            it['error'] = str(e)[:300]
            logger.warning(f"AI redraw {k}: {e}")
        return it
    with ThreadPoolExecutor(max(1, min(8, int(a.get('workers') or 3)))) as ex:
        items = list(ex.map(one, job['work']))
    logger.info(f"AI redraw ({provider}): {len(items)} files, "
                f"{sum(1 for i in items if i.get('cached') is False and 'error' not in i)} paid calls, "
                f"{sum(1 for i in items if 'error' in i)} errors")
    return {'status': 'ok', 'items': items}


def style_preset(data):
    """Save (`params`) or delete (`delete: true`) a user preset; built-in names are fixed."""
    name = str(data.get('name') or '').strip()[:40]
    if not name or name in logo_style.PRESETS:
        raise ValueError(tr('style.err_preset_name'))
    store = _style_store(); meta = store.load()
    if data.get('delete'):
        meta['presets'].pop(name, None)
    else:
        meta['presets'][name] = logo_style.normalize(data.get('params'))
    store.save(meta)
    return {'status': 'ok', 'presets': store.presets()}


def set_row_logo(db_t, ecc, sid, freq, logo, any_freq_fallback=False):
    """Point the DB row(s) of one station at `logo` (file base name); returns rows changed.

    Always scoped to the station's country (ECC) — the same PI exists in several
    countries. FM matches the frequency ('098500' or '98500'); with
    `any_freq_fallback` an FM station whose frequency matches nothing gets the logo
    on all its frequencies in that country. DAB matches (ECC, PI).
    """
    ecc = (ecc or '').upper()
    if not ecc or not sid:
        return 0
    conn = sqlite3.connect(DB_FM if db_t == 'FM' else DB_DAB)
    try:
        c = conn.cursor()
        if db_t == 'FM':
            f_s = str(freq or '').zfill(6)
            alt = str(int(f_s)) if f_s.isdigit() else f_s
            c.execute("UPDATE fm_station_logo SET LOGO_FILENAME=? WHERE upper(ECC)=? AND STATION_ID=? "
                      "AND (FREQUENCY=? OR FREQUENCY=?)", (logo, ecc, sid, f_s, alt))
            if c.rowcount == 0 and any_freq_fallback:
                c.execute("UPDATE fm_station_logo SET LOGO_FILENAME=? WHERE upper(ECC)=? AND STATION_ID=?",
                          (logo, ecc, sid))
        else:
            c.execute("UPDATE dab_station_logo SET LOGO_FILENAME=? WHERE upper(ECC)=? AND STATION_ID=?",
                      (logo, ecc, sid))
        n = c.rowcount
        conn.commit()
        return n
    finally:
        conn.close()


class StationViewerHandler(http.server.SimpleHTTPRequestHandler):
    def do_HEAD(self):
        # SimpleHTTPRequestHandler would serve any file of the data directory (config.yaml, DBs)
        self.send_error(405)

    # --- request gate: who may talk to this unauthenticated API -----------------------------
    def _host_allowed(self):
        """Host header must name this machine: blocks DNS rebinding (an attacker's domain that
        resolves to 127.0.0.1 would otherwise read and drive the API through the browser)."""
        import ipaddress
        import socket
        host = (self.headers.get('Host') or '').strip().lower()
        name = host[1:host.find(']')] if host.startswith('[') else host.rsplit(':', 1)[0]
        if not name:
            return True                       # HTTP/1.0 client without Host (curl -0, scripts)
        try:
            ipaddress.ip_address(name)
            return True                       # IP literals: localhost, LAN mode, Docker
        except ValueError:
            pass
        me = socket.gethostname().lower()
        allowed = {'localhost', me, me + '.local', str((CFG.get('server') or {}).get('host') or '').lower()}
        allowed |= {str(h).lower() for h in ((CFG.get('server') or {}).get('allowed_hosts') or [])}
        return name in allowed

    def _origin_allowed(self):
        """A browser POST must come from this app's own page (blocks CSRF from other sites:
        text/plain JSON and form uploads need no CORS preflight). No Origin = not a browser."""
        origin = self.headers.get('Origin')
        if origin is None:
            return True
        host = (self.headers.get('Host') or '').strip().lower()
        return origin.lower() in (f'http://{host}', f'https://{host}')

    def _discard_body(self, limit=1 << 20):
        """Read the unread request body before an early answer (403/404/409/413). Closing a socket
        with unread data makes Windows send a TCP reset, so the client gets WinError 10053/10054
        instead of the answer. Bodies over `limit` are not read; the connection is closed instead."""
        if self.command != 'POST' or getattr(self, '_body_read', False):
            return
        self._body_read = True
        try:
            n = int(self.headers.get('Content-Length', 0) or 0)
        except ValueError:
            n = 0
        if 0 < n <= limit:
            try:
                self.rfile.read(n)
            except OSError:
                pass
        elif n > limit:
            self.close_connection = True

    def _forbidden(self):
        self._discard_body()
        logger.warning("Refused %s %s from %s (Host=%r Origin=%r)", self.command, self.path,
                       self.client_address[0], self.headers.get('Host'), self.headers.get('Origin'))
        self.send_response(403)
        body = b'Forbidden: request not from this app (Host/Origin check)'
        self.send_header('Content-Type', 'text/plain'); self.send_header('Content-Length', str(len(body)))
        self.end_headers(); self.wfile.write(body)

    # writes that touch the working DBs/logos outside handlers that already take SET_LOCK
    _LOCKED_POSTS = ('/api/upload_logo', '/api/copy_logo', '/api/fetch_remote_logo', '/api/delete_records',
                     '/api/settings')
    # not while a background update (which plans DB rowids and writes them minutes later) runs
    _NOT_DURING_UPDATE = ('/api/load_inputs', '/api/load_upload', '/api/delete_records', '/api/stations/import',
                          '/api/rebuild_image', '/api/usb_package',
                          # these write rows the update planned minutes ago (UPDATE ... WHERE rowid=?)
                          '/api/station', '/api/upload_logo', '/api/copy_logo', '/api/fetch_remote_logo',
                          # switches the source / its settings the running update was started with
                          '/api/settings')

    def do_POST(self):
        self._body_read = False          # the handler object serves every request of a keep-alive connection
        try:
            return self._do_post()
        except ConnectionError:              # browser went away mid-request
            pass

    def log_message(self, fmt, *args):
        # the console window is the app for many users: keep the SERVER line visible, log only problems
        if args and str(args[1] if len(args) > 1 else '')[:1] in ('2', '3'):
            return
        logger.info("%s - %s", self.address_string(), fmt % args)

    def _do_post(self):
        if not (self._host_allowed() and self._origin_allowed()):
            return self._forbidden()
        try:
            length = int(self.headers.get('Content-Length', 0) or 0)
        except ValueError:
            return self.send_error(400)
        if self.path != '/api/load_upload' and length > BODY_MAX:     # the upload streams to disk itself
            self._discard_body()
            return self._send_json({'status': 'error', 'error': tr('err.too_big')}, 413)
        if self.path in self._NOT_DURING_UPDATE and ford_db.PROGRESS.snapshot().get('running'):
            self._discard_body()
            return self._send_json({'status': 'error', 'error': tr('err.update_running')}, 409)
        if self.path in self._LOCKED_POSTS:
            if not SET_LOCK.acquire(blocking=False):
                self._discard_body()
                return self._send_json({'status': 'error', 'error': tr('err.busy')}, 409)
            try:
                return self._dispatch_post()
            finally:
                SET_LOCK.release()
        return self._dispatch_post()

    def _dispatch_post(self):
        if self.path == '/api/upload_logo': self.handle_upload_logo()
        elif self.path == '/api/copy_logo': self.handle_copy_logo()
        elif self.path == '/api/fetch_remote_logo': self.handle_fetch_remote_logo()
        elif self.path == '/api/delete_records': self.handle_delete_records()
        elif self.path == '/api/station': self.handle_station()
        elif self.path == '/api/stations/import': self.handle_stations_import()
        elif self.path == '/api/rebuild_image': self.handle_rebuild_image()
        elif self.path == '/api/load_inputs': self.handle_load_inputs()
        elif self.path == '/api/load_upload': self.handle_load_upload()
        elif self.path == '/api/settings': self.handle_settings()
        elif self.path == '/api/usb_package': self.handle_usb_package()
        elif self.path == '/api/logo_versions': self.handle_logo_versions()
        elif self.path in ('/api/style_preview', '/api/style_apply', '/api/style_presets', '/api/style_ai'): self.handle_style()
        # /api/update_radiopolska: legacy alias kept for older bookmarks/scripts.
        elif self.path in ('/api/update', '/api/update_radiopolska'): self.handle_update()
        elif self.path == '/api/update_cancel':
            ford_db.PROGRESS.cancel.set(); self._send_json({'status': 'ok'})
        else:
            self._discard_body(); self.send_error(404)

    def handle_upload_logo(self):
        try:
            length = int(self.headers.get('Content-Length', 0) or 0)
            body = self.rfile.read(length)
            fields, files = parse_multipart_form(self.headers, body)
            if 'file' not in files:
                return self.send_error(400, tr('err.no_file'))
            sid, db_t, ecc, freq = (fields.get('station_id'), fields.get('type'),
                                    fields.get('ecc'), fields.get('freq'))
            if not ecc:
                return self.send_error(400, tr('err.no_ecc'))
            resized = resize_image(files['file'])
            if not resized: return self.send_error(500, tr('err.process_image'))
            fname = hashlib.md5(resized).hexdigest().upper()[:8] + ".PNG"
            t_dir = safe_join(LOGO_DIR, ecc)
            if not t_dir: return self.send_error(400, tr('err.bad_ecc'))
            os.makedirs(t_dir, exist_ok=True)
            with open(os.path.join(t_dir, fname), 'wb') as f: f.write(resized)
            set_row_logo(db_t, ecc, sid, freq, fname[:-4])
            self.send_response(200); self.send_header('Content-type','application/json'); self.end_headers()
            self.wfile.write(json.dumps({'status':'ok','new_logo':fname[:-4]}).encode())
        except Exception as e:
            logger.exception("handle_upload_logo"); self.send_error(500, str(e))

    def handle_copy_logo(self):
        try:
            data = json.loads(self.rfile.read(int(self.headers['Content-Length'])).decode())
            stations = data.get('stations', [{'station_id':data.get('station_id'),'type':data.get('type'),'ecc':data.get('ecc'),'freq':data.get('freq')}])
            src_fname = data.get('filename'); logo_base = src_fname.split('.')[0]
            updated = 0
            for st in stations:
                sid, db_t, ecc, freq = st['station_id'], st['type'], st['ecc'], st['freq']
                src_p = safe_join(MOUNT_DIR, ecc, src_fname)
                if not (src_p and os.path.exists(src_p)): src_p = safe_join(LOGO_DIR, ecc, src_fname)
                if not (src_p and os.path.exists(src_p)): continue
                t_dir = safe_join(LOGO_DIR, ecc)
                if not t_dir: continue
                os.makedirs(t_dir, exist_ok=True)
                if not os.path.exists(os.path.join(t_dir, src_fname)): shutil.copy2(src_p, os.path.join(t_dir, src_fname))
                updated += set_row_logo(db_t, ecc, sid, freq, logo_base, any_freq_fallback=True)
            self.send_response(200); self.send_header('Content-type','application/json'); self.end_headers()
            self.wfile.write(json.dumps({'status':'ok','updated':updated}).encode())
        except Exception as e:
            logger.exception("handle_copy_logo"); self.send_error(500, str(e))

    def handle_fetch_remote_logo(self):
        try:
            data = json.loads(self.rfile.read(int(self.headers['Content-Length'])).decode())
            url = data['url']
            # handles a single station AND bulk — like handle_copy_logo
            stations = data.get('stations') or [{'station_id': data.get('station_id'),
                'type': data.get('type'), 'ecc': data.get('ecc'), 'freq': data.get('freq')}]
            if not public_http_url(url):
                return self._send_json({'status': 'error', 'error': tr('err.remote_url')}, 400)
            from fetcher import FetchError
            try:
                resp = sources.shared_http(CFG).get(url, kind='image', max_bytes=REMOTE_LOGO_MAX,
                                                      url_check=public_http_url)
            except FetchError as e:
                return self._send_json({'status': 'error', 'error': tr('err.remote_fetch', error=str(e))}, 400)
            if resp.status != 200:
                return self._send_json({'status': 'error', 'error': tr('err.remote_fetch', error=f'HTTP {resp.status}')}, 400)
            resized = resize_image(resp.content)
            if not resized: return self.send_error(500, tr('err.process_image'))
            # filename from content — the same logo lands in one file (dedup)
            fname_base = hashlib.md5(resized).hexdigest().upper()[:8]
            updated = 0
            for st in stations:
                sid, db_t = st.get('station_id'), st.get('type')
                ecc = (st.get('ecc') or '').upper(); freq = st.get('freq')
                if not sid or not db_t or not ecc: continue
                t_dir = safe_join(LOGO_DIR, ecc)
                if not t_dir: continue
                os.makedirs(t_dir, exist_ok=True)
                dst = os.path.join(t_dir, fname_base + ".PNG")
                if not os.path.exists(dst):
                    with open(dst, 'wb') as f: f.write(resized)
                updated += set_row_logo(db_t, ecc, sid, freq, fname_base, any_freq_fallback=True)
            self.send_response(200); self.send_header('Content-type','application/json'); self.end_headers()
            self.wfile.write(json.dumps({'status':'ok','new_logo':fname_base,'updated':updated}).encode())
        except Exception as e:
            logger.exception("handle_fetch_remote_logo"); self.send_error(500, str(e))

    def _json_body(self):
        length = int(self.headers.get('Content-Length', 0) or 0)
        return json.loads(self.rfile.read(length).decode('utf-8')) if length else {}

    def _edit_error(self, e):
        return self._send_json({'status': 'error', 'code': e.code, 'info': e.info,
                                'message': tr('err.st_' + e.code, **{k: str(v) for k, v in e.info.items()})}, 400)

    def handle_station(self):
        """Add a station row or edit one (manual editor). Body:
        {action: add|update, country: "E0-D", type: FM|DAB, pi, freq (MHz), name,
         rowid (update), confirm (accept a PI from another country)}.
        Returns {status: ok, rowid, row_html} | {status: confirm, message} | error."""
        try:
            d = self._json_body()
            country = str(d.get('country') or '')
            ecc, _, digit = country.upper().partition('-')
            typ, pi = str(d.get('type') or '').upper(), str(d.get('pi') or '').strip().upper()
            name, action = str(d.get('name') or ''), d.get('action')
            if not ecc or not digit:
                return self._send_json({'status': 'error', 'message': tr('err.no_ecc')}, 400)
            if pi and pi[0] != digit and not d.get('confirm') and action == 'add':
                return self._send_json({'status': 'confirm',
                                        'message': tr('st.pi_other_country', pi=pi, country=_country_label(f"{ecc}-{pi[0]}"))})
            if not SET_LOCK.acquire(blocking=False):
                return self._send_json({'status': 'error', 'message': tr('err.busy')}, 409)
            try:
                if action == 'add':
                    rowid = ford_db.add_station(CFG, typ, ecc, pi, d.get('freq'))
                elif action == 'update':
                    rowid = int(d.get('rowid'))
                    if str(d.get('rt') or '') in ford_db.OWN_TYPES.values():
                        ford_db.update_station(CFG, typ, rowid, sid=pi or None, freq_mhz=d.get('freq'))
                else:
                    return self._send_json({'status': 'error', 'message': 'bad action'}, 400)
                if pi and ('name' in d):
                    ford_db.save_names(CFG['paths']['station_names'], {f"{ecc}/{pi}": name})
            finally:
                SET_LOCK.release()
            ctx = _row_ctx(country)
            row = next((r for r in ctx['rows'] if r['rowid'] == rowid and r['type'] == typ), None)
            return self._send_json({'status': 'ok', 'rowid': rowid,
                                    'row_html': _row_html(row, ctx) if row else ''})
        except ford_db.EditError as e:
            return self._edit_error(e)
        except Exception as e:
            logger.exception("handle_station"); self.send_error(500, str(e))

    def handle_stations_import(self):
        """CSV import: {csv, dry_run: true} -> preview + token; {csv, token} -> write."""
        try:
            d = self._json_body()
            dry = bool(d.get('dry_run', True))
            if not dry and not SET_LOCK.acquire(blocking=False):
                return self._send_json({'status': 'error', 'message': tr('err.busy')}, 409)
            try:
                plan = ford_db.import_rows(CFG, str(d.get('csv') or ''), CFG['paths']['station_names'],
                                           dry_run=dry, token=d.get('token'))
            finally:
                if not dry: SET_LOCK.release()
            return self._send_json(dict(plan, status='ok', dry_run=dry))
        except ford_db.EditError as e:
            return self._edit_error(e)
        except Exception as e:
            logger.exception("handle_stations_import"); self.send_error(500, str(e))

    def handle_delete_records(self):
        try:
            data = json.loads(self.rfile.read(int(self.headers['Content-Length'])).decode())
            del_c = 0
            for db_t in ['fm','dab']:
                if db_t in data and data[db_t]:
                    db_path = DB_FM if db_t == 'fm' else DB_DAB
                    # first delete of the day keeps a copy of the DB as it was (*.bak-del-YYYYMMDD)
                    bak = f"{db_path}.bak-del-{time.strftime('%Y%m%d')}"
                    if not os.path.exists(bak):
                        with sqlite3.connect(db_path) as src, sqlite3.connect(bak) as dst:
                            src.backup(dst)
                        src.close(); dst.close()
                    conn = sqlite3.connect(db_path); c = conn.cursor()
                    for r in data[db_t]: c.execute(f"DELETE FROM {db_t}_station_logo WHERE rowid=?", (r,)); del_c += c.rowcount
                    conn.commit(); conn.close()
            self.send_response(200); self.send_header('Content-type','application/json'); self.end_headers()
            self.wfile.write(json.dumps({'status':'ok','deleted':del_c}).encode())
        except Exception as e:
            logger.exception("handle_delete_records"); self.send_error(500, str(e))

    def handle_update(self):
        """Start an update via the active source, with the two switches (background).

        Body: `{"database": bool, "logos": bool}` — either may be omitted, in
        which case the config default (`update.database` / `update.logos`) applies.
        Answers `{"status": "started"}`; progress and result: GET /api/update_status.
        """
        try:
            length = int(self.headers.get('Content-Length', 0) or 0)
            data = json.loads(self.rfile.read(length).decode()) if length else {}
            def_db, def_logos = config.update_flags(CFG)
            do_db = bool(data.get('database', def_db))
            do_logos = bool(data.get('logos', def_logos))
            if getattr(SOURCE, 'id', 'none') == 'none':
                return self.send_error(400, tr('err.no_source'))
            if SET_LOCK.locked():            # a set swap / build is running: don't write into it
                return self._send_json({'status': 'error', 'error': tr('err.busy')}, 409)
            if not (do_db or do_logos):
                return self.send_error(400, tr('err.nothing_to_update'))
            # runs in the background: requests are deliberately spaced out (fetch.min_interval),
            # so a full update takes minutes; the UI polls /api/update_status
            if not ford_db.start_update(CFG, SOURCE, database=do_db, logos=do_logos):
                return self._send_json({'status': 'error', 'error': tr('err.update_running')}, 409)
            logger.info("On-demand update started (%s): database=%s logos=%s", SOURCE.id, do_db, do_logos)
            self._send_json({'status': 'started'})
        except Exception as e:
            logger.exception("handle_update"); self.send_error(500, str(e))

    def _send_json(self, obj, code=200):
        self.send_response(code); self.send_header('Content-type', 'application/json'); self.end_headers()
        self.wfile.write(json.dumps(obj, ensure_ascii=False).encode())

    def handle_load_inputs(self):
        """Swap the working image + DBs for an input set, without a restart.

        Body: `{"name": "<subdir of paths.inputs_dir>"}`. Copies the set into the
        working paths (existing ones backed up), re-extracts the image for the
        gallery; the page then reloads to re-render the table.
        """
        try:
            length = int(self.headers.get('Content-Length', 0) or 0)
            data = json.loads(self.rfile.read(length).decode()) if length else {}
            if data.get('usb'):
                # only a directory the app itself found on a mounted stick, never any path
                chosen = next((dict(x, name=config.mount_label(x['mount'])) for x in config.usb_input_dirs()
                               if x['path'] == data['usb']), None)
                origin = ('usb', chosen and chosen['name'])
                if not chosen:
                    return self._send_json({'status': 'error', 'error': tr('err.unknown_usb')}, 400)
            else:
                _base, sets = config.list_input_sets(CFG)
                chosen = next((x for x in sets if x['name'] == data.get('name')), None)
                origin = ('set', chosen and chosen['name'])
                if not chosen:
                    return self._send_json({'status': 'error', 'error': tr('err.unknown_set')}, 404)
            if not SET_LOCK.acquire(blocking=False):
                return self._send_json({'status': 'error', 'error': tr('err.busy')}, 409)
            try:
                t0 = time.time()
                copied, mounted, err = swap_set(chosen['path'], origin)
            finally:
                SET_LOCK.release()
            if err:
                return self._send_json({'status': 'error', 'error': err}, 500)
            resp = {'status': 'ok', 'name': chosen['name'], 'copied': copied,
                    'gallery': mounted, 'seconds': round(time.time() - t0, 1)}
            logger.info(f"Loaded input set: {resp}")
            self._send_json(resp)
        except config.InputError as e:          # incomplete/damaged files: nothing was changed
            logger.warning("Load refused: %s", e)
            self._send_json({'status': 'error', 'error': input_error_text(e)}, 400)
        except Exception as e:
            logger.exception("handle_load_inputs"); self.send_error(500, str(e))

    def handle_load_upload(self):
        """Load the three files uploaded from the browser (multipart fields image/fm/dab).

        Streamed to <work>/upload-<ts>/ (the image is ~150 MB), validated (QNX6 superblock,
        SQLite integrity, expected table), then loaded like a set. A bad file -> 400 and the
        working set is untouched."""
        length = int(self.headers.get('Content-Length', 0) or 0)
        if length <= 0 or length > UPLOAD_MAX:
            return self._send_json({'status': 'error', 'error': tr('err.upload_size')}, 413)
        if not SET_LOCK.acquire(blocking=False):
            return self._send_json({'status': 'error', 'error': tr('err.busy')}, 409)
        up = os.path.join(os.path.dirname(os.path.abspath(IMG_ORIG)), 'upload-' + time.strftime('%Y%m%d-%H%M%S'))
        try:
            t0 = time.time()
            os.makedirs(up, exist_ok=True)
            try:
                paths = stream_multipart(self.rfile, length, self.headers.get('Content-Type'), up)
                check_upload(paths)
            except ValueError as e:
                return self._send_json({'status': 'error', 'error': str(e)}, 400)
            copied, mounted, err = swap_set(up, ('upload', ', '.join(UPLOAD_FIELDS.values())))
            if err:
                return self._send_json({'status': 'error', 'error': err}, 500)
            resp = {'status': 'ok', 'copied': copied, 'gallery': mounted, 'seconds': round(time.time() - t0, 1)}
            logger.info(f"Loaded uploaded files: {resp}")
            self._send_json(resp)
        except Exception as e:
            logger.exception("handle_load_upload"); self.send_error(500, str(e))
        finally:
            shutil.rmtree(up, ignore_errors=True)
            SET_LOCK.release()

    def handle_settings(self):
        try:
            self._send_json(save_settings(self._json_body()))
        except ValueError as e:
            self._send_json({'status': 'error', 'error': str(e)}, 400)
        except Exception as e:
            logger.exception("handle_settings"); self.send_error(500, str(e))

    def handle_rebuild_image(self):
        """Build a new QNX6 image (original + logos_sync5) in pure Python."""
        if not SET_LOCK.acquire(blocking=False):
            return self._send_json({'status': 'error', 'error': tr('err.busy')}, 409)
        try:
            self._rebuild_image()
        finally:
            SET_LOCK.release()

    def _rebuild_image(self):
        try:
            length = int(self.headers.get('Content-Length', 0) or 0)
            data = json.loads(self.rfile.read(length).decode()) if length else {}
            self._send_json(build_image(bool(data.get('overwrite'))))
        except Exception as e:
            logger.error(f"Image rebuild error: {e}")
            self.send_error(500, str(e))

    def handle_usb_package(self):
        """Pack the image + FM/DAB DBs as a SyncMyMod USB package.

        Body: `{"image": "auto"|"built"|"loaded"|"build"|"restore", "overwrite": bool,
        "target": "<mounted stick>"|""}`. "build" first builds the image from the
        loaded one + logos_sync5/ (`overwrite` as in /api/rebuild_image) and packs that.
        "restore" packs the loaded image + the DBs as loaded (work/.orig), i.e. undoes every change.
        With a target the package is written onto the stick, otherwise zipped
        into paths.usb_dir and offered under /download/<zip>.
        """
        try:
            length = int(self.headers.get('Content-Length', 0) or 0)
            data = json.loads(self.rfile.read(length).decode()) if length else {}
            # default (basic mode) = build now, so the package is exactly what the preview shows
            which = data.get('image') if data.get('image') in ('auto', 'built', 'loaded', 'build', 'restore') \
                else 'build'
            dbs = (DB_FM, DB_DAB)
            if which == 'restore':
                # undo: the loaded image and the DBs exactly as they were loaded (work/.orig snapshot),
                # not the working copies the user may have edited since
                dbs = tuple(config.loaded_original(CFG, n) for n in ('fm_station_logo.db', 'dab_station_logo.db'))
                if not all(dbs):
                    return self._send_json({'status': 'error', 'error': tr('err.restore_no_original')}, 400)
            if which == 'built' and not usb_package.built_matches(CFG):
                return self._send_json({'status': 'error', 'error': tr('err.no_built_image')}, 400)
            if not SET_LOCK.acquire(blocking=False):
                return self._send_json({'status': 'error', 'error': tr('err.busy')}, 409)
            try:
                built = None
                if which == 'build':
                    built = build_image(bool(data.get('overwrite')))
                    which = 'built'
                if which == 'restore':
                    kind, img = 'restore', IMG_ORIG
                else:
                    kind, img = usb_package.pick_image(CFG, which)
                # the label the user sees ("E:", "01_set", "uploaded files"), not a temp folder name
                info = config.loaded_info(CFG)
                label = info.get('label') or os.path.basename(info.get('path') or '')
                if info.get('kind') == 'upload':
                    label = 'uploaded files' + (f" {info['time']}" if info.get('time') else '')
                resp = usb_package.build_package(
                    img, *dbs, CFG['paths']['usb_dir'], target=data.get('target') or None,
                    source=label, image_note=kind)
            finally:
                SET_LOCK.release()
            resp['kind'] = kind
            resp['build'] = built
            resp.pop('zip_path', None)
            logger.info(f"USB package: {resp}")
            self._send_json(resp)
        except (ValueError, IOError) as e:
            logger.error(f"USB package error: {e}")
            self._send_json({'status': 'error', 'error': str(e)}, 400)
        except Exception as e:
            logger.exception("handle_usb_package"); self.send_error(500, str(e))

    def handle_logo_versions(self):
        """Save per-file version picks. Body: `{"choices": {"E2/NAME.PNG": "image"|"sync5"|"styled"|null}}`."""
        try:
            length = int(self.headers.get('Content-Length', 0) or 0)
            data = json.loads(self.rfile.read(length).decode()) if length else {}
            ch = data.get('choices') or {}
            key_ok = re.compile(r'[0-9A-Fa-f]{2}/[A-Za-z0-9_\-]{1,40}\.PNG', re.I).fullmatch
            if not isinstance(ch, dict) or any(v not in usb_package.VERSIONS + (None,) for v in ch.values()) \
                    or not all(isinstance(k, str) and key_ok(k) for k in ch):
                return self._send_json({'status': 'error', 'error': 'bad choices'}, 400)
            cur = usb_package.save_choices(CFG['paths']['logo_choices'], ch)
            logger.info(f"Logo version choices saved: {len(ch)} changed, {len(cur)} total")
            self._send_json({'status': 'ok', 'saved': len(ch), 'total': len(cur)})
        except Exception as e:
            logger.exception("handle_logo_versions"); self.send_error(500, str(e))

    def handle_style(self):
        """🖌 Logo style: /api/style_preview, /api/style_apply, /api/style_presets (see style_*)."""
        try:
            length = int(self.headers.get('Content-Length', 0) or 0)
            data = json.loads(self.rfile.read(length).decode()) if length else {}
            if self.path == '/api/style_presets':
                return self._send_json(style_preset(data))
            # not during a build or set swap: the sources and styled_dir are in use
            if not SET_LOCK.acquire(blocking=False):
                return self._send_json({'status': 'error', 'error': tr('err.busy')}, 409)
            try:
                if self.path == '/api/style_ai':
                    job = style_ai_prepare(data)
                else:
                    fn = style_preview if self.path == '/api/style_preview' else style_apply
                    return self._send_json(fn(data))
            finally:
                SET_LOCK.release()
            if job.get('status'):
                return self._send_json(job)                     # scan
            return self._send_json(style_ai_run(job))           # network calls: lock released
        except ValueError as e:
            self._send_json({'status': 'error', 'error': str(e)}, 400)
        except Exception as e:
            logger.exception("handle_style"); self.send_error(500, str(e))

    def _usb_zip(self, name):
        """Path of a packaged zip in paths.usb_dir, or None (name must be one we made)."""
        if not re.fullmatch(r'usb_logos_\d{8}-\d{6}\.zip', name):
            return None
        return os.path.join(CFG['paths']['usb_dir'], name)

    def do_GET(self):
        try:
            return self._do_get()
        except ConnectionError:              # browser went away (Broken pipe / reset / WinError 10053)
            pass
        except Exception:  # noqa: BLE001 - always answer instead of dropping the connection
            logger.exception("GET %s", self.path)
            try:
                self._send_json({'status': 'error', 'error': tr('err.internal')}, 500)
            except OSError:
                pass

    def _do_get(self):
        if not self._host_allowed():
            return self._forbidden()
        if self.path == '/favicon.ico':          # no icon: answer quietly instead of a 404 per page load
            self.send_response(204); self.end_headers(); return
        if self.path.startswith('/web/'):
            return self._serve_web_asset()

        if self.path.startswith('/download/'):
            name = os.path.basename(self.path)
            fn = {os.path.basename(p): p for p in (IMG_ORIG, IMG_OUT)}.get(name) or self._usb_zip(name)
            if fn and os.path.exists(fn):
                sz = os.path.getsize(fn)
                self.send_response(200)
                self.send_header('Content-type', 'application/octet-stream')
                self.send_header('Content-Disposition', f'attachment; filename="{name}"')
                self.send_header('Content-Length', str(sz))
                self.end_headers()
                with open(fn, 'rb') as f:
                    while True:
                        chunk = f.read(1 << 20)
                        if not chunk: break
                        self.wfile.write(chunk)
                return
            return self.send_error(404)

        if self.path.startswith(('/logos/', '/img_gallery/', '/logos_sync5/', '/logos_styled/')):
            import urllib.parse as _up
            parts = _up.unquote(self.path.split('?',1)[0]).strip('/').split('/')
            if len(parts)>=3:
                if parts[0]=='img_gallery': f_p = safe_join(MOUNT_DIR, parts[1], parts[2])
                elif parts[0]=='logos_sync5': f_p = safe_join(LOGO_DIR, parts[1], parts[2])
                elif parts[0]=='logos_styled': f_p = safe_join(CFG['paths']['styled_dir'], parts[1], parts[2])
                else: f_p = logo_source(parts[1], parts[2])[1]
                if f_p and os.path.isfile(f_p):
                    return self._send_png(f_p)
            self.send_response(404); self.send_header('Cache-Control', 'no-store'); self.end_headers(); return

        if self.path == '/api/update_status':
            st = ford_db.PROGRESS.snapshot()
            err = st.get('error') or ((st.get('result') or {}).get('error'))
            if err:
                st['error_text'] = tr(err['key'], **err.get('fmt', {}))
            return self._send_json(st)

        if self.path in ('/api/usb_targets', '/api/pkg_status', '/api/logo_versions'):
            # not while an image is being built/swapped: image_out may be half-written
            if not SET_LOCK.try_read():
                return self._send_json({'status': 'error', 'error': tr('err.busy')}, 409)
            try:
                if self.path == '/api/logo_versions':
                    return self._send_json(logo_versions())
                if self.path == '/api/pkg_status':
                    return self._send_json(dict(pkg_report(), status='ok', ver=logo_ver()))
                img = {k: os.path.exists(p) and time.strftime('%Y-%m-%d %H:%M', time.localtime(os.path.getmtime(p)))
                       for k, p in (('built', IMG_OUT), ('loaded', IMG_ORIG))}
                stale = bool(img['built']) and not usb_package.built_matches(CFG)
                if stale:
                    img['built'] = False   # built from another set/image: never offer it
                counts = {k: (d and {x: len(v) for x, v in pkg_report(k).items() if isinstance(v, list)})
                          for k, d in img.items()}
                return self._send_json({'status': 'ok', 'targets': usb_package.usb_targets(), 'images': img, 'stale': stale,
                                        'auto': usb_package.pick_image(CFG)[0], 'report': counts})
            finally:
                SET_LOCK.end_read()

        if self.path == '/api/style_info':
            return self._send_json(style_info())

        if self.path == '/api/session':
            return self._send_json(session_state())

        if self.path == '/api/settings':
            return self._send_json(settings_info())

        if self.path == '/api/usb_inputs':
            return self._send_json({'status': 'ok', 'dirs': config.usb_input_dirs(),
                                    'active': config.active_input_dir(CFG)})

        if self.path == '/api/changes':
            if not SET_LOCK.try_read():
                return self._send_json({'status': 'error', 'error': tr('err.busy')}, 409)
            try:
                return self._send_json(changes_summary())
            finally:
                SET_LOCK.end_read()

        if self.path == '/api/inputs':
            base, sets = config.list_input_sets(CFG)
            return self._send_json({'inputs_dir': base, 'sets': sets})

        if self.path.startswith('/api/stations/export'):
            import urllib.parse
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            country = (q.get('country', [''])[0] or '').upper()
            if not re.fullmatch(r'[0-9A-F]{2}-[0-9A-F]', country):
                return self.send_error(400, tr('err.no_ecc'))
            body = ford_db.export_rows(CFG, country, CFG['paths']['station_names']).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/csv; charset=utf-8')
            self.send_header('Content-Disposition', f'attachment; filename="stations_{country}.csv"')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers(); self.wfile.write(body)
            return

        if self.path.startswith('/api/gallery'):
            import urllib.parse
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            ecc, sid, name = params.get('ecc',[''])[0].upper(), params.get('sid',[''])[0].upper(), params.get('name',[''])[0].upper()
            if not ecc: return self.send_error(400, tr('err.no_ecc'))
            ocr_m, fn_to_n, sid_to_n = {}, {}, {}
            if os.path.exists(DB_OCR):
                conn = config.connect_ro(DB_OCR)
                try:
                    for r in conn.execute("SELECT filename, recognized_text FROM logo_text"): ocr_m[r[0].upper().split('.')[0]] = r[1].upper()
                finally:
                    conn.close()
            if os.path.exists('output.csv'):
                for st in _source_stations():
                    if st['ecc'] == ecc: sid_to_n[st['id'].strip().upper()] = st['program'].strip().upper()
            for db_f, db_t in [(DB_FM, 'fm'), (DB_DAB, 'dab')]:
                if os.path.exists(db_f):
                    conn = config.connect_ro(db_f)
                    try:
                        found = conn.execute(f"SELECT STATION_ID, LOGO_FILENAME FROM {db_t}_station_logo "
                                             f"WHERE upper(ECC)=?", (ecc,)).fetchall()
                    finally:
                        conn.close()
                    for r in found:
                        s, f = (r[0] or "").upper(), (r[1] or "").upper()
                        if f and s in sid_to_n:
                            if f not in fn_to_n: fn_to_n[f] = set()
                            fn_to_n[f].add(sid_to_n[s])
            t_path = safe_join(MOUNT_DIR, ecc)
            logos = []
            try:
                files = [f for f in os.listdir(t_path) if f.upper().endswith('.PNG')] if (t_path and os.path.isdir(t_path)) else []
            except OSError as e:
                logger.error("Gallery read failed for %s: %s", t_path, e); files = []
            if files:
                kws = [w for w in name.split() if len(w)>=3]
                def get_score(f):
                    b = f.upper().split('.')[0]
                    if sid and sid==b: return 0
                    if sid and sid in b: return 1
                    if kws and b in ocr_m:
                        txt = ocr_m[b]
                        for kw in kws:
                            if kw==txt: return 2
                            if kw in txt: return 3
                    if kws and b in fn_to_n:
                        for an in fn_to_n[b]:
                            for kw in kws:
                                if kw in an: return 4
                    if kws:
                        for kw in kws:
                            if kw in b: return 5
                    return 100
                files.sort(key=lambda f: (get_score(f), f.upper()))
                for f in files:
                    b = f.upper().split('.')[0]
                    logos.append({'file':f, 'ocr':ocr_m.get(b,""), 'score':get_score(f)})
            self.send_response(200); self.send_header('Content-type','application/json'); self.end_headers()
            self.wfile.write(json.dumps({'logos':logos}).encode())
            return

        if self.path.split('?', 1)[0] == '/':
            # the table holds one country at a time (all DB rows would be ~30k): ?country= or the cookie
            import urllib.parse as _up
            q = _up.parse_qs(self.path.partition('?')[2]).get('country', [''])[0]
            ck = re.search(r'(?:^|;\s*)lu_country=([^;]+)', self.headers.get('Cookie') or '')
            body = render_index(q or (ck.group(1) if ck else '')).encode()
            self.send_response(200); self.send_header('Content-type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body))); self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404); self.end_headers()

    def _send_png(self, path):
        """PNG with revalidation: the same URL can mean another file after a set swap."""
        st = os.stat(path)
        etag = f'"{logo_ver()}-{int(st.st_mtime)}-{st.st_size}"'
        if self.headers.get('If-None-Match') == etag:
            self.send_response(304); self.send_header('ETag', etag); self.end_headers(); return
        with open(path, 'rb') as f: data = f.read()
        self.send_response(200); self.send_header('Content-type', 'image/png')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-cache'); self.send_header('ETag', etag)
        self.end_headers(); self.wfile.write(data)

    def _serve_web_asset(self):
        import mimetypes
        rel = self.path.split('?', 1)[0][len('/web/'):]
        fp = safe_join(WEB_DIR, *[s for s in rel.split('/') if s])
        if fp and os.path.isfile(fp):
            ctype = mimetypes.guess_type(fp)[0] or 'application/octet-stream'
            with open(fp, 'rb') as f:
                data = f.read()
            self.send_response(200); self.send_header('Content-type', ctype)
            self.send_header('Content-Length', str(len(data))); self.end_headers()
            self.wfile.write(data); return
        self.send_response(404); self.end_headers()


def _country_label(code):
    """'PL (E2-3)' for an RDS country code, or the bare code when it has no name."""
    name = logo_style.country_name(code)
    return f"{name} ({code})" if name else code


def _row_countries():
    """{"E2-3": rows} over both DBs — RDS country = ECC + first hex digit of the PI code."""
    out = {}
    for db_f, t in [(DB_FM, 'fm'), (DB_DAB, 'dab')]:
        if os.path.exists(db_f):
            conn = config.connect_ro(db_f)
            for ecc, d, n in conn.execute(f"SELECT upper(ECC), upper(substr(STATION_ID,1,1)), count(*) "
                                          f"FROM {t}_station_logo WHERE STATION_ID != '' GROUP BY 1, 2"):
                k = f"{ecc or '?'}-{d}"
                out[k] = out.get(k, 0) + n
            conn.close()
    return out


def pick_country(want, counts):
    """`want` if the DBs have it, else Poland (E2-3), else the biggest country."""
    if want in counts:
        return want
    if 'E2-3' in counts:
        return 'E2-3'
    return max(counts, key=counts.get) if counts else ''


def _source_stations():
    """Station dicts from the source cache `output.csv` (with ECC; old 5-column
    files get the active source's default ECC). [] when there is no cache."""
    if not os.path.exists('output.csv'):
        return []
    default_ecc = getattr(SOURCE, 'default_ecc', '') or ''
    with open('output.csv', 'r', encoding='utf-8') as f:
        rdr = csv.reader(f); next(rdr, None)
        return [ford_db._station_from_csv(r, default_ecc) for r in rdr if len(r) >= 5]


def _row_ctx(country):
    """Everything needed to render rows of one RDS country ("E2-3"): DB rows,
    source names/links (output.csv), editor names, duplicate counts, logo picks."""
    ecc, _, digit = country.partition('-')
    ecc, digit = ecc.upper(), digit.upper()
    st_info, rows = {}, []
    for st in _source_stations():
        if st['ecc'] != ecc:
            continue
        sid = st['id'].strip().upper()
        is_dab, freq_db = SOURCE.classify_freq(st['freq']) if SOURCE else (True, None)
        fp = int(freq_db) / 1000.0 if (not is_dab and freq_db) else st['freq']
        st_info.setdefault(sid, []).append({'name': st['program'], 'link': st['link'],
                                            'logo_url': st['logo'], 'freq_parsed': fp})
    for db_f, t in [(DB_FM,'fm'), (DB_DAB,'dab')]:
        if os.path.exists(db_f):
            conn = config.connect_ro(db_f)
            try:
                found = conn.execute(f"SELECT rowid, STATION_ID, FREQUENCY, ECC, LOGO_FILENAME, RECEIVER_TYPE FROM {t}_station_logo "
                                     f"WHERE upper(ECC)=? AND upper(substr(STATION_ID,1,1))=? ORDER BY STATION_ID", (ecc, digit)).fetchall()
            finally:
                conn.close()
            for r in found:
                rows.append({'type':t.upper(), 'rowid':r[0], 'id':r[1], 'freq':r[2], 'ecc':r[3], 'logo':r[4], 'rt': r[5] or ''})
    dup_c = {}
    for r in rows: k=(r['type'],r['id'],r['freq'],r['ecc']); dup_c[k]=dup_c.get(k,0)+1
    return {'rows': rows, 'st_info': st_info, 'dup_c': dup_c, 'choices': _choices(), 'ver': logo_ver(),
            'names': ford_db.load_names(CFG['paths']['station_names'])}


def _safe_href(url):
    """Links come from source data (CSV, scraped pages): only http(s), never javascript: & co."""
    u = str(url or '').strip().lower()
    return u.startswith(('http://', 'https://'))


def _row_html(r, ctx):
    """One <tr> of the station table (also returned by /api/station after an edit)."""
    esc = lambda v: html.escape(str(v if v is not None else ''), quote=True)  # noqa: E731
    fd, f_mhz = r['freq'], None
    if r['type']=='FM':
        try: v = int(r['freq'])/1000.0; fd = f"{ford_db._mhz(r['freq'])} MHz"; f_mhz = v
        except (TypeError, ValueError): pass
    else: fd = "DAB+"
    cands = ctx['st_info'].get((r['id'] or '').upper(), [])
    match = None
    if f_mhz:
        for c in cands:
            if isinstance(c['freq_parsed'], float) and abs(c['freq_parsed']-f_mhz)<0.05: match=c; break
    if not match and cands: match = cands[0]
    own_name = ctx['names'].get(f"{(r['ecc'] or '').upper()}/{(r['id'] or '').upper()}", '')
    sn = own_name or (match['name'] if match else "")
    sl = match['link'] if match else ""; slu = match['logo_url'] if match else ""
    src = f"/logos/{esc(r['ecc'])}/{esc(r['logo'])}.PNG?v={ctx['ver']}"; rk = f"{r['type']}_{r['id']}_{r['freq']}"
    # no logo name yet: no <img> (it would request /logos/E2/.PNG); app.js adds one when a logo is set
    img_tag = (f'<img id="img_{esc(rk)}" src="{src}" loading="lazy" onerror="this.style.opacity=0.2" '
               f'class="logo-preview">') if r['logo'] else ''
    is_d = ctx['dup_c'].get((r['type'],r['id'],r['freq'],r['ecc']), 0)>1
    da = 'data-is-duplicate="true"' if is_d else ''
    rs = 'style="background-color: #3e2e2e;"' if is_d else ''
    safe_link = _safe_href(sl)
    link = (f'<a href="{esc(sl)}" target="_blank" rel="noopener">{esc(sn)}</a>' if (safe_link and sn)
            else (esc(sn) or "-"))
    where = logo_source(r['ecc'], f"{r['logo']}.PNG", ctx['choices'])[0] if r['logo'] else None
    tag = f'<span class="src-tag src-{where or "none"}" title="{T["src." + (where or "none") + "_title"]}">{T["src." + (where or "none")]}</span>'
    own = r.get('rt') in ford_db.OWN_TYPES.values()
    return f"""<tr {da} {rs}>
            <td><input type="checkbox" class="row-check" data-type="{esc(r['type'])}" data-rowid="{r['rowid']}" data-id="{esc(r['id'])}" data-ecc="{esc(r['ecc'])}" data-freq="{esc(r['freq'])}" data-slu="{esc(slu)}" data-name="{esc(sn)}" data-logo="{esc(r['logo'])}" data-rt="{esc(r.get('rt'))}"></td>
            <td class="type-{esc(r['type'].lower())}">{esc(r['type'])}{'' if not own else ' <span class="own-tag" title="' + esc(T['st.own_title']) + '">+</span>'}</td><td class="mono">{esc(r['id'])}</td><td>{link}</td><td class="mono">{esc(fd)}</td><td class="mono small">{esc(r['ecc'])}</td>
            <td class="logo-cell">{img_tag}{tag}<span class="pkg-tag"></span></td>
            <td class="mono small">{esc(r['logo'])}</td><td style="text-align:center">{_status_cell(match)}</td>
            <td class="actions"><button onclick="triggerUploadRow(this)">{T['btn.change']}</button>
            <button class="secondary" onclick="openGalleryRow(this)">{T['btn.gallery']}</button>
            <button class="secondary" onclick="editStation(this)" title="{esc(T['st.edit_title'])}">✎</button></td></tr>"""


def _status_cell(match):
    """Status column: ✅ = station found in the data source; otherwise a neutral muted
    mark (not an error) — "—" when no source is configured, "·" when not in its data."""
    if match:
        return f'<span title="{html.escape(T["st.status_found"], quote=True)}">✅</span>'
    key, mark = ('st.status_nosrc', '—') if getattr(SOURCE, 'id', 'none') == 'none' else ('st.status_missing', '·')
    return f'<span class="muted" title="{html.escape(T[key], quote=True)}">{mark}</span>'


def _build_rows(country):
    ctx = _row_ctx(country)
    return "".join(_row_html(r, ctx) for r in ctx['rows'])


def _ui_config():
    """The slice of config the page needs (update-switch defaults, source name)."""
    do_db, do_logos = config.update_flags(CFG)
    active = config.active_input_dir(CFG)
    return {'active_set': os.path.basename(active) if active else '', 'img_ver': IMG_VER, 'img_ver_logos': logo_ver(),
            'session': session_state(),
            'update': {'database': do_db, 'logos': do_logos,
                       'on_startup': bool((CFG.get('update') or {}).get('on_startup', False))},
            'source': {'id': getattr(SOURCE, 'id', ''), 'name': getattr(SOURCE, 'name', ''),
                       'enabled': getattr(SOURCE, 'id', 'none') != 'none'},
            # RDS country (ECC-first PI digit) -> ISO code, for the country filter
            'countries': {f'{e}-{d}': c for e, m in logo_style.COUNTRIES.items() for d, c in m.items()}}


def render_index(country=''):
    """Render web/index.html with language, the injected i18n catalog and the table rows
    of one RDS country (`country` "E2-3"; unknown -> pick_country())."""
    counts = _row_countries()
    country = pick_country(country.upper(), counts)
    cfg = dict(_ui_config(), country=country, country_rows=counts)
    def js(obj):        # JSON inside <script>: "</" must not close the tag (names come from folders/DBs)
        return json.dumps(obj, ensure_ascii=False).replace('</', '<\\/')
    parts = {'__LANG__': html.escape(str(CFG.get('language', 'en'))), '__ROWS__': _build_rows(country),
             '__T_JSON__': js(T), '__CFG_JSON__': js(cfg)}
    # one pass: a station or folder named like a placeholder must not be substituted again
    return re.sub('__(?:LANG|ROWS|T_JSON|CFG_JSON)__', lambda m: parts[m.group(0)], INDEX_TEMPLATE)


class _AppServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    # Linux/macOS: SO_REUSEADDR only skips TIME_WAIT after a restart. On Windows it would let us bind a
    # port that another program is ALREADY serving (the browser then shows that program), so there
    # the socket is exclusive instead.
    allow_reuse_address = os.name != 'nt'

    def server_bind(self):
        import socket
        if os.name == 'nt' and hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def _port_answers(port):
    """Something already accepts connections on this port on 127.0.0.1 or ::1 (e.g. Docker, WSL)."""
    import socket
    for fam, addr in ((socket.AF_INET, '127.0.0.1'), (socket.AF_INET6, '::1')):
        try:
            with socket.socket(fam, socket.SOCK_STREAM) as s:
                s.settimeout(0.3)
                if s.connect_ex((addr, port)) == 0:
                    return True
        except OSError:
            pass
    return False


def _bind_server(host, port, tries=20):
    """Bind the HTTP server; if `port` is taken, say so and use the next free one (up to +tries)."""
    for p in range(port, port + tries + 1):
        if not _port_answers(p):
            try:
                return _AppServer((host, p), StationViewerHandler), p
            except OSError as e:
                logger.debug("bind %s:%s: %s", host, p, e)
        if p == port:
            print(f"Port {port} is already used by another program; trying the next free port. "
                  f"(Choose one with --port or 'port:' in config.yaml.)", flush=True)
    sys.exit(f"No free port between {port} and {port + tries}. Start with --port <number>.")


def run(cfg, source, log_file='sync_log.txt', config_path='config.yaml', open_browser=False):
    """Configure the runtime context from cfg/source and start the threaded server.

    open_browser: open the UI in the default browser once the port is bound.
    """
    global CFG, SOURCE, T, INDEX_TEMPLATE, PORT, LOGO_DIR_NAME, LOGO_DIR, MOUNT_DIR
    global DB_FM, DB_DAB, DB_OCR, IMG_ORIG, IMG_OUT, WORK_DIR, CONFIG_PATH
    CFG, SOURCE, CONFIG_PATH = cfg, source, config_path
    p = cfg['paths']
    PORT = cfg['port']
    LOGO_DIR_NAME = p['logo_dir']; LOGO_DIR = os.path.abspath(p['logo_dir'])
    MOUNT_DIR = p['mount_dir']; DB_FM = p['db_fm']; DB_DAB = p['db_dab']; DB_OCR = p['db_ocr']
    IMG_ORIG = p['image']; IMG_OUT = p['image_out']; WORK_DIR = p['work_dir']
    T = i18n.catalog()
    T['_btn_update'] = i18n.t('btn.update_source', source=source.name)
    with open(os.path.join(WEB_DIR, 'index.html'), 'r', encoding='utf-8') as f:
        INDEX_TEMPLATE = f.read()

    check_db_writable()
    ensure_mounted()
    host = (cfg.get('server') or {}).get('host') or '127.0.0.1'
    httpd, PORT = _bind_server(host, PORT)
    if host in ('127.0.0.1', 'localhost', '::1'):
        print(f"SERVER: http://127.0.0.1:{PORT}", flush=True)
    else:
        lan = get_local_ip() if host in ('0.0.0.0', '', '::') else host
        print(f"SERVER: http://127.0.0.1:{PORT} / http://{lan}:{PORT}", flush=True)
        logger.warning("Listening on %s: anyone on this network can open the UI and change the "
                       "loaded databases and logos (no password). Set server.host: 127.0.0.1 "
                       "to allow this computer only.", host)
    # No update at launch unless update.on_startup is on: the app opens as a preview
    # of the frequencies/logos already present in this img/db set.
    upd = cfg.get('update') or {}
    do_db, do_logos = config.update_flags(cfg)
    if upd.get('on_startup') and (do_db or do_logos) and 'requests' in sys.modules:
        logger.info("Startup update enabled (database=%s, logos=%s).", do_db, do_logos)
        ford_db.startup_update(cfg, source)     # background thread; progress in /api/update_status
    else:
        logger.info("Preview mode: no update at startup — showing the current image/DB set. "
                    "Use the Update button (or set update.on_startup: true).")
    with httpd:
        if open_browser:
            import webbrowser
            # 127.0.0.1, not "localhost": on Windows localhost may resolve to ::1, where another
            # program (WSL relay, Docker) can listen on the same port
            url = f"http://127.0.0.1:{PORT}/"
            threading.Timer(0.5, lambda: webbrowser.open(url) or
                            logger.info("Open %s in your browser.", url)).start()
        httpd.serve_forever()
