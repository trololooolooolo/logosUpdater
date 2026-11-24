"""Optional: read the text on logo files locally with EasyOCR into logo_ocr.db.

Offline alternative to ocr_api_scanner.py (needs `pip install easyocr`, which
downloads its models on the first run). Paths come from config.yaml.

    python3 ocr_scanner.py --languages en pl
"""
import argparse
import logging
import os
import sqlite3

import config

logger = logging.getLogger(__name__)


def init_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE IF NOT EXISTS logo_text (ecc TEXT, filename TEXT, recognized_text TEXT, "
                 "PRIMARY KEY(ecc, filename))")
    conn.commit()
    return conn


def scan_logo(reader, path):
    try:
        text = " ".join(reader.readtext(path, detail=0)).replace('\n', ' ').strip().upper()
        return " ".join(w for w in text.split() if len(w) > 1)     # drop one-letter noise
    except Exception as e:  # noqa: BLE001
        logger.error("Error scanning %s: %s", path, e)
        return ''


def run_scanner(cfg, languages=('en',), gpu=False):
    import easyocr                       # heavy: only when actually scanning
    logo_dir = cfg['paths']['logo_dir']
    conn = init_db(cfg['paths']['db_ocr'])
    try:
        scanned = set(conn.execute("SELECT ecc, filename FROM logo_text").fetchall())
        todo = []
        if os.path.isdir(logo_dir):
            for ecc in sorted(os.listdir(logo_dir)):
                d = os.path.join(logo_dir, ecc)
                if os.path.isdir(d):
                    todo += [(ecc, f, os.path.join(d, f)) for f in sorted(os.listdir(d))
                             if f.upper().endswith('.PNG') and (ecc, f) not in scanned]
        logger.info("Found %d new logos to scan.", len(todo))
        if not todo:
            return
        logger.info("Initializing EasyOCR (%s)...", ', '.join(languages))
        reader = easyocr.Reader(list(languages), gpu=gpu)
        for i, (ecc, fname, full) in enumerate(todo, 1):
            text = scan_logo(reader, full)
            if text:
                conn.execute("INSERT OR REPLACE INTO logo_text VALUES (?, ?, ?)", (ecc, fname, text))
                conn.commit()
            if i % 10 == 0:
                logger.info("Progress: %d/%d, last text: %r", i, len(todo), text)
        logger.info("OCR scanning completed.")
    finally:
        conn.close()


def main():
    p = argparse.ArgumentParser(description='Read logo text locally with EasyOCR into logo_ocr.db (optional helper).')
    p.add_argument('--config', default='config.yaml', help='config file (default: config.yaml)')
    p.add_argument('--languages', nargs='+', default=['en'], help='EasyOCR language codes (default: en)')
    p.add_argument('--gpu', action='store_true', help='use the GPU')
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    run_scanner(config.load_config(a.config), a.languages, a.gpu)


if __name__ == '__main__':
    main()
