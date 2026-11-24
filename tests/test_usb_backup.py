"""The car-side install script and the backup it makes, run for real in `sh` on a fake car.

autoinstall.sh honours LU_ROOT (prefix for /fs/..., /tmp/...) and LU_NO_WAIT (don't wait for the
stick to be pulled) — both unset on the car. FMods tools, remount scripts, umount and sleep are
stubbed in the fake /fs/rwdata/dev, which the script puts first on PATH.
Run: python3 -m unittest tests.test_usb_backup
"""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import usb_package  # noqa: E402

FILES = ('station_logos.img', 'fm_station_logo.db', 'dab_station_logo.db')
CAR = {'station_logos.img': 'fs/images', 'fm_station_logo.db': 'fs/mp/resources/FM/station_logo',
       'dab_station_logo.db': 'fs/mp/resources/DAB/station_logo'}
STUBS = {
    'instutility': 'exit 0',
    'utserviceutility': 'cat "$2" >> "$LU_ROOT/log"',
    'remount_rw.sh': 'echo RW >> "$LU_ROOT/log"',
    'remount_ro.sh': 'echo RO >> "$LU_ROOT/log"',
    'umount': 'echo UMOUNT >> "$LU_ROOT/log"',
    'sleep': 'exit 0',
}


def write(p, data):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, 'wb') as f:
        f.write(data)


def read(p):
    with open(p, 'rb') as f:
        return f.read()


