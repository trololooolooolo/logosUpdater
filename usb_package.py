"""usb_package.py — prepare a SyncMyMod USB package for Ford Sync 3 (FMods Tools).

Layout written (the same one the car packages in inputs/ use):

    <root>/SyncMyMod/autoinstall.sh
    <root>/SyncMyMod/files/other/station_logos.img
    <root>/SyncMyMod/files/other/fm_station_logo.db
    <root>/SyncMyMod/files/other/dab_station_logo.db
    <root>/SyncMyMod/files/other/MANIFEST.md5
    <root>/SyncMyMod/README_USB.txt      (inside SyncMyMod/: the zip has a single top-level folder)

`<root>` is either a mounted USB stick (written in place) or a staging dir that
is then zipped for download. Before anything is written the image is read back
as QNX6 (superblock checksum) and both DBs pass `PRAGMA integrity_check`, so a
broken file never reaches the car.

On the car, autoinstall.sh (needs FMods Tools >= 2.9) checks the files on the
USB first (size and, where the car has `cksum`, the POSIX CRC baked in at package
time), backs up the car's current image + DBs to SyncMyMod/backup/ on the
stick (once, verified) and the DBs to *.db.ori (once, so the Ford originals survive
repeated installs), then swaps in the new files one by one: copy to <file>.new,
verify, rename. If a step fails, the files already replaced are restored from the
backup the same way.

CLI:
    python3 usb_package.py                       # zip from the built image (or the loaded one)
    python3 usb_package.py --image loaded        # package the loaded image unchanged
    python3 usb_package.py --target /media/$USER/USB   # write straight onto a stick
"""

import hashlib
import os
import re
import shutil

import config
import sqlite3
import time
import zipfile

PKG_DIR = 'SyncMyMod'
OTHER_SUB = os.path.join('files', 'other')
BACKUP_SUB = 'backup'          # SyncMyMod/backup: the car's original files, saved by autoinstall.sh
STICK_MARGIN_MB = 10          # spare room asked for on a stick on top of the package (+ the car's backup)
FILES = ('station_logos.img', 'fm_station_logo.db', 'dab_station_logo.db')

# Mount points that count as a removable stick (Ubuntu udisks2 / older automount).
_USB_FSTYPES = {'vfat', 'exfat', 'msdos', 'fuseblk', 'fat', 'fat32'}  # FAT32 / exFAT (what the car reads); Linux / macOS / Windows names
# ('fuseblk' = exFAT through FUSE on older Linux; an NTFS stick mounted by ntfs-3g looks the same and slips through)

