"""Writing a package straight onto a stick: staged, verified, then swapped (any OS).

usb_targets() is mocked (a temp folder plays the stick); the demo set comes from tools/demo_set.py.
Run: python3 -m unittest tests.test_stick_write
"""
import hashlib
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import usb_package  # noqa: E402

FILES = usb_package.FILES


def write(p, data):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, 'wb') as f:
        f.write(data)


def tree_digest(root):
    """{relative path: md5} of every file under root."""
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            p = os.path.join(dirpath, fn)
            with open(p, 'rb') as f:
                out[os.path.relpath(p, root)] = hashlib.md5(f.read()).hexdigest()
    return out


class StickWrite(unittest.TestCase):
    def setUp(self):
        import demo_set
        self.tmp = tempfile.TemporaryDirectory()
        self.set = demo_set.build(os.path.join(self.tmp.name, 'set'))
        self.files = [os.path.join(self.set, f) for f in FILES]
        self.pkg_mb = sum(os.path.getsize(p) for p in self.files) / 1048576
        self.stick = os.path.join(self.tmp.name, 'stick')
        os.makedirs(self.stick)
        self.free_mb = 10000
        self.patch = mock.patch.object(usb_package, 'usb_targets', side_effect=lambda: [
            {'path': self.stick, 'fstype': 'vfat', 'free_mb': self.free_mb, 'has_pkg': False}])
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def build(self):
        return usb_package.build_package(*self.files, os.path.join(self.tmp.name, 'zips'), target=self.stick)

    def pkg(self, *parts):
        return os.path.join(self.stick, 'SyncMyMod', *parts)

    def add_backup(self):
        for f in FILES + ('BACKUP_INFO.txt',):          # what the first install on the car leaves there
            write(self.pkg('backup', f), f'ORIGINAL {f}'.encode())

    def leftovers(self):
        return [n for n in os.listdir(self.stick) if n.startswith('SyncMyMod.new-')]

    def test_success_keeps_backup(self):
        first = self.build()
        self.assertEqual(first['old_folders'], [])
        self.assertNotIn('moved_old', first)
        self.add_backup()
        second = self.build()
        self.assertTrue(second['kept_backup'])
        self.assertEqual(sorted(os.listdir(self.pkg('backup'))), sorted(FILES + ('BACKUP_INFO.txt',)))
        self.assertEqual([o['name'] for o in second['old_folders']], [second['moved_old']])
        self.assertFalse(os.path.exists(os.path.join(self.stick, second['moved_old'], 'backup')))
        self.assertEqual(sorted(os.listdir(self.pkg())),
                         ['README_USB.txt', 'autoinstall.sh', 'backup', 'files'])
        for name, md5 in second['manifest'].items():
            with open(self.pkg('files', 'other', name), 'rb') as f:
                self.assertEqual(hashlib.md5(f.read()).hexdigest(), md5)
        self.assertEqual(self.leftovers(), [])

    def test_failed_copy_leaves_old_package(self):
        self.build()
        self.add_backup()
        before = tree_digest(self.pkg())
        listing = sorted(os.listdir(self.stick))
        real = shutil.copyfile
        calls = []

        def flaky(src, dst, *a, **kw):
            calls.append(dst)
            if len(calls) > 1:
                raise OSError(5, 'Input/output error (stick pulled)')
            return real(src, dst, *a, **kw)

        with mock.patch.object(usb_package.shutil, 'copyfile', side_effect=flaky):
            with self.assertRaises(IOError) as cm:
                self.build()
        self.assertIn('untouched', str(cm.exception))
        self.assertTrue(all('SyncMyMod.new-' in d for d in calls), calls)
        self.assertEqual(tree_digest(self.pkg()), before)
        self.assertEqual(sorted(os.listdir(self.stick)), listing)
        self.assertEqual(self.leftovers(), [])

    def test_md5_mismatch_leaves_old_package(self):
        self.build()
        before = tree_digest(self.pkg())
        with mock.patch.object(usb_package, '_verify_tree', side_effect=IOError('md5 mismatch after copy: x')):
            with self.assertRaises(IOError):
                self.build()
        self.assertEqual(tree_digest(self.pkg()), before)
        self.assertEqual(self.leftovers(), [])

    def test_no_space_mentions_old_folders(self):
        self.build()
        self.build()                                    # leaves one SyncMyMod.old-*
        olds = [n for n in os.listdir(self.stick) if n.startswith('SyncMyMod.old-')]
        self.assertEqual(len(olds), 1)
        self.free_mb = 0
        with self.assertRaises(ValueError) as cm:
            self.build()
        msg = str(cm.exception)
        self.assertIn('0 MB free', msg)
        self.assertIn(olds[0], msg)
        self.assertEqual(self.leftovers(), [])

    def test_required_space_with_and_without_backup(self):
        # without a backup on the stick: package + room for the car's backup (~2x)
        margin = mock.patch.object(usb_package, 'STICK_MARGIN_MB', 0)
        margin.start()
        self.addCleanup(margin.stop)
        self.free_mb = self.pkg_mb * 1.5
        with self.assertRaises(ValueError) as cm:
            self.build()
        self.assertIn('backup', str(cm.exception))
        self.assertFalse(os.path.exists(self.pkg()))
        # a complete backup already on the stick: the car writes none, ~1x is enough
        self.add_backup()
        self.assertEqual(self.build()['status'], 'ok')
        # an incomplete one does not count
        os.remove(self.pkg('backup', 'dab_station_logo.db'))
        with self.assertRaises(ValueError):
            self.build()


if __name__ == '__main__':
    unittest.main()