class FakeCar:
    """Mixin: a fake car tree with the stubs, the old car files and a package on fs/usb0
    (shared with tests/test_car_install.py)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.r = self.tmp.name
        dev = self.p('fs/rwdata/dev')
        os.makedirs(dev)
        write(os.path.join(dev, 'mods_tools.txt'), b'FMODS_TOOLS_2.9\n')
        for name, body in STUBS.items():
            self.stub(name, body)
        for d in ('fs/tmpfs', 'tmp'):
            os.makedirs(self.p(d))
        write(self.p('fs/mp/resources/station_logo/Logo/Version.inf'), b'Version Number = CAR-1\n')
        self.old = {f: f'OLD {f} '.encode() * 50 for f in FILES}
        self.new = {f: f'NEW {f} '.encode() * 70 for f in FILES}
        for f in FILES:
            write(self.p(CAR[f], f), self.old[f])
        src = self.p('pc')
        for f in FILES:
            write(os.path.join(src, f), self.new[f])
        usb_package._write_tree(self.p('fs/usb0'), *(os.path.join(src, f) for f in FILES), 'test', 'built')

    def tearDown(self):
        for d, _dirs, _files in os.walk(self.r):
            os.chmod(d, 0o755)
        self.tmp.cleanup()

    def p(self, *parts):
        return os.path.join(self.r, *parts)

    def stub(self, name, body):
        path = self.p('fs/rwdata/dev', name)
        write(path, f'#!/bin/sh\n{body}\n'.encode())
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)

    def run_script(self):
        env = dict(os.environ, LU_ROOT=self.r, LU_NO_WAIT='1')
        res = subprocess.run(['sh', self.p('fs/usb0/SyncMyMod/autoinstall.sh')], env=env,
                             capture_output=True, text=True, timeout=60)
        with open(self.p('fs/tmpfs/status'), encoding='utf-8') as f:
            status = f.read().strip()
        log = read(self.p('log')).decode() if os.path.exists(self.p('log')) else ''
        return res, status, log

    def car(self):
        return {f: read(self.p(CAR[f], f)) for f in FILES}

    def backup(self):
        d = self.p('fs/usb0/SyncMyMod/backup')
        return {f: read(os.path.join(d, f)) for f in FILES if os.path.exists(os.path.join(d, f))}



@unittest.skipUnless(shutil.which('sh') and os.name == 'posix', 'needs a POSIX sh')
class InstallScript(FakeCar, unittest.TestCase):
    def test_source_name_cannot_inject_shell(self):
        evil = 'set\nrm -rf /\n$(touch PWNED) `touch PWNED2`; "x" \'y\' \r\u017c'
        root = self.p('evil')
        src = self.p('pc')
        usb_package._write_tree(root, *(os.path.join(src, f) for f in FILES), evil, 'built')
        script = read(os.path.join(root, 'SyncMyMod', 'autoinstall.sh')).decode('ascii')
        hits = [ln for ln in script.split('\n') if 'PWNED' in ln or 'rm -rf /' in ln]
        self.assertEqual(len(hits), 1, hits)
        self.assertTrue(hits[0].startswith('# Generated: '), hits[0])
        self.assertNotRegex(hits[0], r'[$`;"\'\\\r]')
        self.assertNotIn('\r', script)
        res = subprocess.run(['sh', '-n', os.path.join(root, 'SyncMyMod', 'autoinstall.sh')],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(usb_package._script_safe('a' * 100), 'a' * 60)

    def test_install_makes_verified_backup(self):
        res, status, log = self.run_script()
        self.assertIn('Installation completed', status, res.stderr)
        self.assertEqual(self.backup(), self.old, 'backup = the car files before the install')
        self.assertEqual(self.car(), self.new)
        info = read(self.p('fs/usb0/SyncMyMod/backup/BACKUP_INFO.txt')).decode()
        self.assertIn('CAR-1', info)
        self.assertIn(f'station_logos.img: {len(self.old["station_logos.img"])} bytes', info)
        self.assertLess(log.index('RW'), log.index('UMOUNT'))
        self.assertTrue(os.path.exists(self.p(CAR['fm_station_logo.db'], 'fm_station_logo.db.ori')))

    def test_stick_full_changes_nothing(self):
        # cp to the stick writes only part of the image and still says "ok" (a full FAT stick can)
        self.stub('cp', 'case "$2" in */backup/*) head -c 100 "$1" > "$2/$(basename "$1")"; exit 0 ;; '
                        '*) exec /bin/cp "$@" ;; esac')
        res, status, log = self.run_script()
        self.assertIn('Backup to USB failed', status, res.stderr)
        self.assertEqual(self.car(), self.old, 'car files untouched')
        self.assertFalse(os.path.exists(self.p('fs/usb0/SyncMyMod/backup')), 'no half backup left behind')
        self.assertNotIn('RW', log, 'file systems never switched to read-write')

    def test_unwritable_stick_changes_nothing(self):
        os.chmod(self.p('fs/usb0/SyncMyMod'), 0o555)
        res, status, log = self.run_script()
        self.assertIn('Backup to USB failed', status, res.stderr)
        self.assertEqual(self.car(), self.old)
        self.assertNotIn('RW', log)

    def test_existing_backup_kept(self):
        first = {f: f'FIRST {f}'.encode() for f in FILES}
        for f in FILES:
            write(self.p('fs/usb0/SyncMyMod/backup', f), first[f])
        res, status, _ = self.run_script()
        self.assertIn('Installation completed', status, res.stderr)
        self.assertEqual(self.backup(), first, 'an existing backup is never overwritten')
        self.assertEqual(self.car(), self.new)

    def test_incomplete_backup_stops(self):
        write(self.p('fs/usb0/SyncMyMod/backup/station_logos.img'), b'x')
        res, status, log = self.run_script()
        self.assertIn('backup is incomplete', status, res.stderr)
        self.assertEqual(self.car(), self.old)
        self.assertEqual(self.backup(), {'station_logos.img': b'x'}, 'the user decides what to do with it')
        self.assertNotIn('RW', log)

    def test_missing_payload_changes_nothing(self):
        os.remove(self.p('fs/usb0/SyncMyMod/files/other/dab_station_logo.db'))
        res, status, log = self.run_script()
        self.assertIn('Missing dab_station_logo.db', status, res.stderr)
        self.assertEqual(self.car(), self.old)
        self.assertFalse(os.path.exists(self.p('fs/usb0/SyncMyMod/backup')))



class StickWrite(unittest.TestCase):
    """Writing a package onto a stick keeps SyncMyMod/backup (any OS)."""

    def setUp(self):
        from unittest import mock
        sys.path.insert(0, os.path.join(ROOT, 'tools'))
        import demo_set
        self.tmp = tempfile.TemporaryDirectory()
        self.set = demo_set.build(os.path.join(self.tmp.name, 'set'))
        self.stick = os.path.join(self.tmp.name, 'stick')
        os.makedirs(self.stick)
        self.patch = mock.patch.object(usb_package, 'usb_targets', return_value=[
            {'path': self.stick, 'fstype': 'vfat', 'free_mb': 10000, 'has_pkg': False}])
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def build(self):
        files = [os.path.join(self.set, f) for f in FILES]
        return usb_package.build_package(*files, os.path.join(self.tmp.name, 'zips'), target=self.stick)

    def test_backup_moves_into_new_package(self):
        first = self.build()
        self.assertNotIn('kept_backup', first)
        backup = os.path.join(self.stick, 'SyncMyMod', 'backup')
        for f in FILES + ('BACKUP_INFO.txt',):          # what the first install on the car leaves there
            write(os.path.join(backup, f), f'ORIGINAL {f}'.encode())
        second = self.build()
        self.assertTrue(second['kept_backup'])
        self.assertEqual(sorted(os.listdir(backup)), sorted(FILES + ('BACKUP_INFO.txt',)))
        self.assertEqual(read(os.path.join(backup, 'station_logos.img')), b'ORIGINAL station_logos.img')
        old = os.path.join(self.stick, second['moved_old'])
        self.assertFalse(os.path.exists(os.path.join(old, 'backup')), 'the renamed folder no longer has it')
        self.assertTrue(os.path.exists(os.path.join(self.stick, 'SyncMyMod', 'files', 'other', 'station_logos.img')))

    def test_single_top_level_folder(self):
        # "Extract here" must give SyncMyMod/ directly, so the README lives inside it
        import zipfile
        files = [os.path.join(self.set, f) for f in FILES]
        res = usb_package.build_package(*files, os.path.join(self.tmp.name, 'zips'))
        with zipfile.ZipFile(res['zip_path']) as z:
            names = z.namelist()
        self.assertEqual({n.split('/')[0] for n in names}, {'SyncMyMod'}, names)
        self.assertIn('SyncMyMod/README_USB.txt', names)
        write(os.path.join(self.stick, 'README_USB.txt'), b'old layout')
        self.build()
        self.assertEqual(sorted(os.listdir(self.stick))[:1], ['SyncMyMod'])
        self.assertFalse(os.path.exists(os.path.join(self.stick, 'README_USB.txt')))
        readme = read(os.path.join(self.stick, 'SyncMyMod', 'README_USB.txt')).decode()
        self.assertLess(readme.index('Installation'), readme.index('Instalacja'), 'English first')

    def test_no_os_sync_needed(self):
        from unittest import mock
        with mock.patch.object(usb_package.os, 'sync', create=True) as s:
            del usb_package.os.sync                       # like Windows
            try:
                self.assertEqual(self.build()['status'], 'ok')
            finally:
                usb_package.os.sync = s


if __name__ == '__main__':
    unittest.main()