AUTOINSTALL = r'''#!/bin/sh
# LogosUpdater - FM/DAB station logos for Ford Sync 3 (QNX)
# Generated: __DATE__   Source: __SOURCE__
# Requires FMods Tools __MIN_TOOLS__+ (instutility, remount_rw.sh, remount_ro.sh).

FANCYNAME="FM/DAB Logos (LogosUpdater)"
VERSION="__VERSION__"

# LU_ROOT / LU_NO_WAIT exist only for the tests (tests/test_usb_backup.py, tests/test_car_install.py
# run this script on a fake car tree); on the car both are unset, so every path below is the real one.
R="${LU_ROOT:-}"
PATH=${R}/fs/rwdata/dev:$PATH
USB="${R}/fs/usb0"
USB_DIR="${USB}/SyncMyMod"
OTHER_DIR="${USB_DIR}/files/other"
BACKUP_DIR="${USB_DIR}/backup"
IMG_DIR="${R}/fs/images"
LOGO_MNT="${R}/fs/mp/resources/station_logo/Logo"
FM_DIR="${R}/fs/mp/resources/FM/station_logo"
DAB_DIR="${R}/fs/mp/resources/DAB/station_logo"
DISPLAY=${R}/fs/tmpfs/status
POPUP=${R}/tmp/popup.txt
MODTOOLS="FMODS_TOOLS"
MIN_MODTOOLS_VERSION="__MIN_TOOLS__"
FILES="station_logos.img fm_station_logo.db dab_station_logo.db"

output()   { echo "${1}" > "$DISPLAY"; sleep "${2:-1}"; }
progress() { echo "PROGRESS ${1}" > "$DISPLAY"; }
popup()    { echo "${1}" > "$POPUP"; utserviceutility popup "$POPUP"; }
fsize()    { ls -l "${1}" 2>/dev/null | awk '{print $5}'; }

waitForUsbRemoval() {
    [ -n "${LU_NO_WAIT:-}" ] && exit 0
    while [ -e "${USB}" ]; do sleep 1; done
    output "REBOOT" 3
    exit 0
}

carfile() {
    case "$1" in
        station_logos.img) echo "${IMG_DIR}/$1" ;;
        fm_station_logo.db) echo "${FM_DIR}/$1" ;;
        *) echo "${DAB_DIR}/$1" ;;
    esac
}

# Size and POSIX cksum CRC of each payload file, computed when the package was made.
expected() {
    case "$1" in
        station_logos.img) echo "__SIZE_IMG__ __CRC_IMG__" ;;
        fm_station_logo.db) echo "__SIZE_FM__ __CRC_FM__" ;;
        dab_station_logo.db) echo "__SIZE_DAB__ __CRC_DAB__" ;;
    esac
}

# cksum is used only if it exists AND computes the POSIX CRC (self-test on a known string);
# otherwise files are checked by size alone.
HAVE_CKSUM=0
if command -v cksum >/dev/null 2>&1 && \
        [ "$(echo LogosUpdater | cksum 2>/dev/null | awk '{print $1}')" = "__CRC_SELFTEST__" ]; then
    HAVE_CKSUM=1
fi
crc() { cksum < "${1}" 2>/dev/null | awk '{print $1}'; }

# matches_expected PATH NAME: PATH has the packaged size (and CRC) of payload file NAME
matches_expected() {
    set -- "$1" $(expected "$2")
    [ -n "$2" ] && [ "$(fsize "$1")" = "$2" ] || return 1
    [ "$HAVE_CKSUM" = 1 ] || return 0
    [ "$(crc "$1")" = "$3" ]
}

# same_file A B: same size (and CRC) - used when restoring from the backup
same_file() {
    sa=$(fsize "$1")
    [ -n "$sa" ] && [ "$sa" = "$(fsize "$2")" ] || return 1
    [ "$HAVE_CKSUM" = 1 ] || return 0
    [ "$(crc "$1")" = "$(crc "$2")" ]
}

# payload_ok PATH NAME: PATH is either the packaged payload NAME, or an exact copy of
# SyncMyMod/backup/NAME (the manual undo in README_USB.txt: copy the backup files into
# files/other/ and install again - that backup was verified against the car when it was made)
payload_ok() {
    matches_expected "$1" "$2" && return 0
    [ -s "${BACKUP_DIR}/$2" ] && same_file "$1" "${BACKUP_DIR}/$2"
}

# put_file SRC DEST [NAME]: copy SRC to DEST.new, verify it (against payload NAME, or against SRC),
# then rename over DEST. DEST is either the old or the complete new file, never a partial one.
put_file() {
    rm -f "${2}.new"
    if cp "$1" "${2}.new"; then
        if [ -n "${3:-}" ]; then
            payload_ok "${2}.new" "$3" && mv -f "${2}.new" "$2" && return 0
        else
            same_file "$1" "${2}.new" && mv -f "${2}.new" "$2" && return 0
        fi
    fi
    rm -f "${2}.new"
    return 1
}

# --- FMods Tools present? -------------------------------------------------------
if ! grep -q "${MODTOOLS}" "${R}/fs/rwdata/dev/mods_tools.txt" 2>/dev/null; then
    popup "FMods Tools not found. Installation aborted."
    exit 0
fi
TOOLS_VER=$(grep "$MODTOOLS" "${R}/fs/rwdata/dev/mods_tools.txt" | head -1 | awk -F'_' '{print $NF}')
# major.minor compared as integers (2.10 >= 2.9); the version is passed as data, never as awk code
if ! awk -v have="$TOOLS_VER" -v need="$MIN_MODTOOLS_VERSION" 'BEGIN {
        split(have, h, "."); split(need, n, ".")
        if (h[1] + 0 != n[1] + 0) exit !(h[1] + 0 > n[1] + 0)
        exit !(h[2] + 0 >= n[2] + 0) }'; then
    popup "FMods Tools ${MIN_MODTOOLS_VERSION} or higher is required. Installation aborted."
    exit 0
fi

instutility &
sleep 2 # instutility needs a moment before it accepts status lines

output "DEV ${FANCYNAME} ${VERSION}" 2

# --- check the USB payload BEFORE touching the car -----------------------------
progress 5
output "Checking files on USB..." 1
for f in $FILES; do
    if [ ! -s "${OTHER_DIR}/${f}" ]; then
        output "Missing ${f} on USB. Nothing was changed." 5
        waitForUsbRemoval
    fi
    if ! payload_ok "${OTHER_DIR}/${f}" "$f"; then
        output "File ${f} on USB is damaged or incomplete (copy it again). Nothing was changed." 8
        waitForUsbRemoval
    fi
done

# --- backup of the car's files to the stick: BEFORE anything is changed -----------
# Made once per stick and never overwritten. Each copy is checked against the car's
# file; if the copy fails (stick full, write error) the install stops here and the car
# keeps its files. Reading the car files needs no RW remount.
progress 10
if [ -e "${BACKUP_DIR}" ]; then
    for f in $FILES; do
        if [ ! -s "${BACKUP_DIR}/${f}" ]; then
            output "SyncMyMod/backup is incomplete (${f}). Copy the folder to a computer, delete it from the stick and try again. Nothing was changed." 8
            waitForUsbRemoval
        fi
    done
    output "Backup already on USB (SyncMyMod/backup) - kept." 1
else
    output "Backing up current logos to USB (SyncMyMod/backup)..." 1
    BACKUP_OK=1
    mkdir -p "${BACKUP_DIR}" || BACKUP_OK=0
    for f in $FILES; do
        [ "$BACKUP_OK" = 1 ] || break
        src=$(carfile "$f")
        if ! cp "$src" "${BACKUP_DIR}/" || [ ! -s "${BACKUP_DIR}/${f}" ] \
                || ! same_file "$src" "${BACKUP_DIR}/${f}"; then
            BACKUP_OK=0
        fi
    done
    if [ "$BACKUP_OK" != 1 ]; then
        rm -rf "${BACKUP_DIR}"      # an incomplete backup must not look like a good one later
        output "Backup to USB failed (stick full?). Nothing was changed. Free space on the stick and try again." 8
        waitForUsbRemoval
    fi
    {
        echo "LogosUpdater backup of the car's original files (made before the first install from this stick)."
        echo "Date (car clock): $(date)"
        echo "Logo image: $(head -1 "${LOGO_MNT}/Version.inf" 2>/dev/null)"
        for f in $FILES; do echo "${f}: $(fsize "${BACKUP_DIR}/${f}") bytes"; done
        echo "To undo: a) in LogosUpdater load these three files (step 1), then step 4 with"
        echo "'Pack the loaded files unchanged', or b) copy them into SyncMyMod/files/other/"
        echo "and install again. Keep this folder; use one stick per car."
    } > "${BACKUP_DIR}/BACKUP_INFO.txt"
    sync
fi

progress 15
output "Setting RW permissions to FS..." 1
remount_rw.sh
sleep 1
[ -e "${FM_DIR}/fm_station_logo.db.ori" ]   || cp "${FM_DIR}/fm_station_logo.db"   "${FM_DIR}/fm_station_logo.db.ori"
[ -e "${DAB_DIR}/dab_station_logo.db.ori" ] || cp "${DAB_DIR}/dab_station_logo.db" "${DAB_DIR}/dab_station_logo.db.ori"

# On a failed step: put back every file already replaced (from SyncMyMod/backup, verified the
# same way), make sure the image is there, switch back to read-only and tell the user.
REPLACED=""
install_failed() {
    RESTORE_OK=1
    for f in $REPLACED; do
        put_file "${BACKUP_DIR}/${f}" "$(carfile "$f")" || RESTORE_OK=0
    done
    if [ ! -s "${IMG_DIR}/station_logos.img" ]; then
        put_file "${BACKUP_DIR}/station_logos.img" "${IMG_DIR}/station_logos.img" || RESTORE_OK=0
    fi
    remount_ro.sh
    sync; sync
    progress 100
    if [ "$RESTORE_OK" != 1 ]; then
        output "Install failed (${1}) and restoring the original files also failed. Restore them from SyncMyMod/backup on the stick as described in SyncMyMod/README_USB.txt." 10
    elif [ -n "$REPLACED" ]; then
        output "Install failed (${1}). The original files were restored from SyncMyMod/backup." 8
    else
        output "Install failed (${1}). The car files were not changed." 8
    fi
    waitForUsbRemoval
}

# --- image ---------------------------------------------------------------------
progress 30
output "Unmounting FM/DAB logos partition..." 1
umount -f "${LOGO_MNT}"

progress 40
output "Copying new FM/DAB .img file..." 1
put_file "${OTHER_DIR}/station_logos.img" "${IMG_DIR}/station_logos.img" station_logos.img \
    || install_failed "copying station_logos.img"
REPLACED="station_logos.img"

# --- databases -----------------------------------------------------------------
progress 70
output "Copying new FM/DAB .db files..." 1
for f in fm_station_logo.db dab_station_logo.db; do
    put_file "${OTHER_DIR}/${f}" "$(carfile "$f")" "$f" || install_failed "copying ${f}"
    REPLACED="${REPLACED} ${f}"
done

progress 95
output "Setting RO permissions to FS..." 1
remount_ro.sh
sync; sync; sync

progress 100
output "Installation completed. Please remove the USB stick to reboot." 1
waitForUsbRemoval
'''

