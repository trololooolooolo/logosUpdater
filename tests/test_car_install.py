"""autoinstall.sh on a fake car: payload checks (size + POSIX cksum baked in at package time),
atomic replacement (<file>.new, verify, rename), restore from SyncMyMod/backup on failure,
the FMods Tools version check, and the Python cksum against GNU cksum.
Harness: tests/test_usb_backup.FakeCar (LU_ROOT / LU_NO_WAIT, stubs in fake /fs/rwdata/dev).
Run: python3 -m unittest tests.test_car_install
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import usb_package  # noqa: E402
from test_usb_backup import FakeCar, FILES, CAR, read, write  # noqa: E402

CP = shutil.which('cp') or '/bin/cp'
SH = shutil.which('sh') or '/bin/sh'
# what the script needs from the system (sleep, umount, ... are stubbed); cksum deliberately absent
TOOLS = ('ls', 'awk', 'grep', 'head', 'cp', 'mv', 'rm', 'mkdir', 'date', 'cat', 'sync', 'basename')
SCRIPT = 'fs/usb0/SyncMyMod/autoinstall.sh'
IMG = 'station_logos.img'


@unittest.skipUnless(shutil.which('sh') and os.name == 'posix', 'needs a POSIX sh')
class CarInstall(FakeCar, unittest.TestCase):
    def run_car(self, path=None):
        env = dict(os.environ, LU_ROOT=self.r, LU_NO_WAIT='1')
        if path is not None:
            env['PATH'] = path
        res = subprocess.run([SH, self.p(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
        with open(self.p('fs/tmpfs/status'), encoding='utf-8') as f:
            status = f.read().strip()
        log = read(self.p('log')).decode() if os.path.exists(self.p('log')) else ''
        return res, status, log

    def no_cksum_path(self):
        b = self.p('bin')
        os.makedirs(b, exist_ok=True)
        for t in TOOLS:
            os.symlink(shutil.which(t), os.path.join(b, t))
        return b

    def payload(self, f):
        return self.p('fs/usb0/SyncMyMod/files/other', f)

    def leftovers(self):
        return [os.path.join(d, f) for d, _, fs in os.walk(self.p('fs')) for f in fs if f.endswith('.new')]

    def cp_stub(self, case):
        """cp stub: `case` = shell case arms on "$1"/"$2", everything else goes to the real cp."""
        self.stub('cp', f'case "$1|$2" in {case} *) exec {CP} "$@" ;; esac')

    # --- success --------------------------------------------------------------------------
    def test_success(self):
        res, status, log = self.run_car()
        self.assertIn('Installation completed', status, res.stderr)
        self.assertEqual(self.car(), self.new)
        self.assertEqual(self.backup(), self.old)
        self.assertEqual(self.leftovers(), [])
        self.assertTrue(log.rstrip().endswith('RO'), log)

    def test_manual_undo_from_backup(self):
        """README_USB.txt way (b): copy backup/ into files/other/ and install again -> car back to the originals."""
        res, status, _ = self.run_car()
        self.assertIn('Installation completed', status, res.stderr)
        for f in FILES:
            shutil.copyfile(self.p('fs/usb0/SyncMyMod/backup', f), self.payload(f))
        res, status, _ = self.run_car()
        self.assertIn('Installation completed', status, res.stderr)
        self.assertEqual(self.car(), self.old)
        self.assertEqual(self.backup(), self.old, 'backup untouched')

    def test_neither_payload_nor_backup_refused(self):
        self.run_car()                                     # creates the backup
        write(self.payload('fm_station_logo.db'), b'something else entirely')
        res, status, _ = self.run_car()
        self.assertIn('damaged or incomplete', status, res.stderr)
        self.assertEqual(self.car(), self.new, 'nothing changed by the refused run')

    def test_script_is_posix_ascii_lf(self):
        raw = read(self.p(SCRIPT))
        raw.decode('ascii')
        self.assertNotIn(b'\r', raw)
        self.assertNotIn(b'__', raw.replace(b'__MIN', b''), 'every placeholder filled')
        shells = [['sh', '-n']] + ([['busybox', 'sh', '-n']] if shutil.which('busybox') else []) \
            + ([['dash', '-n']] if shutil.which('dash') else [])
        for sh in shells:
            res = subprocess.run(sh + [self.p(SCRIPT)], capture_output=True, text=True, timeout=30)
            self.assertEqual(res.returncode, 0, (sh, res.stderr))
        crc, size = usb_package.posix_cksum(path=self.payload(IMG))
        self.assertIn(f'station_logos.img) echo "{size} {crc}"', raw.decode())

    # --- payload on the stick ---------------------------------------------------------------
    def test_truncated_payload_changes_nothing(self):
        write(self.payload(IMG), read(self.payload(IMG))[:100])      # stick pulled early on the PC
        res, status, log = self.run_car()
        self.assertIn('File station_logos.img on USB is damaged or incomplete', status, res.stderr)
        self.assertIn('Nothing was changed', status)
        self.assertEqual(self.car(), self.old)
        self.assertFalse(os.path.exists(self.p('fs/usb0/SyncMyMod/backup')))
        self.assertNotIn('RW', log)

    def flip_byte(self, f):
        data = bytearray(read(self.payload(f)))
        data[10] ^= 0x01                                   # same size, other content
        write(self.payload(f), bytes(data))

    def test_corrupt_payload_caught_by_cksum(self):
        self.assertIsNotNone(shutil.which('cksum'))
        self.flip_byte('fm_station_logo.db')
        res, status, log = self.run_car()
        self.assertIn('File fm_station_logo.db on USB is damaged', status, res.stderr)
        self.assertEqual(self.car(), self.old)
        self.assertNotIn('RW', log)

    def test_without_cksum_sizes_only(self):
        path = self.no_cksum_path()
        res, status, _ = self.run_car(path)
        self.assertIn('Installation completed', status, res.stderr)
        self.assertEqual(self.car(), self.new)
        # sizes only: a same-size corruption is not noticed, a short file still is
        for f in FILES:
            write(self.p(CAR[f], f), self.old[f])
        write(self.payload(IMG), read(self.payload(IMG))[:-1])
        res, status, _ = self.run_car(path)
        self.assertIn('damaged or incomplete', status, res.stderr)
        self.assertEqual(self.car(), self.old)
        write(self.payload(IMG), self.new[IMG])
        self.flip_byte('fm_station_logo.db')
        res, status, _ = self.run_car(path)
        self.assertIn('Installation completed', status, res.stderr)

    def test_broken_cksum_falls_back_to_sizes(self):
        self.stub('cksum', 'echo 123 4')                     # not the POSIX CRC: self-test fails
        res, status, _ = self.run_car()
        self.assertIn('Installation completed', status, res.stderr)
        self.assertEqual(self.car(), self.new)

    # --- failures while replacing car files --------------------------------------------------
    def test_image_copy_partial(self):
        # cp writes only part of the image to station_logos.img.new and still says "ok"
        self.cp_stub(f'*"|"*/fs/images/{IMG}.new) head -c 100 "$1" > "$2"; exit 0 ;;')
        res, status, log = self.run_car()
        self.assertIn('Install failed (copying station_logos.img)', status, res.stderr)
        self.assertIn('car files were not changed', status)
        self.assertEqual(self.car(), self.old)
        self.assertEqual(self.leftovers(), [])
        self.assertTrue(log.rstrip().endswith('RO'), log)

    def test_image_copy_fails(self):
        self.cp_stub(f'*"|"*/fs/images/{IMG}.new) head -c 100 "$1" > "$2"; exit 1 ;;')
        res, status, _ = self.run_car()
        self.assertIn('Install failed (copying station_logos.img)', status, res.stderr)
        self.assertEqual(self.car(), self.old)
        self.assertEqual(self.leftovers(), [])

    def test_db_failure_restores_image(self):
        self.cp_stub('*/files/other/dab_station_logo.db"|"*) exit 1 ;;')
        res, status, log = self.run_car()
        self.assertIn('Install failed (copying dab_station_logo.db)', status, res.stderr)
        self.assertIn('original files were restored from SyncMyMod/backup', status)
        self.assertEqual(self.car(), self.old, 'image and fm db restored from the backup')
        self.assertEqual(self.leftovers(), [])
        self.assertTrue(log.rstrip().endswith('RO'), log)

    def test_restore_failure_says_so(self):
        self.cp_stub('*/files/other/fm_station_logo.db"|"*) exit 1 ;; '
                     '*/SyncMyMod/backup/*"|"*.new) exit 1 ;;')
        res, status, _ = self.run_car()
        self.assertIn('restoring the original files also failed', status, res.stderr)
        self.assertIn('README_USB.txt', status)
        self.assertEqual(read(self.p(CAR[IMG], IMG)), self.new[IMG], 'never left without an image')

    # --- FMods Tools version ----------------------------------------------------------------
    def test_tools_version(self):
        for ver, ok in (('2.10', True), ('2.9', True), ('2.8', False), ('3.0', True), ('1.12', False),
                        ('garbage', False)):
            with self.subTest(ver=ver):
                for f in FILES:
                    write(self.p(CAR[f], f), self.old[f])
                write(self.p('fs/rwdata/dev/mods_tools.txt'), f'FMODS_TOOLS_{ver}\n'.encode())
                if os.path.exists(self.p('log')):
                    os.remove(self.p('log'))
                env = dict(os.environ, LU_ROOT=self.r, LU_NO_WAIT='1')
                subprocess.run([SH, self.p(SCRIPT)], env=env, capture_output=True, timeout=60)
                log = read(self.p('log')).decode() if os.path.exists(self.p('log')) else ''
                self.assertEqual(self.car() == self.new, ok, log)
                self.assertEqual('or higher is required' in log, not ok, log)


class PosixCksum(unittest.TestCase):
    def test_known_values(self):
        self.assertEqual(usb_package.posix_cksum(b''), (4294967295, 0))
        self.assertEqual(usb_package.posix_cksum(b'123456789'), (930766865, 9))   # CRC-32/CKSUM check

    @unittest.skipUnless(shutil.which('cksum'), 'needs cksum')
    def test_matches_gnu_cksum(self):
        with tempfile.TemporaryDirectory() as d:
            samples = [b'', b'a', b'LogosUpdater\n', bytes(range(256)) * 999, os.urandom(3 << 20 | 17)]
            for i, data in enumerate(samples):
                p = os.path.join(d, f'f{i}')
                write(p, data)
                try:
                    out = subprocess.run(['cksum', p], capture_output=True, text=True, check=True).stdout.split()
                except OSError as e:     # e.g. a sandbox that cannot start Git Bash's cksum.exe (WinError 623)
                    self.skipTest(f'cannot run cksum here: {e}')
                self.assertEqual(usb_package.posix_cksum(path=p), (int(out[0]), int(out[1])))
                self.assertEqual(usb_package.posix_cksum(data), (int(out[0]), int(out[1])))


if __name__ == '__main__':
    unittest.main()
