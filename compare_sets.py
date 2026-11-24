#!/usr/bin/env python3
"""Porównanie zestawów wsadowych (station_logos.img + fm/dab_station_logo.db).

    python3 compare_sets.py inputs/01_*                 # podsumowanie jednego zestawu
    python3 compare_sets.py inputs/01_* inputs/03_*     # różnice A -> B
    python3 compare_sets.py A B --list                  # + pełne listy różniących się plików/wierszy

Argumentem może być katalog zestawu (lub paczka SyncMyMod / USB) albo pojedynczy
plik .img / .db. Niczego nie modyfikuje (bazy otwierane read-only).
"""
import argparse
import collections
import hashlib
import os
import sqlite3

import config
import sys

from qnx6fs import QNX6Reader

NAMES = ('station_logos.img', 'fm_station_logo.db', 'dab_station_logo.db')


def find_set(path):
    """{nazwa: ścieżka} dla plików zestawu pod `path` (bez rozróżniania wielkości liter)."""
    if os.path.isfile(path):
        base = os.path.basename(path).lower()
        if base.endswith('.img') or '.img.' in base:
            return {'station_logos.img': path}
        return {('dab' if 'dab' in base else 'fm') + '_station_logo.db': path}
    found = {}
    for root, dirs, files in os.walk(path):
        dirs.sort()
        for f in files:
            if f.lower() in NAMES and f.lower() not in found:
                found[f.lower()] = os.path.join(root, f)
    return found


def read_img(path):
    """{ścieżka_w_obrazie: md5}, Version.inf"""
    r = QNX6Reader(path)
    files, version = {}, ''
    try:
        for p, ino, kind in r.walk():
            if kind != 'file':
                continue
            data = r.read_inode_data(ino)
            files[p.lstrip('/')] = hashlib.md5(data).hexdigest()
            if p == '/Version.inf':
                version = data.decode('latin-1').strip().replace('\n', ' | ')
    finally:
        r.close()
    return files, version


def read_db(path):
    c = config.connect_ro(path)
    try:
        table = c.execute("select name from sqlite_master where type='table'").fetchone()[0]
        rows = c.execute(f'select ECC, STATION_ID, RECEIVER_TYPE, FREQUENCY, LOGO_FILENAME '
                         f'from "{table}"').fetchall()
    finally:
        c.close()
    return rows


def summarize_img(files, version):
    per_ecc = collections.Counter(p.split('/')[0] for p in files if '/' in p)
    return f'{len(files)} plików  {dict(sorted(per_ecc.items()))}\n    {version}'


def summarize_db(rows):
    rt = collections.Counter(r[2] for r in rows)
    logos = len({r[4] for r in rows})
    return f'{len(rows)} wierszy, {logos} różnych LOGO_FILENAME, RECEIVER_TYPE={dict(rt)}'


def show_list(label, items, full, limit=15):
    items = sorted(items, key=str)
    print(f'    {label}: {len(items)}')
    for it in (items if full else items[:limit]):
        print(f'      {it}')
    if not full and len(items) > limit:
        print(f'      … (+{len(items) - limit}, użyj --list)')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('a')
    ap.add_argument('b', nargs='?')
    ap.add_argument('--list', action='store_true', help='pełne listy różnic')
    args = ap.parse_args()

    sa = find_set(args.a)
    sb = find_set(args.b) if args.b else None
    names = [n for n in NAMES if n in sa or (sb and n in sb)]
    if not names:
        sys.exit(f'Brak plików zestawu w {args.a}')

    for name in names:
        print(f'== {name}')
        pa, pb = sa.get(name), sb.get(name) if sb else None
        is_img = name.endswith('.img')
        reader = read_img if is_img else read_db
        try:
            da = reader(pa) if pa else None
            db = reader(pb) if pb else None
        except Exception as e:  # np. obraz nie-QNX6
            print(f'  błąd odczytu: {e!r}')
            continue
        for tag, p, d in (('A', pa, da), ('B', pb, db)):
            if sb is None and tag == 'B':
                continue
            if d is None:
                print(f'  {tag}: (brak)')
            else:
                print(f'  {tag}: {p}\n    ' + (summarize_img(*d) if is_img else summarize_db(d)))
        if da is None or db is None:
            continue
        if is_img:
            fa, fb = da[0], db[0]
            show_list('dodane w B', fb.keys() - fa.keys(), args.list)
            show_list('usunięte w B', fa.keys() - fb.keys(), args.list)
            show_list('zmienione (inna treść)', [k for k in fa.keys() & fb.keys() if fa[k] != fb[k]], args.list)
        else:
            ca, cb = collections.Counter(da), collections.Counter(db)
            show_list('wiersze dodane w B', (cb - ca).elements(), args.list)
            show_list('wiersze usunięte w B', (ca - cb).elements(), args.list)


if __name__ == '__main__':
    main()