README = '''LogosUpdater - USB package for Ford Sync 3 (FMods Tools)
=========================================================
Generated: {date}
Set: {source}
Image: {image} ({image_note})

Installation
------------
1. FAT32 (or exFAT) USB stick with the SyncMyMod/ folder directly in its root.
2. FMods Tools {min_tools}+ must be installed on the car.
3. Switch the ignition on (engine running is best) and plug in the stick - the install starts by itself.
4. When "Installation completed" appears, pull the stick - the radio restarts.

Safety checks
-------------
Before touching the car the script checks the size (and, if the car has `cksum`, the checksum) of every
file on the stick against the values recorded when this package was made; a damaged or incomplete
copy stops the install with nothing changed - copy the package to the stick again.
Each car file is replaced in one step (copied next to it, verified, then renamed), so a failure never
leaves a half-written file. If any step fails, the files already replaced are restored from
SyncMyMod/backup/ and the screen says so.

Backup
------
On the first install from this stick the script - BEFORE changing anything - copies the car's current
image and databases to SyncMyMod/backup/ (on the stick) and checks every copy. If the copy fails (e.g.
stick full), the install stops and the car stays unchanged. An existing backup is never overwritten;
the databases are also kept as *.db.ori on the car (once).
  - Do NOT delete SyncMyMod/backup/. Copy it to a computer as well (details: BACKUP_INFO.txt).
  - One stick = one car: the backup on the stick holds that car's files.
Going back to the original:
  a) in LogosUpdater load the three files from SyncMyMod/backup/ (step 1), in step 4 tick
     "Pack the loaded files unchanged (undo)" and install the package as usual, or
  b) copy the files from backup/ to SyncMyMod/files/other/ and install again
     (MANIFEST.md5 then no longer matches - that is expected, the car checks them against backup/).

Checksums: SyncMyMod/files/other/MANIFEST.md5  (md5sum -c MANIFEST.md5)

Files
-----
{files}


=========================================================
LogosUpdater - paczka USB dla Ford Sync 3 (FMods Tools)
=========================================================
Wygenerowano: {date}
Zestaw: {source}
Obraz: {image} ({image_note})

Instalacja
----------
1. Pendrive FAT32 (lub exFAT), na nim katalog SyncMyMod/ dokładnie w głównym katalogu.
2. Na aucie muszą być zainstalowane FMods Tools {min_tools}+.
3. Włącz zapłon (najlepiej silnik), włóż pendrive - instalacja ruszy sama.
4. Po komunikacie "Installation completed" wyjmij pendrive - radio się zrestartuje.

Kontrole bezpieczeństwa
-----------------------
Zanim skrypt cokolwiek zmieni w aucie, sprawdza rozmiar (a jeśli auto ma `cksum` - także sumę
kontrolną) każdego pliku na pendrivie względem wartości zapisanych przy tworzeniu paczki; uszkodzona
lub niepełna kopia zatrzymuje instalację bez żadnych zmian - skopiuj paczkę na pendrive jeszcze raz.
Każdy plik w aucie jest podmieniany jednym krokiem (kopia obok, sprawdzenie, zmiana nazwy), więc błąd
nigdy nie zostawia pliku zapisanego w połowie. Jeśli któryś krok się nie uda, pliki już podmienione
są przywracane z SyncMyMod/backup/, a ekran o tym informuje.

Kopia zapasowa
--------------
Przy pierwszej instalacji z tego pendrive'a skrypt - ZANIM cokolwiek zmieni - kopiuje obecny
obraz i bazy z auta do SyncMyMod/backup/ (na pendrive) i sprawdza każdą kopię. Jeśli kopia się
nie uda (np. pełny pendrive), instalacja zatrzymuje się i auto zostaje bez zmian. Istniejącej
kopii skrypt nigdy nie nadpisuje; bazy trzyma też jako *.db.ori w aucie (tylko raz).
  - NIE usuwaj SyncMyMod/backup/ i zrób jego kopię na komputerze (opis: BACKUP_INFO.txt).
  - Jeden pendrive = jedno auto: kopia na pendrivie to pliki tamtego auta.
Powrót do oryginału:
  a) w LogosUpdaterze wczytaj trzy pliki z SyncMyMod/backup/ (krok 1), w kroku 4 zaznacz
     "Spakuj wczytane pliki bez zmian (cofnięcie)" i zainstaluj paczkę jak zwykle, albo
  b) skopiuj pliki z backup/ do SyncMyMod/files/other/ i zainstaluj ponownie
     (MANIFEST.md5 przestaje wtedy pasować - to normalne, auto sprawdza je względem backup/).

Sumy kontrolne: SyncMyMod/files/other/MANIFEST.md5  (md5sum -c MANIFEST.md5)

Pliki
-----
{files}
'''

