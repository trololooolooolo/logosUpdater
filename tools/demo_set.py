#!/usr/bin/env python3
"""Generate a small demo input set: a QNX6 station_logos.img with neutral made-up logos
plus FM/DAB databases with a few rows in two countries (Poland E2-3, Germany E0-D).

For trying the app without your own car files, for screenshots in docs/ (no real
broadcaster logos) and for tests. Nothing here comes from a real car or station.

  python tools/demo_set.py                    # -> inputs/00_demo/
  python tools/demo_set.py --out /tmp/demo    # elsewhere
  python view_stations.py --load inputs/00_demo
(`python` inside the activated venv; outside it `python3` on Linux/macOS, `py` on Windows.)
"""
import argparse
import os
import sqlite3
import struct
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PIL import Image, ImageDraw, ImageFont  # noqa: E402
from qnx6fs import QNX6Writer, BOOTBLOCK_SIZE  # noqa: E402

VERSION = 'DEMO-1.0'
# logo file -> (label, plate colour, text colour, style)
LOGOS = {
    'E2': {
        'DE000A01': ('STATION A', (200, 30, 40), (255, 255, 255), 'plate'),
        'DE000B02': ('STATION B', (255, 255, 255), (20, 60, 160), 'card'),   # white "card" = background to remove
        'DE000C03': ('RADIO C', (0, 0, 0, 0), (240, 150, 0), 'text'),      # already transparent
        'DE000D04': ('DAB ONE', (40, 120, 60), (255, 255, 255), 'round'),
    },
    'E0': {
        'DE000E05': ('SENDER E', (30, 30, 30), (255, 220, 0), 'plate'),
        'DE000F06': ('WELLE F', (0, 90, 170), (255, 255, 255), 'round'),
    },
}
# (ECC, PI, RECEIVER_TYPE, FREQUENCY, LOGO_FILENAME); 'F'/'D' = rows as Ford ships them
FM_ROWS = [
    ('E2', '3D01', 'F', '098500', 'DE000A01'),
    ('E2', '3D01', 'F', '101300', 'DE000A01'),   # same station, second transmitter
    ('E2', '3D02', 'F', '088700', 'DE000B02'),
    ('E2', '3D03', 'F', '104200', 'DE000C03'),
    ('E2', '3D04', 'F', '095900', 'DE000G07'),   # logo file missing from the image
    ('E0', 'DD01', 'F', '095100', 'DE000E05'),
    ('E0', 'DD02', 'F', '090300', 'DE000F06'),
]
DAB_ROWS = [
    ('E2', '3DA1', 'D', '000000', 'DE000D04'),
    ('E2', '3D01', 'D', '000000', 'DE000A01'),
    ('E0', 'DDA1', 'D', '000000', 'DE000F06'),
]
SCHEMA = 'CREATE TABLE "{t}" ("ECC", "STATION_ID", "RECEIVER_TYPE", "FREQUENCY", "LOGO_FILENAME")'


def _font(size):
    for name in ('DejaVuSans-Bold.ttf', 'Arial Bold.ttf', 'arialbd.ttf'):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default()


def make_logo(label, bg, fg, style):
    """160x120 RGBA logo with a made-up name; the styles exercise the 🖌 filters."""
    im = Image.new('RGBA', (160, 120), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    if style == 'card':
        d.rectangle((0, 0, 159, 119), fill=bg)
    elif style == 'plate':
        d.rectangle((14, 24, 145, 95), fill=bg)
    elif style == 'round':
        d.ellipse((30, 10, 129, 109), fill=bg)
    font = _font(22 if len(label) < 9 else 18)
    w = d.textlength(label, font=font)
    d.text(((160 - w) / 2, 46), label, fill=fg, font=font)
    return im


def build(out):
    os.makedirs(out, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tree = os.path.join(tmp, 'tree')
        for ecc, logos in LOGOS.items():
            os.makedirs(os.path.join(tree, ecc))
            for name, spec in logos.items():
                make_logo(*spec).save(os.path.join(tree, ecc, name + '.PNG'))
        with open(os.path.join(tree, 'Version.inf'), 'w', newline='\r\n') as f:
            f.write(f'Version Number = {VERSION}\n')
        # minimal template: block size 1024, one allocation group (Ford images use 4096; both read fine)
        tmpl = os.path.join(tmp, 'tmpl.img')
        sb = bytearray(512)
        struct.pack_into('<I', sb, 0x30, 1024)
        struct.pack_into('<I', sb, 0x44, 1)
        with open(tmpl, 'wb') as f:
            f.write(bytes(BOOTBLOCK_SIZE) + bytes(sb))
        QNX6Writer(tree, template_img=tmpl, image_size=2 << 20).write(os.path.join(out, 'station_logos.img'))
    for name, table, rows in (('fm_station_logo.db', 'fm_station_logo', FM_ROWS),
                              ('dab_station_logo.db', 'dab_station_logo', DAB_ROWS)):
        p = os.path.join(out, name)
        if os.path.exists(p):
            os.remove(p)
        c = sqlite3.connect(p)
        c.execute(SCHEMA.format(t=table))
        c.executemany(f'INSERT INTO "{table}" VALUES (?,?,?,?,?)', rows)
        c.commit()
        c.close()
    with open(os.path.join(out, 'INFO.txt'), 'w', encoding='utf-8') as f:
        f.write('Demo set made by tools/demo_set.py: made-up stations and logos in Poland (E2-3) and '
                'Germany (E0-D). Not from a real car; do not put it on a car.\n')
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--out', default=os.path.join(ROOT, 'inputs', '00_demo'), help='output directory')
    args = ap.parse_args()
    out = build(args.out)
    print(f'Demo set written to {out}\n  load it:  {load_hint(out)}')


def _q(path):
    return f'"{path}"' if ' ' in path else path


def load_hint(out):
    """The command that loads `out`, in this OS's path style: short relative paths when run from the
    app folder, absolute ones otherwise (relpath also fails across Windows drives)."""
    app = os.path.join(ROOT, 'view_stations.py')
    try:
        if os.path.samefile(os.getcwd(), ROOT):
            app, out = 'view_stations.py', os.path.relpath(out)
    except (OSError, ValueError):
        pass
    return f'python {_q(app)} --load {_q(os.path.normpath(out))}'


if __name__ == '__main__':
    main()
