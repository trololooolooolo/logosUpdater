"""Optional: read the text on logo files with the OCR.space API into logo_ocr.db.

The gallery uses the recognised text to suggest logos for stations. Paths come
from config.yaml (same as the app); the OCR.space key from config `ocr.api_key`
or env OCR_SPACE_API_KEY - never hard-coded.

    python3 ocr_api_scanner.py --ecc E2 --language pol --batch 50
"""
import argparse
import logging
import os
import sqlite3
import time

import requests

import config

OCR_URL = 'https://api.ocr.space/parse/image'
logger = logging.getLogger(__name__)


def init_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE IF NOT EXISTS logo_text (ecc TEXT, filename TEXT, recognized_text TEXT, "
                 "PRIMARY KEY(ecc, filename))")
    conn.commit()
    return conn


def scan_via_api(file_path, api_key, language):
    """Recognised text; '' = nothing / permanent error (don't retry); None = transient error."""
    try:
        with open(file_path, 'rb') as f:
            payload = {'apikey': api_key, 'language': language, 'isOverlayRequired': False,
                       'FileType': 'PNG', 'detectOrientation': False, 'scale': True, 'OCREngine': 2}
            r = requests.post(OCR_URL, data=payload, files={'file': f}, timeout=30)
        if r.status_code != 200:
            logger.error("HTTP %s for %s", r.status_code, file_path)
            return None
        result = r.json()
        if result.get('IsErroredOnProcessing'):
            logger.error("API error for %s: %s", file_path, result.get('ErrorMessage'))
            return ''
        parsed = result.get('ParsedResults') or []
        return parsed[0].get('ParsedText', '').replace('\n', ' ').strip().upper() if parsed else ''
    except Exception as e:  # noqa: BLE001 - network / JSON problems: try again next run
        logger.error("API call failed for %s: %s", file_path, e)
    return None


def run_scanner(cfg, api_key, eccs=None, language='eng', batch=50, reset_empty=False):
    paths = cfg['paths']
    conn = init_db(paths['db_ocr'])
    try:
        if reset_empty:
            n = conn.execute("DELETE FROM logo_text WHERE recognized_text = '' OR recognized_text IS NULL").rowcount
            conn.commit()
            logger.info("Removed %d empty results (they will be scanned again).", n)
        scanned = set(conn.execute("SELECT ecc, filename FROM logo_text").fetchall())
        todo = []
        for base in (paths['logo_dir'], paths['mount_dir']):
            if not os.path.isdir(base):
                continue
            for ecc in sorted(eccs or os.listdir(base)):
                d = os.path.join(base, ecc)
                if not os.path.isdir(d):
                    continue
                for f in sorted(os.listdir(d)):
                    if f.upper().endswith('.PNG') and (ecc, f) not in scanned:
                        todo.append((ecc, f, os.path.join(d, f)))
                        scanned.add((ecc, f))
        logger.info("Logos left to scan: %d (this run: up to %d)", len(todo), batch)
        done = 0
        for ecc, fname, full in todo[:batch]:
            text = scan_via_api(full, api_key, language)
            if text is None:
                continue                     # transient: not saved, retried next run
            conn.execute("INSERT OR REPLACE INTO logo_text VALUES (?, ?, ?)", (ecc, fname, text))
            conn.commit()
            done += 1
            logger.info("%s/%s: %s", ecc, fname, repr(text) if text else '(no text)')
            time.sleep(1)                    # be gentle with the free API tier
        logger.info("Scanned %d logos.", done)
    finally:
        conn.close()


def main():
    p = argparse.ArgumentParser(description='Read logo text with the OCR.space API into logo_ocr.db (optional helper).')
    p.add_argument('--config', default='config.yaml', help='config file (default: config.yaml)')
    p.add_argument('--ecc', action='append', help='only this ECC folder (repeatable; default: all)')
    p.add_argument('--language', default='eng', help="OCR.space language code, e.g. eng, pol, ger (default: eng)")
    p.add_argument('--batch', type=int, default=50, help='max logos per run, to save the API quota (default: 50)')
    p.add_argument('--reset-empty', action='store_true', help='scan again the logos where nothing was recognised')
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    cfg = config.load_config(a.config)
    key = os.environ.get('OCR_SPACE_API_KEY') or (cfg.get('ocr') or {}).get('api_key', '')
    if not key:
        raise SystemExit("No OCR.space API key: set ocr.api_key in config.yaml or env OCR_SPACE_API_KEY.")
    run_scanner(cfg, key, [e.upper() for e in a.ecc or []] or None, a.language, a.batch, a.reset_empty)


if __name__ == '__main__':
    main()