MIN_TOOLS = '2.9'


_BITREV = bytes(int(f'{i:08b}'[::-1], 2) for i in range(256))   # byte with its bits mirrored


def _rev32(x):
    return int(f'{x:032b}'[::-1], 2)


def posix_cksum(data=None, path=None):
    """CRC of POSIX `cksum` (CRC-32/CKSUM: poly 0x04C11DB7, MSB first, the length appended
    least-significant byte first, result inverted) of `data` (bytes) or of the file at `path`.

    The MSB-first CRC is the bit mirror of zlib's LSB-first one over bit-mirrored bytes, so the
    work runs in C (zlib.crc32 + bytes.translate): ~0.5 s for a 200 MB image."""
    import zlib
    z = 0xFFFFFFFF          # zlib.crc32(d, v) works on ~v: this makes the raw register start at 0
    n = 0

    def feed(chunk):
        nonlocal z
        z = zlib.crc32(chunk.translate(_BITREV), z)

    if path is not None:
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(1 << 20), b''):
                feed(chunk)
                n += len(chunk)
    else:
        feed(data)
        n = len(data)
    size = n
    tail = bytearray()
    while n:
        tail.append(n & 0xFF)
        n >>= 8
    feed(bytes(tail))
    return _rev32(z ^ 0xFFFFFFFF) ^ 0xFFFFFFFF, size


