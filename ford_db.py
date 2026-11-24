"""ford_db.py — device side of LogosUpdater (Ford Sync 3.4 / QNX6).

This is the part that is intentionally NOT pluggable: the Ford DB schema
(``ECC, STATION_ID, RECEIVER_TYPE, FREQUENCY, LOGO_FILENAME``), the
``output.csv`` cache, logo download/resize into ``logos_sync5/``, and the OCR
matching. Data **sources** (sources/*.py) only produce normalized ``Station``
records; everything here maps them onto the device + image.

All paths come from the config dict (``cfg['paths']``). Logs stay English.
"""

from __future__ import annotations

import csv
import hashlib
import json
import io
import logging
import os
import re
import sqlite3
from collections import Counter

import threading
import time

from PIL import Image

import fetcher

logger = logging.getLogger(__name__)

CSV_FILE = "output.csv"
# Logo URLs come from data (output.csv / plugin data): public hosts only, never this computer or the LAN.
# Tests that serve logos from 127.0.0.1 set this to None.
LOGO_URL_CHECK = fetcher.public_http_url
VERSION_FILE = "last_change.txt"


# --------------------------------------------------------------------- helpers
def safe_join(base, *segs):
    """Join paths and guarantee the result stays INSIDE base (anti path-traversal)."""
    for s in segs:
        # ':' too: on Windows "C:evil" is a drive-relative path, "x:y" an alternate data stream
        if not s or s in (".", "..") or any(c in s for c in ("/", "\\", "\x00", ":")):
            return None
    base = os.path.abspath(base)
    p = os.path.abspath(os.path.join(base, *segs))
    try:                       # commonpath also handles a drive root base ("C:\\") correctly
        if os.path.commonpath([base, p]) != base:
            return None
    except ValueError:         # different drives
        return None
    return p


