#!/usr/bin/env python3
"""Buduje nowy obraz QNX6 station_logos: oryginał + nowe logo z logos_sync5/.

W całości w Pythonie na Ubuntu — bez sudo, bez QEMU, bez QNX SDP.
Wykorzystuje qnx6fs.QNX6Reader (ekstrakcja) i QNX6Writer (budowa).

Workflow:
  python3 build_logo_img.py                 # dograj brakujące logo, zbuduj obraz
  python3 build_logo_img.py --overwrite     # nadpisz też istniejące (po zmianie)
  python3 build_logo_img.py --dry-run       # tylko pokaż co by się zmieniło
"""
import os
import sys
import time
import shutil
import hashlib
import argparse

from qnx6fs import QNX6Reader, QNX6Writer

ORIG_IMG = 'station_logos.img'
SYNC_DIR = 'logos_sync5'
OUT_IMG = 'station_logos_new.img'
WORK_DIR = 'qnx_merged'      # scalone drzewo (oryginał + logo)


def md5(path):
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for c in iter(lambda: f.read(65536), b''):
            h.update(c)
    return h.hexdigest()


def merge(sync_dir, target_dir, overwrite=False, choices=None, styled_dir=None):
    """Copy logos from sync_dir (and picked styled files) into the extracted image tree.

    Missing files are always added. For a file the image already has, the
    per-file choice wins ({"ECC/NAME.PNG": "image"|"sync5"|"styled"}, from the UI's
    logo-version picker); files without a choice follow `overwrite`. "styled"
    takes styled_dir/ECC/NAME.PNG (logo_style.py); without that file it counts as no choice.
    """
    choices = dict(choices or {})
    for k, v in list(choices.items()):
        if v == 'styled' and not (styled_dir and os.path.isfile(os.path.join(styled_dir, *k.split('/')))):
            del choices[k]
    added = updated = skipped = 0
    if not os.path.isdir(sync_dir):
        print(f"[WARN] brak {sync_dir}/ — pomijam dogrywanie")
    else:
        for ecc in sorted(os.listdir(sync_dir)):
            s = os.path.join(sync_dir, ecc)
            if not os.path.isdir(s):
                continue
            d = os.path.join(target_dir, ecc)
            os.makedirs(d, exist_ok=True)
            for fn in sorted(os.listdir(s)):
                sp = os.path.join(s, fn)
                dp = os.path.join(d, fn)
                want = choices.get(f'{ecc}/{fn}'.upper())
                if not os.path.isfile(sp) or want == 'styled':
                    continue
                if not os.path.exists(dp):
                    shutil.copy2(sp, dp); added += 1
                elif (want or ('sync5' if overwrite else 'image')) == 'sync5' and md5(sp) != md5(dp):
                    shutil.copy2(sp, dp); updated += 1
                else:
                    skipped += 1
    for k, v in sorted(choices.items()):
        if v != 'styled':
            continue
        ecc, fn = k.split('/')
        d = os.path.join(target_dir, ecc)
        os.makedirs(d, exist_ok=True)
        # the image's own spelling of the name, if it has the file
        dp = next((os.path.join(d, f) for f in os.listdir(d) if f.upper() == fn), os.path.join(d, fn))
        existed = os.path.exists(dp)
        shutil.copy2(os.path.join(styled_dir, ecc, fn), dp)
        if existed: updated += 1
        else: added += 1
    return added, updated, skipped


def _defaults():
    """Ścieżki z config.yaml (jeśli jest), inaczej domyślne z config.DEFAULTS (stałe powyżej = awaryjnie)."""
    d = {'image': ORIG_IMG, 'logo_dir': SYNC_DIR, 'image_out': OUT_IMG, 'work_dir': WORK_DIR,
         'logo_choices': 'logo_choices.json', 'styled_dir': 'logos_styled'}
    try:
        import config
        d.update({k: v for k, v in config.DEFAULTS['paths'].items() if k in d})
    except SystemExit:
        pass
    if os.path.exists('config.yaml'):
        try:
            import config
            d.update({k: v for k, v in config.load_config('config.yaml')['paths'].items() if k in d})
        except Exception as e:
            print(f"[WARN] config.yaml nieczytelny ({e}) — domyślne ścieżki")
    return d


def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    d = _defaults()
    ap = argparse.ArgumentParser()
    ap.add_argument('--img', default=d['image'], help='obraz źródłowy (oryginał)')
    ap.add_argument('--sync', default=d['logo_dir'], help='katalog z nowymi logo')
    ap.add_argument('--out', default=d['image_out'], help='obraz wyjściowy')
    ap.add_argument('--work', default=d['work_dir'], help='katalog roboczy (scalone drzewo)')
    ap.add_argument('--overwrite', action='store_true', help='nadpisuj istniejące pliki (bez wyboru wersji w UI)')
    ap.add_argument('--choices', default=d['logo_choices'], help='wybór wersji per plik (JSON z UI)')
    ap.add_argument('--styled', default=d['styled_dir'], help='logo ze stylem (🖌 w UI), wybór "styled"')
    ap.add_argument('--dry-run', action='store_true', help='nie buduj obrazu')
    ap.add_argument('--keep-work', action='store_true', help='nie usuwaj katalogu roboczego')
    args = ap.parse_args()

    if not os.path.exists(args.img):
        print(f"[ERR] brak {args.img}"); sys.exit(1)

    # 1) ekstrakcja oryginału (pure Python, bez sudo)
    t0 = time.time()
    if os.path.exists(args.work):
        shutil.rmtree(args.work)
    r = QNX6Reader(args.img)
    print(f"[1] oryginał: checksum_ok={r.checksum_ok} inodes={r.num_inodes} "
          f"blocks={r.num_blocks}")
    n = r.extract_to(args.work)
    r.close()
    print(f"    wyekstrahowano {n} plików do {args.work}/ ({time.time()-t0:.1f}s)")

    # 2) dogranie nowych logo
    from usb_package import load_choices
    added, updated, skipped = merge(args.sync, args.work, args.overwrite, load_choices(args.choices), args.styled)
    print(f"[2] merge {args.sync}/: dodano={added} zaktualizowano={updated} "
          f"pominięto={skipped}")

    if args.dry_run:
        print("[*] --dry-run — nie buduję obrazu")
        if not args.keep_work:
            shutil.rmtree(args.work)
        return

    # 3) budowa nowego obrazu
    t1 = time.time()
    if os.path.exists(args.out):
        bak = args.out + '.bak-' + time.strftime('%Y%m%d-%H%M%S')
        shutil.move(args.out, bak)
        print(f"[3] backup starego {args.out} -> {bak}")
    w = QNX6Writer(args.work, template_img=args.img)
    info = w.write(args.out)
    from usb_package import mark_built
    mark_built(args.img, args.out)
    print(f"[3] zbudowano {args.out} ({time.time()-t1:.1f}s): {info}")

    # 4) auto-weryfikacja: odczyt z powrotem naszym readerem
    r2 = QNX6Reader(args.out)
    files = sum(1 for _, _, k in r2.walk() if k == 'file')
    r2.close()
    print(f"[4] weryfikacja odczytu: checksum_ok={r2.checksum_ok} plików={files}")
    print(f"\n[OK] Gotowe: {args.out}")
    print(f"     Test kernelem Linux:  sudo bash verify_mount.sh {args.out} {args.work}")
    if not args.keep_work:
        print(f"     (katalog {args.work}/ zachowany jako referencja do verify_mount.sh)")


if __name__ == '__main__':
    main()