def _md5(path):
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def check_inputs(img, db_fm, db_dab):
    """Raise ValueError unless the image reads as QNX6 and both DBs are sound.

    Returns {'files': n, 'version': ...} for the image."""
    for p in (img, db_fm, db_dab):
        if not os.path.isfile(p) or os.path.getsize(p) == 0:
            raise ValueError(f"missing or empty: {p}")
    from qnx6fs import validate_image
    # checksum, geometry (a truncated file fails), every directory and block tree; QNX6Error is a ValueError
    files = validate_image(img)['files']
    for db in (db_fm, db_dab):
        c = config.connect_ro(db)
        try:
            res = c.execute('PRAGMA integrity_check').fetchone()[0]
            cols = {row[1].upper() for t in c.execute(
                "select name from sqlite_master where type='table'").fetchall()
                for row in c.execute(f'PRAGMA table_info("{t[0]}")')}
        finally:
            c.close()
        if res != 'ok':
            raise ValueError(f"SQLite integrity_check failed for {db}: {res}")
        if not {'ECC', 'LOGO_FILENAME'} <= cols:
            raise ValueError(f"{db}: no ECC/LOGO_FILENAME columns — not a Ford station_logo DB")
    return {'files': files}


def usb_targets():
    """Writable removable sticks: [{'path', 'fstype', 'free_mb', 'has_pkg'}].

    Detection per OS lives in config.removable_mounts(); only FAT32/exFAT
    sticks are offered (what FMods Tools on the car can read)."""
    import config
    out = []
    for m in config.removable_mounts():
        mnt = m['path']
        if m['fstype'].lower() not in _USB_FSTYPES or not os.access(mnt, os.W_OK):
            continue
        try:
            free = shutil.disk_usage(mnt).free
        except OSError:
            continue
        out.append({'path': mnt, 'fstype': m['fstype'],
                    'free_mb': round(free / 1048576),
                    'has_pkg': os.path.isdir(os.path.join(mnt, PKG_DIR))})
    return out


def _flush_to_disk(path):
    """Make sure a file written to a USB stick is really on it (portable: fsync exists everywhere)."""
    with open(path, 'ab') as f:
        f.flush()
        os.fsync(f.fileno())


_SCRIPT_UNSAFE = re.compile(r'[^A-Za-z0-9._ +:-]')


def _script_safe(value, limit=60):
    """A value substituted into autoinstall.sh (runs as root on the car): only [A-Za-z0-9._ +:-],
    everything else (newlines, quotes, $, backticks, ...) becomes '_', at most `limit` characters."""
    return _SCRIPT_UNSAFE.sub('_', str(value))[:limit]


def _write_tree(root, img, db_fm, db_dab, source, image_note, sync=False):
    """Write SyncMyMod/ + README under root; returns the manifest {name: md5}.
    sync=True (writing to a stick): every file is flushed to the device."""
    other = os.path.join(root, PKG_DIR, OTHER_SUB)
    os.makedirs(other, exist_ok=True)
    manifest = {}
    for src, name in zip((img, db_fm, db_dab), FILES):
        dst = os.path.join(other, name)
        shutil.copyfile(src, dst)
        if sync:
            _flush_to_disk(dst)
        manifest[name] = _md5(src)
    date = time.strftime('%Y-%m-%d %H:%M')
    script = AUTOINSTALL
    # size + POSIX cksum CRC of each payload file as written: the car checks them before and after
    # every copy (integers only, so nothing from the files can reach the shell)
    for key, name in zip(('IMG', 'FM', 'DAB'), FILES):
        crc, size = posix_cksum(path=os.path.join(other, name))
        script = (script.replace(f'__SIZE_{key}__', str(int(size)))
                  .replace(f'__CRC_{key}__', str(int(crc))))
    script = script.replace('__CRC_SELFTEST__', str(posix_cksum(b'LogosUpdater\n')[0]))
    script = (script.replace('__DATE__', _script_safe(date))
              .replace('__SOURCE__', _script_safe(source or '-'))
              .replace('__VERSION__', _script_safe(time.strftime('%Y-%m-%d')))
              .replace('__MIN_TOOLS__', MIN_TOOLS))
    # ASCII + LF only: QNX sh chokes on CRLF; the set name may carry non-ASCII
    script = script.encode('ascii', 'replace').decode('ascii')
    with open(os.path.join(root, PKG_DIR, 'autoinstall.sh'), 'w', encoding='ascii', newline='\n') as f:
        f.write(script)
    with open(os.path.join(other, 'MANIFEST.md5'), 'w', encoding='utf-8', newline='\n') as f:
        f.writelines(f"{h}  {n}\n" for n, h in manifest.items())
    listing = '\n'.join(f"  SyncMyMod/files/other/{n}  {os.path.getsize(os.path.join(other, n)):>11,d} B  md5 {h}"
                        for n, h in manifest.items())
    with open(os.path.join(root, PKG_DIR, 'README_USB.txt'), 'w', encoding='utf-8', newline='\n') as f:
        f.write(README.format(date=date, source=source or '-', image=os.path.basename(img),
                              image_note=image_note, min_tools=MIN_TOOLS, files=listing))
    return manifest


def _verify_tree(root, manifest):
    other = os.path.join(root, PKG_DIR, OTHER_SUB)
    bad = [n for n, h in manifest.items() if _md5(os.path.join(other, n)) != h]
    if bad:
        raise IOError(f"md5 mismatch after copy: {', '.join(bad)}")