def resize_image(image_bytes, max_size=(160, 120)):
    try:
        img = Image.open(io.BytesIO(image_bytes))
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        try:
            resample_filter = Image.LANCZOS
        except AttributeError:
            resample_filter = Image.BICUBIC
        img.thumbnail(max_size, resample_filter)
        new_img = Image.new("RGBA", max_size, (0, 0, 0, 0))
        offset = ((max_size[0] - img.size[0]) // 2, (max_size[1] - img.size[1]) // 2)
        new_img.paste(img, offset)
        out = io.BytesIO()
        new_img.save(out, format="PNG")
        return out.getvalue()
    except Exception as e:  # noqa: BLE001
        logger.error("Resize error: %s", e)
        return None


# ------------------------------------------------------------- validation
_HEX2 = re.compile(r"[0-9A-F]{2}")
_HEX4 = re.compile(r"[0-9A-F]{4}")
FM_MIN_KHZ, FM_MAX_KHZ = 87500, 108000


def validate_station(ecc, sid, is_dab, freq_db):
    """Return ``None`` if the station can be written, else a short English reason.

    ``ecc``: RDS country (2 hex), ``sid``: PI code (4 hex), ``freq_db``: the
    6-digit FM string (ignored for DAB). Shared by source updates and the editor.
    """
    if not ecc:
        return "no country (ECC)"
    if not _HEX2.fullmatch(str(ecc).upper()):
        return "invalid ECC"
    if not _HEX4.fullmatch(str(sid or "").upper()):
        return "invalid PI"
    if not is_dab:
        try:
            khz = int(freq_db)
        except (TypeError, ValueError):
            return "invalid frequency"
        if not FM_MIN_KHZ <= khz <= FM_MAX_KHZ:
            return "invalid frequency"
    return None


def _freq_key(freq):
    """FM frequency as stored ('098500', '98500', 98500) -> canonical '098500'."""
    try:
        return f"{int(freq):06d}"
    except (TypeError, ValueError):
        return str(freq or "")


def _station_from_csv(r, default_ecc):
    """One `output.csv` row -> station dict. 5 columns = old format (no ECC)."""
    ecc = (r[5].strip() if len(r) >= 6 else "") or (default_ecc or "")
    return {"id": r[0], "program": r[1], "freq": r[2], "link": r[3], "logo": r[4],
            "ecc": ecc.upper()}


# ------------------------------------------------------------- update progress
class UpdateProgress:
    """State of the running update for the UI (/api/update_status); thread-safe.

    Phases: ``signature`` (what changed?), ``list`` (station list), ``pages``
    (station detail pages for logo URLs), ``logos`` (logo files), ``database``.
    The ETA is estimated from the pace so far within the current phase - with the
    polite rate limit that is roughly ``min_interval`` seconds per request.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.cancel = threading.Event()
        self._s = {"running": False}

    def start(self, source_name):
        with self._lock:
            self.cancel.clear()
            self._s = {"running": True, "source": source_name, "phase": "", "done": 0, "total": None,
                       "message": "", "started": time.time(), "phase_started": time.time(),
                       "result": None, "error": None}

    def phase(self, name, total=None):
        with self._lock:
            self._s.update(phase=name, done=0, total=total, message="", phase_started=time.time())

    def __call__(self, done, total=None, message=""):
        with self._lock:
            self._s.update(done=done, message=str(message)[:200])
            if total is not None:
                self._s["total"] = total

    def finish(self, result=None, error=None):
        with self._lock:
            self._s.update(running=False, result=result, error=error)

    def cancelled(self):
        return self.cancel.is_set()

    def snapshot(self):
        with self._lock:
            s = dict(self._s)
        now = time.time()
        if s.get("running"):
            s["elapsed"] = round(now - s["started"])
            done, total = s.get("done") or 0, s.get("total")
            if total and done:
                rate = (now - s["phase_started"]) / done
                s["eta"] = round(rate * (total - done))
        s.pop("phase_started", None)
        s.pop("started", None)
        return s


PROGRESS = UpdateProgress()


def _fetch_error_info(exc):
    """SourceError / fetcher errors -> {'key', 'fmt'} for a translated UI message, else None."""
    from fetcher import BudgetExceeded, RobotsDisallowed
    from sources.base import SourceError
    if isinstance(exc, SourceError):
        return {"key": exc.key, "fmt": {k: str(v) for k, v in exc.fmt.items()}}
    if isinstance(exc, RobotsDisallowed):
        return {"key": "err.source_robots", "fmt": {"url": getattr(exc, "url", "")}}
    if isinstance(exc, BudgetExceeded):
        return {"key": "err.source_budget", "fmt": {"count": str(getattr(exc, "count", "") or "")}}
    return None


# ------------------------------------------------------------- update pipeline
def refresh_station_data(cfg, source, allow_fetch=True, phase=None):
    """Return the station list, refreshing `output.csv` from `source` if needed.

    Source-agnostic; `source` is a DataSource instance. Fetches only when the
    source reports a change (or the CSV is missing/URL-less). With
    ``allow_fetch=False`` it never hits the network — it just loads whatever
    `output.csv` already holds (used by a logos-only update).
    """
    force_update = not os.path.exists(CSV_FILE) or os.path.getsize(CSV_FILE) == 0
    if not force_update:
        # Self-heal: CSV exists but has no logo URLs at all -> force a refetch.
        try:
            with open(CSV_FILE, "r", encoding="utf-8") as f:
                rdr = csv.reader(f)
                next(rdr, None)
                if not any(len(r) >= 5 and r[4].strip() for r in rdr):
                    logger.info("output.csv has no logo URLs — forcing a refetch.")
                    force_update = True
        except Exception as e:  # noqa: BLE001
            logger.debug("peek output.csv: %s", e)

    stations = []
    try:
        if not allow_fetch:
            update_needed, signature = False, None
            if force_update:
                logger.info("No usable output.csv — fetching station data first.")
                allow_fetch = True
        if allow_fetch:
            signature = source.change_signature()
            update_needed = True
            # Compare the source's change-signature against our stored one.
            if signature is not None and os.path.exists(VERSION_FILE):
                with open(VERSION_FILE, "r", encoding="utf-8") as f:
                    if f.read().strip() == signature:
                        update_needed = False

        if not update_needed and not force_update:
            logger.info("Source data is current. Loading from CSV...")
            with open(CSV_FILE, "r", encoding="utf-8") as f:
                reader = csv.reader(f)
                next(reader, None)
                for r in reader:
                    if len(r) >= 5:
                        stations.append(_station_from_csv(r, source.default_ecc))
        else:
            logger.info("Fetching station list from source '%s'...", source.id)
            if phase:
                phase("list")
            recs = list(source.fetch_stations())
            for st in recs:
                stations.append({"id": st.id, "program": st.name, "freq": st.freq_raw,
                                 "link": st.link, "logo": st.logo_url or "",
                                 "ecc": (st.ecc or source.default_ecc or "").strip().upper()})

            # Logo URLs: one detail page per distinct link, one at a time. The shared
            # client spaces the requests out (rate limit) and answers repeats from cache.
            from sources.base import Station as _St
            todo = {}
            for st in stations:
                if st["link"] and not st["logo"] and st["link"] not in todo:
                    todo[st["link"]] = _St(id=st["id"], name=st["program"], freq_raw=st["freq"],
                                           link=st["link"], ecc=st["ecc"])
            if phase:
                phase("pages", len(todo))
            by_link = {}
            for i, (lk, st) in enumerate(todo.items()):
                if source.cancelled():
                    logger.info("Update cancelled after %d of %d station pages.", i, len(todo))
                    break
                source.progress(i, len(todo), st.name)
                by_link[lk] = source.fetch_logo_url(st)
            for s in stations:
                if not s["logo"] and by_link.get(s["link"]):
                    s["logo"] = by_link[s["link"]]

            with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["ID", "Name", "Freq", "Link", "Logo", "ECC"])
                for s in stations:
                    w.writerow([s["id"], s["program"], s["freq"], s["link"], s["logo"], s["ecc"]])
            if signature and not source.cancelled():   # a cancelled run must be redone next time
                with open(VERSION_FILE, "w", encoding="utf-8") as f:
                    f.write(signature)

    except Exception as e:  # noqa: BLE001
        if _fetch_error_info(e):
            raise                      # the user must see it (layout changed, robots.txt, budget)
        logger.error("Station data refresh error: %s", e)
    return stations


def update_database(cfg, source, phase=None):
    """Switch 1: refresh station data and write new/changed frequencies to the DBs."""
    stations = refresh_station_data(cfg, source, phase=phase)
    if stations and phase:
        phase("database")
    if not stations:
        logger.warning("No station data — database left untouched.")
        return {"added": 0, "updated": 0, "stations": 0, "skipped": {}}
    stats = sync_database(cfg, source, stations)
    stats["stations"] = len(stations)
    return stats


def update_logos(cfg, source, phase=None):
    """Switch 2: download the logo files that are missing locally.

    Uses the logo URLs already in `output.csv`; only if that cache is unusable
    does it fetch station data first.
    """
    refresh_station_data(cfg, source, allow_fetch=False, phase=phase)
    return download_missing_logos(cfg, source, phase=phase)


def run_update(cfg, source, database=True, logos=True, progress=None):
    """Run an update with the two independent switches. Returns a stats dict.

    `progress` (an UpdateProgress, default the module-wide PROGRESS) receives the
    phase/progress and carries the cancel flag. Errors the user must see (the
    source's SourceError, robots.txt refusal, request budget) end the run with
    ``stats["error"] = {"key", "fmt"}`` instead of raising.
    """
    import sources
    progress = progress or PROGRESS
    t0 = time.time()
    stats = {"database": None, "logos": None,
             "added": 0, "updated": 0, "downloaded": 0, "skipped": {}, "error": None,
             "cancelled": False}
    if not (database or logos):
        logger.info("Update skipped: both switches are off.")
        stats["seconds"] = 0.0
        return stats
    logger.info("Update starting (database=%s, logos=%s) via source '%s'.",
                database, logos, source.id)
    http = sources.shared_http(cfg)
    http.new_run((cfg.get("fetch") or {}).get("budget"))
    source.bind(source.http, progress, progress.cancelled)
    try:
        if database:
            progress.phase("signature")
            db_stats = update_database(cfg, source, phase=progress.phase)
            stats["database"] = db_stats
            stats["added"] = db_stats.get("added", 0)
            stats["updated"] = db_stats.get("updated", 0)
            stats["skipped"] = db_stats.get("skipped", {})
        if logos and not progress.cancelled():
            logo_stats = update_logos(cfg, source, phase=progress.phase)
            stats["logos"] = logo_stats
            stats["downloaded"] = logo_stats.get("downloaded", 0)
            stats["updated"] += logo_stats.get("updated", 0)
    except Exception as e:  # noqa: BLE001
        info = _fetch_error_info(e)
        if not info:
            raise
        logger.warning("Update stopped: %s", e)
        stats["error"] = info
    stats["cancelled"] = progress.cancelled()
    stats["requests"] = dict(getattr(http, "stats", {}) or {})
    stats["seconds"] = round(time.time() - t0, 1)
    logger.info("Update done: %s", stats)
    return stats


def start_update(cfg, source, database=True, logos=True):
    """Run the update in a background thread (UI polls PROGRESS). False if one is running."""
    if PROGRESS.snapshot().get("running"):
        return False
    PROGRESS.start(source.name)

    def work():
        try:
            PROGRESS.finish(result=run_update(cfg, source, database, logos, PROGRESS))
        except Exception as e:  # noqa: BLE001
            logger.exception("update")
            PROGRESS.finish(error={"key": "err.source_failed", "fmt": {"source": source.name, "error": str(e)}})

    threading.Thread(target=work, daemon=True, name="update").start()
    return True


def sync_database(cfg, source, web_stations):
    """Map source stations onto the Ford DBs (+ OCR-assisted logo matching).

    Returns ``{"added": n, "updated": n}`` totalled over both DBs.
    """
    p = cfg["paths"]
    db_ocr, db_fm, db_dab = p["db_ocr"], p["db_fm"], p["db_dab"]
    ocr_keywords = {}
    if os.path.exists(db_ocr):
        try:
            conn = sqlite3.connect(db_ocr)
            for ecc, fname, text in conn.execute(
                "SELECT ecc, filename, recognized_text FROM logo_text"
            ):
                if not text:
                    continue
                for w in text.upper().split():
                    if len(w) >= 3:
                        ocr_keywords.setdefault(w, []).append((ecc, fname))
            conn.close()
        except Exception as e:  # noqa: BLE001
            logger.debug("OCR keywords load: %s", e)

    # Keywords present in MANY different logos are too generic (RADIO, FM...).
    generic_kw = {kw for kw, lst in ocr_keywords.items()
                  if len({f for _, f in lst}) > 10}

    # Validate once per station; rows the device can't use are skipped, never guessed.
    valid, skipped = [], Counter()
    for st in web_stations:
        sid = str(st["id"]).strip().upper()
        ecc = (st.get("ecc") or "").strip().upper()
        is_dab, freq_db = source.classify_freq(str(st["freq"]))
        why = validate_station(ecc, sid, is_dab, freq_db)
        if why:
            skipped[why] += 1
            continue
        valid.append((ecc, sid, st["program"].upper(), is_dab, freq_db))
    if skipped:
        logger.warning("Skipped %d source rows: %s", sum(skipped.values()), dict(skipped))

    totals = {"added": 0, "updated": 0, "skipped": dict(skipped)}
    for db_file, table in [(db_fm, "fm_station_logo"), (db_dab, "dab_station_logo")]:
        if not os.path.exists(db_file):
            continue
        dab_table = table == "dab_station_logo"
        logger.info("Syncing %s...", db_file)
        conn = sqlite3.connect(db_file)
        cur = conn.cursor()
        cur.execute(f"SELECT ECC, STATION_ID, FREQUENCY, LOGO_FILENAME FROM {table}")
        # Key = (ECC, PI, frequency); DAB rows have no meaningful frequency
        # ('0' for ours, '000000' for Ford's), so DAB matches on (ECC, PI) only.
        db_entries = {}
        for e, sid, fr, logo in cur.fetchall():
            k = ((e or "").upper(), (sid or "").upper(), None if dab_table else _freq_key(fr))
            if logo or k not in db_entries:
                db_entries[k] = logo or ""

        to_add, to_update = [], []
        for ecc, sid, name, is_dab, freq_db in valid:
            if is_dab != dab_table:
                continue
            key = (ecc, sid, None if is_dab else freq_db)
            if key not in db_entries:
                to_add.append((ecc, sid, ("2" if is_dab else "1"), ("0" if is_dab else freq_db), ""))
                db_entries[key] = ""

            if not db_entries.get(key):
                kws = [w for w in name.split() if len(w) >= 4 and w not in generic_kw]
                cands = [fn for kw in kws if kw in ocr_keywords
                         for ec, fn in ocr_keywords[kw] if (ec or "").upper() == ecc]
                if cands:
                    ranked = Counter(cands).most_common(2)
                    top_fn, top_n = ranked[0]
                    runner_n = ranked[1][1] if len(ranked) > 1 else 0
                    distinct = len(set(cands))
                    if distinct == 1 or (top_n >= 2 and top_n > runner_n):
                        best = top_fn.split(".")[0]
                        if is_dab:
                            to_update.append((best, ecc, sid))
                        else:
                            to_update.append((best, ecc, sid, freq_db, str(int(freq_db))))

        if to_add:
            cur.executemany(
                f"INSERT INTO {table} (ECC, STATION_ID, RECEIVER_TYPE, FREQUENCY, LOGO_FILENAME) VALUES (?,?,?,?,?)",
                to_add,
            )
        # Only UPDATE our own sync rows (RECEIVER_TYPE 1/2) — never the Ford originals ('F'/'D').
        for up in set(to_update):
            if dab_table:
                cur.execute(
                    f"UPDATE {table} SET LOGO_FILENAME=? WHERE upper(ECC)=? AND upper(STATION_ID)=? "
                    f"AND RECEIVER_TYPE='2'", up)
            else:
                cur.execute(
                    f"UPDATE {table} SET LOGO_FILENAME=? WHERE upper(ECC)=? AND upper(STATION_ID)=? "
                    f"AND (FREQUENCY=? OR FREQUENCY=?) AND RECEIVER_TYPE='1'", up)
        conn.commit()
        conn.close()
        n_add, n_upd = len(to_add), len(set(to_update))
        totals["added"] += n_add
        totals["updated"] += n_upd
        logger.info("DB %s: added %d, updated %d", db_file, n_add, n_upd)
    return totals


def download_missing_logos(cfg, source, progress_every=50, phase=None):
    """Download MISSING logo images (urls already in output.csv) -> logos_sync5/.

    Goes through the shared rate-limited client (kind="image": content type and size
    checked); each distinct URL is downloaded at most once per run.
    """
    import sources
    from fetcher import BudgetExceeded, FetchError, RobotsDisallowed
    p = cfg["paths"]
    logo_dir = os.path.abspath(p["logo_dir"])
    if not os.path.exists(CSV_FILE):
        logger.info("No output.csv — skipping logo download.")
        return {"downloaded": 0, "updated": 0}
    url_map = {}
    with open(CSV_FILE, "r", encoding="utf-8") as f:
        rdr = csv.reader(f)
        next(rdr, None)
        for r in rdr:
            if len(r) < 5 or not r[4].strip():
                continue
            st = _station_from_csv(r, source.default_ecc)
            is_dab, freq_db = source.classify_freq(r[2])
            url_map.setdefault((st["ecc"], r[0].strip().upper()), []).append(
                (is_dab, freq_db, r[4].strip()))
    if not url_map:
        logger.info("output.csv has no logo URLs — refresh data first.")
        return {"downloaded": 0, "updated": 0}

    http = sources.shared_http(cfg)
    # 1) plan: which DB rows need which file from which URL
    plan = []          # (db_f, table, rowid, old_logo, fname, dst, url)
    for db_f, table, want_dab in [(p["db_fm"], "fm_station_logo", False),
                                  (p["db_dab"], "dab_station_logo", True)]:
        if not os.path.exists(db_f):
            continue
        conn = sqlite3.connect(db_f)
        rows = conn.execute(f"SELECT rowid, ECC, STATION_ID, FREQUENCY, LOGO_FILENAME FROM {table}").fetchall()
        conn.close()
        for rowid, ecc, sid, freq, logo in rows:
            ecc = (ecc or "").upper()
            if not ecc:
                continue
            if logo and os.path.exists(os.path.join(logo_dir, ecc, f"{logo}.PNG")):
                continue
            url = None
            for is_dab, freq_db, u in url_map.get((ecc, (sid or "").upper()), []):
                if is_dab != want_dab:
                    continue
                if want_dab or freq_db == freq or (freq_db and str(int(freq_db)) == str(freq)):
                    url = u
                    break
            if not url:
                continue
            fname = logo or hashlib.md5(f"{sid}{freq}".encode("utf-8")).hexdigest()[:8].upper()
            if not _LOGO_RE.fullmatch(fname):       # a name from a foreign DB must not become a path
                continue
            dst_dir = safe_join(logo_dir, ecc)
            if not dst_dir:
                continue
            plan.append((db_f, table, rowid, logo or "", fname, os.path.join(dst_dir, f"{fname}.PNG"), url))

    # 2) download (distinct URLs once), write files, then point the DB rows at them
    urls = list(dict.fromkeys(u for *_, dst, u in plan if not os.path.exists(dst)))
    if phase:
        phase("logos", len(urls))
    got, downloaded, failed = {}, 0, []
    for i, url in enumerate(urls):
        if source.cancelled():
            logger.info("Logo download cancelled after %d of %d.", i, len(urls))
            break
        source.progress(i, len(urls), url.rsplit("/", 1)[-1])
        try:
            resp = http.get(url, kind="image", url_check=LOGO_URL_CHECK)
        except (RobotsDisallowed, BudgetExceeded):
            raise
        except FetchError as e:
            logger.debug("download logo %s: %s", url, e)
            failed.append(f"{url} ({e})")
            continue
        if resp.status == 200:
            img = resize_image(resp.content)      # only what Pillow can read becomes a logo file
            if img:
                got[url] = img
            else:
                failed.append(f"{url} (not a usable image)")
        else:
            failed.append(f"{url} (HTTP {resp.status})")
    if failed:
        logger.warning("%d logo download(s) failed, e.g. %s", len(failed), "; ".join(failed[:3]))
    updated, per_db = 0, {}
    for db_f, table, rowid, old, fname, dst, url in plan:
        if not os.path.exists(dst):
            if url not in got:
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "wb") as g:
                g.write(got[url])
            downloaded += 1
            if downloaded % progress_every == 0:
                logger.info("  ...wrote %d logos", downloaded)
        if fname != old:
            per_db.setdefault((db_f, table), []).append((fname, rowid))
    for (db_f, table), ups in per_db.items():
        conn = sqlite3.connect(db_f)
        conn.executemany(f"UPDATE {table} SET LOGO_FILENAME=? WHERE rowid=?", ups)
        conn.commit()
        conn.close()
        updated += len(ups)
    logger.info("Logo download done: %d files from %d downloads, db updated %d, failed %d.",
                downloaded, len(got), updated, len(failed))
    return {"downloaded": downloaded, "updated": updated, "failed": len(failed)}


def startup_update(cfg, source):
    """Background update at launch, honouring the config switches.

    Only reached when ``update.on_startup`` is true; without it the app starts in
    preview mode and shows whatever the current img/db set already contains.
    Runs like the button: in a thread, visible in /api/update_status.
    """
    u = cfg.get("update") or {}
    return start_update(cfg, source, database=bool(u.get("database", True)),
                        logos=bool(u.get("logos", True)))


# ------------------------------------------------------------- manual editor
# Rows the app owns: RECEIVER_TYPE '1' (FM) / '2' (DAB). Ford's own rows are
# 'F' / 'D' and keep their PI/frequency (only logo changes and deletion allowed).
OWN_TYPES = {"FM": "1", "DAB": "2"}
EXPORT_COLUMNS = ["type", "ecc", "pi", "frequency", "logo", "name", "receiver_type"]
_LOGO_RE = re.compile(r"[A-Za-z0-9_\-]{1,40}")


class EditError(ValueError):
    """Editor refusal; ``code`` is a stable id the UI translates (``err.st_<code>``)."""

    def __init__(self, code, **info):
        super().__init__(code)
        self.code, self.info = code, info


def _db_table(cfg, typ):
    p = cfg["paths"]
    if typ not in ("FM", "DAB"):
        raise EditError("bad_type")
    db = p["db_fm"] if typ == "FM" else p["db_dab"]
    if not os.path.exists(db):          # sqlite3.connect would silently create an empty file
        raise EditError("no_db")
    return db, ("fm_station_logo" if typ == "FM" else "dab_station_logo")


def parse_mhz(value):
    """'98.5' / '98,5' / '98.50 MHz' -> '098500'; '' / None -> None."""
    if value is None or str(value).strip() == "":
        return None
    m = re.fullmatch(r"\s*(\d{2,3})(?:[.,](\d{1,2}))?\s*(?:MHz)?\s*", str(value), re.I)
    if not m:
        return "x"   # not a number -> validate_station reports "invalid frequency"
    return f"{int(round(float(m.group(1) + '.' + (m.group(2) or '0')) * 1000)):06d}"


_REASON_CODE = {"no country (ECC)": "no_ecc", "invalid ECC": "bad_ecc",
                "invalid PI": "bad_pi", "invalid frequency": "bad_freq"}


def _check(ecc, sid, typ, freq_db):
    why = validate_station(ecc, sid, typ == "DAB", freq_db)
    if why:
        raise EditError(_REASON_CODE.get(why, "invalid"))


def _find(cur, table, typ, ecc, sid, freq_db, exclude_rowid=None):
    """rowid + RECEIVER_TYPE of a row with this identity (FM: + frequency), or None."""
    sql = f"SELECT rowid, RECEIVER_TYPE FROM {table} WHERE upper(ECC)=? AND upper(STATION_ID)=?"
    args = [ecc, sid]
    if typ == "FM":
        sql += " AND (FREQUENCY=? OR FREQUENCY=?)"
        args += [freq_db, str(int(freq_db))]
    if exclude_rowid is not None:
        sql += " AND rowid<>?"
        args.append(exclude_rowid)
    return cur.execute(sql, args).fetchone()


def add_station(cfg, typ, ecc, sid, freq_mhz=None, logo=""):
    """Insert an app-owned row; returns its rowid. Raises EditError."""
    ecc, sid = (ecc or "").strip().upper(), (sid or "").strip().upper()
    freq_db = None if typ == "DAB" else parse_mhz(freq_mhz)
    _check(ecc, sid, typ, freq_db)
    if logo and not _LOGO_RE.fullmatch(logo):
        raise EditError("bad_logo")
    db, table = _db_table(cfg, typ)
    conn = sqlite3.connect(db)
    try:
        cur = conn.cursor()
        dup = _find(cur, table, typ, ecc, sid, freq_db)
        if dup:
            raise EditError("duplicate", rowid=dup[0])
        cur.execute(f"INSERT INTO {table} (ECC, STATION_ID, RECEIVER_TYPE, FREQUENCY, LOGO_FILENAME) "
                    f"VALUES (?,?,?,?,?)", (ecc, sid, OWN_TYPES[typ], freq_db or "0", logo or ""))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update_station(cfg, typ, rowid, sid=None, freq_mhz=None):
    """Change PI and/or frequency of an app-owned row (Ford rows are refused)."""
    db, table = _db_table(cfg, typ)
    conn = sqlite3.connect(db)
    try:
        cur = conn.cursor()
        row = cur.execute(f"SELECT ECC, STATION_ID, RECEIVER_TYPE, FREQUENCY FROM {table} WHERE rowid=?",
                          (rowid,)).fetchone()
        if not row:
            raise EditError("not_found")
        ecc, old_sid, rtype, old_freq = row
        if rtype not in OWN_TYPES.values():
            raise EditError("factory_row")
        ecc = (ecc or "").upper()
        sid = (sid or old_sid or "").strip().upper()
        freq_db = None if typ == "DAB" else (parse_mhz(freq_mhz) if freq_mhz not in (None, "") else _freq_key(old_freq))
        _check(ecc, sid, typ, freq_db)
        dup = _find(cur, table, typ, ecc, sid, freq_db, exclude_rowid=rowid)
        if dup:
            raise EditError("duplicate", rowid=dup[0])
        cur.execute(f"UPDATE {table} SET STATION_ID=?, FREQUENCY=? WHERE rowid=?",
                    (sid, freq_db or "0", rowid))
        conn.commit()
        return rowid
    finally:
        conn.close()


# Display names: the Ford DBs have no name column, so names typed in the editor
# live in a small JSON file {"E0/D312": "Radio Test"}; they are never written to the DBs.
def load_names(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return {str(k).upper(): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_names(path, updates):
    """Merge {"ECC/PI": name} into the names file ('' removes); atomic write."""
    names = load_names(path)
    for k, v in updates.items():
        k, v = k.upper(), (v or "").strip()
        if v:
            names[k] = v[:80]
        else:
            names.pop(k, None)
    import config
    config.atomic_write(path, json.dumps(names, ensure_ascii=False, indent=1, sort_keys=True))
    return names


def _country_rows(cfg, ecc, digit):
    """All rows of one RDS country: dicts with type/ecc/pi/freq_db/logo/rtype/rowid."""
    out = []
    for typ in ("FM", "DAB"):
        try:
            db, table = _db_table(cfg, typ)
        except EditError:
            continue
        conn = sqlite3.connect(db)
        try:
            for rowid, e, sid, rt, fr, logo in conn.execute(
                    f"SELECT rowid, ECC, STATION_ID, RECEIVER_TYPE, FREQUENCY, LOGO_FILENAME FROM {table} "
                    f"WHERE upper(ECC)=? AND upper(substr(STATION_ID,1,1))=? ORDER BY STATION_ID, FREQUENCY",
                    (ecc.upper(), digit.upper())):
                out.append({"type": typ, "ecc": (e or "").upper(), "pi": (sid or "").upper(),
                            "freq_db": None if typ == "DAB" else _freq_key(fr), "logo": logo or "",
                            "rtype": rt or "", "rowid": rowid})
        finally:
            conn.close()
    return out


def _mhz(freq_db):
    """'098500' -> '98.5', '100770' -> '100.77' (Ford data has 10 kHz steps); '' for none."""
    if not freq_db:
        return ""
    try:
        whole, frac = divmod(int(freq_db), 1000)
    except (TypeError, ValueError):     # odd value in a foreign DB: show it as it is
        return str(freq_db)
    frac = f"{frac:03d}".rstrip("0") or "0"
    return f"{whole}.{frac}"


def export_rows(cfg, country, names_path):
    """CSV text with every row of `country` ("E0-D")."""
    ecc, _, digit = country.partition("-")
    names = load_names(names_path)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(EXPORT_COLUMNS)
    for r in _country_rows(cfg, ecc, digit):
        w.writerow([r["type"], r["ecc"], r["pi"], _mhz(r["freq_db"]), r["logo"],
                    names.get(f"{r['ecc']}/{r['pi']}", ""), r["rtype"]])
    return buf.getvalue()


def import_token(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def import_rows(cfg, text, names_path, dry_run=True, token=None):
    """Plan (and with dry_run=False apply) a CSV import of station rows.

    Adds missing rows as app-owned rows, updates the logo of existing app-owned
    rows, sets display names; Ford rows are reported as skipped and never changed.
    Writing requires the token from the dry run (= the same file) and backs up both
    DBs first. Returns {"add": [...], "update": [...], "skipped": [...],
    "rejected": [{"line", "reason"}], "names": n, "token"}.
    """
    tok = import_token(text)
    plan = {"add": [], "update": [], "skipped": [], "rejected": [], "names": 0, "token": tok}
    rdr = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    cols = {c.strip().lower(): c for c in (rdr.fieldnames or [])}
    missing = [c for c in ("type", "ecc", "pi") if c not in cols]
    if missing:
        raise EditError("csv_columns", missing=",".join(missing))
    get = lambda row, c: (row.get(cols[c]) or "").strip() if c in cols else ""  # noqa: E731
    names_now, name_updates, seen = load_names(names_path), {}, set()
    conns = {}
    try:
        for typ in ("FM", "DAB"):
            db, table = _db_table(cfg, typ)
            conns[typ] = (sqlite3.connect(db), table)
        for line, row in enumerate(rdr, start=2):
            typ = get(row, "type").upper()
            ecc, sid, logo = get(row, "ecc").upper(), get(row, "pi").upper(), get(row, "logo")
            if logo.upper().endswith(".PNG"):
                logo = logo[:-4]
            if typ not in OWN_TYPES:
                plan["rejected"].append({"line": line, "reason": "bad_type"}); continue
            if get(row, "receiver_type").upper() in ("F", "D"):
                # an exported Ford row: informational only, imports never change those
                plan["skipped"].append({"line": line, "type": typ, "ecc": ecc, "pi": sid,
                                        "frequency": get(row, "frequency"), "logo": logo,
                                        "reason": "factory_row"}); continue
            freq_db = None if typ == "DAB" else parse_mhz(get(row, "frequency"))
            why = validate_station(ecc, sid, typ == "DAB", freq_db)
            if why:
                plan["rejected"].append({"line": line, "reason": _REASON_CODE.get(why, "invalid")}); continue
            if logo and not _LOGO_RE.fullmatch(logo):
                plan["rejected"].append({"line": line, "reason": "bad_logo"}); continue
            ident = (typ, ecc, sid, freq_db)
            if ident in seen:   # repeated line (e.g. a duplicate row in the DB): harmless
                plan["skipped"].append({"line": line, "type": typ, "ecc": ecc, "pi": sid,
                                        "frequency": _mhz(freq_db), "logo": logo, "reason": "duplicate"}); continue
            seen.add(ident)
            name = get(row, "name")
            if "name" in cols and name != names_now.get(f"{ecc}/{sid}", "") and f"{ecc}/{sid}" not in name_updates:
                name_updates[f"{ecc}/{sid}"] = name
            conn, table = conns[typ]
            hit = conn.execute(
                f"SELECT rowid, RECEIVER_TYPE, LOGO_FILENAME FROM {table} WHERE upper(ECC)=? AND upper(STATION_ID)=?"
                + (" AND (FREQUENCY=? OR FREQUENCY=?)" if typ == "FM" else ""),
                [ecc, sid] + ([freq_db, str(int(freq_db))] if typ == "FM" else [])).fetchone()
            item = {"line": line, "type": typ, "ecc": ecc, "pi": sid, "frequency": _mhz(freq_db), "logo": logo}
            if not hit:
                plan["add"].append(item)
            elif hit[1] not in OWN_TYPES.values():
                plan["skipped"].append(dict(item, reason="factory_row"))
            elif logo and logo != (hit[2] or ""):
                plan["update"].append(dict(item, rowid=hit[0]))
        plan["names"] = len(name_updates)
        if dry_run or not (plan["add"] or plan["update"] or name_updates):
            return plan
        if token != tok:
            raise EditError("stale_preview")
        stamp = __import__("time").strftime("%Y%m%d-%H%M%S")
        for typ, (conn, table) in conns.items():
            conn.commit()
            backup = sqlite3.connect(f"{_db_table(cfg, typ)[0]}.bak-{stamp}")
            conn.backup(backup)
            backup.close()
        for it in plan["add"]:
            conn, table = conns[it["type"]]
            conn.execute(f"INSERT INTO {table} (ECC, STATION_ID, RECEIVER_TYPE, FREQUENCY, LOGO_FILENAME) "
                         f"VALUES (?,?,?,?,?)", (it["ecc"], it["pi"], OWN_TYPES[it["type"]],
                                                 parse_mhz(it["frequency"]) or "0", it["logo"]))
        for it in plan["update"]:
            conn, table = conns[it["type"]]
            conn.execute(f"UPDATE {table} SET LOGO_FILENAME=? WHERE rowid=?", (it["logo"], it["rowid"]))
        for conn, _ in conns.values():
            conn.commit()
        if name_updates:
            save_names(names_path, name_updates)
        plan["backup"] = stamp
        return plan
    finally:
        for conn, _ in conns.values():
            conn.close()