def _complete_backup(pkg):
    """pkg/backup holds every payload file, non-empty: the car keeps it and writes no new one
    (the same test autoinstall.sh makes)."""
    return all(os.path.isfile(p) and os.path.getsize(p) > 0
               for p in (os.path.join(pkg, BACKUP_SUB, n) for n in FILES))


def _dir_mb(path):
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, fn))
            except OSError:
                pass
    return total / 1048576


def _old_folders(target):
    """[{'name', 'mb'}] of the SyncMyMod.old-* folders earlier writes left on the stick (never deleted
    automatically: they are the user's, the user decides)."""
    try:
        names = sorted(n for n in os.listdir(target)
                       if n.startswith(PKG_DIR + '.old-') and os.path.isdir(os.path.join(target, n)))
    except OSError:
        return []
    return [{'name': n, 'mb': round(_dir_mb(os.path.join(target, n)))} for n in names]


def _free_name(path):
    """path, or path-2, path-3, ... if it already exists (two writes in the same second)."""
    cand, i = path, 2
    while os.path.exists(cand):
        cand, i = f"{path}-{i}", i + 1
    return cand


def _write_to_stick(target, img, db_fm, db_dab, source, image_note, stamp, need_mb):
    """Write the package onto a stick without ever leaving a half-written SyncMyMod/ behind.

    The new tree goes to <stick>/SyncMyMod.new-<stamp>/SyncMyMod first, every file fsync'ed and
    checked by md5. Only then the swap (renames on one file system = instant): the existing
    SyncMyMod/ -> SyncMyMod.old-<stamp>, its backup/ into the new tree, new tree -> SyncMyMod/.
    Any failure before the swap removes the staging folder and leaves SyncMyMod/ as it was."""
    allowed = {t['path']: t for t in usb_targets()}
    if target not in allowed:
        raise ValueError(f"not a mounted USB stick: {target}")
    pkg = os.path.join(target, PKG_DIR)
    has_backup = _complete_backup(pkg)
    # the package itself; without a complete SyncMyMod/backup the first install on the car also
    # copies the car's image + DBs (about the same size) onto the stick
    need = need_mb * (1 if has_backup else 2) + STICK_MARGIN_MB
    free = allowed[target]['free_mb']
    old_folders = _old_folders(target)
    if free < need:
        msg = (f"not enough free space on {target}: {free} MB free, need ~{round(need)} MB"
               + ("" if has_backup else " (the package + room for the car's backup in SyncMyMod/backup)"))
        if old_folders:
            msg += (f". Old packages from earlier writes can be deleted from the stick to make room: "
                    + ', '.join(f"{o['name']} ({o['mb']} MB)" for o in old_folders))
        raise ValueError(msg)
    staging = _free_name(os.path.join(target, f"{PKG_DIR}.new-{stamp}"))
    try:
        manifest = _write_tree(staging, img, db_fm, db_dab, source, image_note, sync=True)
        if hasattr(os, 'sync'):        # not on Windows; the files were fsync'ed one by one anyway
            os.sync()
        _verify_tree(staging, manifest)
    except BaseException as e:
        shutil.rmtree(staging, ignore_errors=True)
        if isinstance(e, Exception):
            raise IOError(f"writing the package to {target} failed ({e}); "
                          f"nothing on the stick was changed (the existing {PKG_DIR}/ is untouched)") from e
        raise
    res = {'target': target, 'manifest': manifest}
    new_pkg = os.path.join(staging, PKG_DIR)
    moved = None
    try:
        if os.path.exists(pkg):
            moved = _free_name(f"{pkg}.old-{stamp}")
            os.rename(pkg, moved)
            # the backup of the car's original files (made by the first install from this stick)
            # moves into the new package: it is the user's way back and must stay where the
            # install script and the guide look for it. Same file system -> instant, no copy.
            if os.path.isdir(os.path.join(moved, BACKUP_SUB)):
                os.replace(os.path.join(moved, BACKUP_SUB), os.path.join(new_pkg, BACKUP_SUB))
                res['kept_backup'] = True
        os.rename(new_pkg, pkg)
    except Exception as e:
        # put the old package back the way it was
        if moved and not os.path.exists(pkg):
            if os.path.isdir(os.path.join(new_pkg, BACKUP_SUB)) and not os.path.exists(os.path.join(moved, BACKUP_SUB)):
                os.replace(os.path.join(new_pkg, BACKUP_SUB), os.path.join(moved, BACKUP_SUB))
            os.rename(moved, pkg)
        shutil.rmtree(staging, ignore_errors=True)
        raise IOError(f"replacing {PKG_DIR}/ on {target} failed ({e}); the existing package was put back") from e
    shutil.rmtree(staging, ignore_errors=True)
    if moved:
        res['moved_old'] = os.path.basename(moved)
    stale = os.path.join(target, 'README_USB.txt')     # older packages kept the README at the root
    if os.path.isfile(stale):
        os.remove(stale)
    res['old_folders'] = _old_folders(target)
    return res


def build_package(img, db_fm, db_dab, out_dir, target=None, source='', image_note=''):
    """Check inputs, then write the package onto `target` (a mounted stick from
    usb_targets()) or zip it into `out_dir`. Returns a result dict."""
    t0 = time.time()
    info = check_inputs(img, db_fm, db_dab)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    need_mb = sum(os.path.getsize(p) for p in (img, db_fm, db_dab)) / 1048576
    res = {'status': 'ok', 'image': os.path.basename(img), 'img_files': info['files'],
           'size_mb': round(need_mb, 1)}
    if target:
        res.update(_write_to_stick(target, img, db_fm, db_dab, source, image_note, stamp, need_mb))
    else:
        os.makedirs(out_dir, exist_ok=True)
        staging = os.path.join(out_dir, f'.staging-{stamp}')
        try:
            manifest = _write_tree(staging, img, db_fm, db_dab, source, image_note)
            zpath = os.path.join(out_dir, f'usb_logos_{stamp}.zip')
            with zipfile.ZipFile(zpath, 'w', zipfile.ZIP_DEFLATED, compresslevel=1) as z:
                for dirpath, _dirs, files in os.walk(staging):
                    for fn in sorted(files):
                        p = os.path.join(dirpath, fn)
                        z.write(p, os.path.relpath(p, staging))
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        # keep only the newest few zips: each is ~the image size
        zips = sorted(f for f in os.listdir(out_dir) if f.startswith('usb_logos_') and f.endswith('.zip'))
        for f in zips[:-3]:
            os.remove(os.path.join(out_dir, f))
        res.update({'zip': os.path.basename(zpath), 'zip_path': zpath, 'manifest': manifest,
                    'zip_mb': round(os.path.getsize(zpath) / 1048576, 1)})
    res['seconds'] = round(time.time() - t0, 1)
    return res


_IDX_CACHE = {}    # (abs path, mtime, size) -> {ECC: {NAME.PNG upper: Inode}}
_DIFF_CACHE = {}   # (image key, file path, its mtime, its size) -> bool (content differs from the image copy)


def _img_key(img):
    st = os.stat(img)
    return (os.path.abspath(img), st.st_mtime, st.st_size)


def image_index(img):
    """{ECC: {NAME.PNG upper: Inode}} of the logo PNGs in a QNX6 image (cached)."""
    key = _img_key(img)
    if key not in _IDX_CACHE:
        from qnx6fs import QNX6Reader
        idx = {}
        r = QNX6Reader(img)
        try:
            for path, inode, kind in r.walk():
                parts = path.strip('/').split('/')
                if kind == 'file' and len(parts) == 2 and parts[1].upper().endswith('.PNG'):
                    idx.setdefault(parts[0].upper(), {})[parts[1].upper()] = inode
        finally:
            r.close()
        _IDX_CACHE.clear()  # one image at a time is enough; each holds ~10k inodes
        _IDX_CACHE[key] = idx
    return _IDX_CACHE[key]


def _db_logos(db_fm, db_dab):
    """{(ECC, NAME.PNG upper)} referenced by the FM/DAB DBs."""
    refs = set()
    for db in (db_fm, db_dab):
        if not os.path.exists(db):
            continue
        c = config.connect_ro(db)
        try:
            names = [r[0] for r in c.execute("select name from sqlite_master where type='table'")]
            t = next((n for n in names if n.lower() in ('fm_station_logo', 'dab_station_logo')), None)
            if t is None:
                continue
            for ecc, fn in c.execute(f'select distinct ECC, LOGO_FILENAME from "{t}"'):
                fn = (fn or '').strip().upper()
                if ecc and fn:
                    refs.add((ecc.upper(), fn if fn.endswith('.PNG') else fn + '.PNG'))
        finally:
            c.close()
    return refs


VERSIONS = ('image', 'sync5', 'styled')   # styled = paths.styled_dir (logo_style.py)


def load_choices(path):
    """Per-file version choices {"ECC/NAME.PNG": "image"|"sync5"|"styled"} (see build_logo_img.merge)."""
    import json
    try:
        with open(path, encoding='utf-8') as f:
            return {k.upper(): v for k, v in json.load(f).items() if v in VERSIONS}
    except (OSError, ValueError):
        return {}


def save_choices(path, changes):
    """Merge `changes` ({key: "image"|"sync5"|"styled"|None}; None clears) into the choices file."""
    import json
    cur = load_choices(path)
    for k, v in changes.items():
        k = k.upper()
        if v in VERSIONS:
            cur[k] = v
        else:
            cur.pop(k, None)
    config.atomic_write(path, json.dumps(dict(sorted(cur.items())), indent=1))
    return cur


def logo_report(img, db_fm, db_dab, logo_dir, choices=None, styled_dir=None):
    """What the car will (not) show from `img` for the logos the DBs point to.

    missing: not in the image, but logo_dir (or a picked styled file) has it — the table previews it, a build adds it
    differs: in the image, logo_dir holds another version and no version was chosen
    pending: a version was chosen (`choices`) but `img` holds another one (needs a build)
    absent:  neither in the image nor in logo_dir
    in_sync5: files (of those in both) whose `img` copy is the logo_dir version
    in_styled: files whose `img` copy is the styled_dir version
    Entries are "ECC/NAME.PNG".
    """
    choices = choices or {}
    idx = image_index(img)
    ikey = _img_key(img)
    sync5 = {}
    try:
        for ecc in os.listdir(logo_dir):
            d = os.path.join(logo_dir, ecc)
            if os.path.isdir(d):
                sync5[ecc.upper()] = {f.upper(): os.path.join(d, f) for f in os.listdir(d)}
    except OSError:
        pass
    missing, differs, pending, absent, in_sync5, in_styled = [], [], [], [], [], []
    reader = None

    def same(path, inode):
        nonlocal reader
        st = os.stat(path)
        dkey = (ikey, path, st.st_mtime, st.st_size)
        if dkey not in _DIFF_CACHE:
            if st.st_size != inode.size:
                _DIFF_CACHE[dkey] = True
            else:
                if reader is None:
                    from qnx6fs import QNX6Reader
                    reader = QNX6Reader(img)
                with open(path, 'rb') as f:
                    _DIFF_CACHE[dkey] = f.read() != reader.read_inode_data(inode)
        return not _DIFF_CACHE[dkey]

    try:
        for ecc, fn in sorted(_db_logos(db_fm, db_dab)):
            sp = sync5.get(ecc, {}).get(fn)
            inode = idx.get(ecc, {}).get(fn)
            name = f'{ecc}/{fn}'
            want = choices.get(name)
            styled = styled_dir and os.path.join(styled_dir, ecc, fn)
            has_styled = bool(styled) and os.path.isfile(styled)
            if want == 'styled' and not has_styled:
                want = None                       # styled file gone: as if no pick
            if inode is None:
                (missing if sp or want == 'styled' else absent).append(name)
                continue
            is_sync5 = bool(sp) and same(sp, inode)
            is_styled = has_styled and same(styled, inode)
            if is_sync5:
                in_sync5.append(name)
            if is_styled:
                in_styled.append(name)
            if want == 'styled' or is_styled:
                # picked styled but not built yet, or the image still holds a styled copy nobody picks
                if want != 'styled' or not is_styled:
                    pending.append(name)
                continue
            if not sp:
                continue
            if want is None:
                if not is_sync5:
                    differs.append(name)
            elif (want == 'sync5') != is_sync5:
                pending.append(name)
    finally:
        if reader:
            reader.close()
    return {'missing': missing, 'differs': differs, 'pending': pending, 'absent': absent,
            'in_sync5': in_sync5, 'in_styled': in_styled}


def _src_stamp(img):
    st = os.stat(img)
    return f"{os.path.abspath(img)}|{st.st_mtime}|{st.st_size}"


def mark_built(src_img, out_img):
    """Record which loaded image `out_img` was built from (sidecar `<out>.src`)."""
    with open(out_img + '.src', 'w', encoding='utf-8') as f:
        f.write(_src_stamp(src_img))


def built_matches(cfg):
    """True if paths.image_out exists and was built from the current paths.image.

    Loading another input set keeps that set's old mtime, so "newer than the
    loaded image" is not enough: a build of the previous set would win.
    """
    p = cfg['paths']
    try:
        with open(p['image_out'] + '.src', encoding='utf-8') as f:
            return os.path.exists(p['image_out']) and f.read().strip() == _src_stamp(p['image'])
    except OSError:
        return False


def pick_image(cfg, which='auto'):
    """('built'|'loaded', path) — 'auto' = the built image if it was built from the loaded one."""
    p = cfg['paths']
    if which == 'built' or (which == 'auto' and built_matches(cfg)):
        return 'built', p['image_out']
    return 'loaded', p['image']


def main():
    import argparse
    import config
    ap = argparse.ArgumentParser(description='Prepare a SyncMyMod USB package (image + FM/DAB DBs).')
    ap.add_argument('--image', choices=('auto', 'built', 'loaded'), default='auto',
                    help='built = paths.image_out, loaded = paths.image (default: newer of the two)')
    ap.add_argument('--target', help='mounted USB stick to write to (default: zip into paths.usb_dir)')
    ap.add_argument('--list', action='store_true', help='list detected USB sticks and exit')
    args = ap.parse_args()
    if args.list:
        for t in usb_targets():
            print(f"{t['path']}  {t['fstype']}  {t['free_mb']} MB free" + ('  [SyncMyMod/ exists]' if t['has_pkg'] else ''))
        return
    cfg = config.load_config()
    kind, img = pick_image(cfg, args.image)
    active = config.active_input_dir(cfg)
    res = build_package(img, cfg['paths']['db_fm'], cfg['paths']['db_dab'], cfg['paths']['usb_dir'],
                        target=os.path.abspath(args.target).rstrip('/') if args.target else None,
                        source=os.path.basename(active) if active else '', image_note=kind)
    print(f"OK ({res['seconds']}s): {res.get('zip_path') or res.get('target')}  image={res['image']} ({kind}), "
          f"{res['img_files']} files")


if __name__ == '__main__':
    main()
