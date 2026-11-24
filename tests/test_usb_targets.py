"""Removable-drive detection on Linux / Windows / macOS with the OS calls replaced.

Run: python3 -m unittest tests.test_usb_targets
"""
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config  # noqa: E402
import usb_package  # noqa: E402


class FakeKernel32:
    """GetLogicalDrives / GetDriveTypeW / GetVolumeInformationW for C:, D:, E:."""
    DRIVES = {'C': (3, 'NTFS'), 'D': (5, 'CDFS'), 'E': (2, 'FAT32'), 'F': (2, None)}  # F: empty card slot

    def GetLogicalDrives(self):
        return sum(1 << (ord(k) - 65) for k in self.DRIVES)

    def GetDriveTypeW(self, root):
        return self.DRIVES[root[0]][0]

    def GetVolumeInformationW(self, root, *a):
        fs = self.DRIVES[root[0]][1]
        if fs is None:
            return 0
        a[5].value = fs
        return 1


class Detect(unittest.TestCase):
    def test_linux_proc_mounts(self):
        mounts = ("/dev/sda1 / ext4 rw 0 0\n"
                  "/dev/sdb1 /media/me/NO\\040NAME vfat rw 0 0\n"
                  "/dev/sdc1 /run/media/me/BIG ext4 rw 0 0\n")
        with mock.patch.object(sys, 'platform', 'linux'), \
                mock.patch('builtins.open', mock.mock_open(read_data=mounts)):
            got = config.removable_mounts()
        self.assertEqual(got, [{'path': '/media/me/NO NAME', 'fstype': 'vfat'},
                               {'path': '/run/media/me/BIG', 'fstype': 'ext4'}])

    def test_windows_removable_letters(self):
        import ctypes
        fake = types.SimpleNamespace(kernel32=FakeKernel32())
        with mock.patch.object(sys, 'platform', 'win32'), \
                mock.patch.object(ctypes, 'windll', fake, create=True):
            got = config.removable_mounts()
        self.assertEqual(got, [{'path': 'E:\\', 'fstype': 'fat32'}])

    def test_macos_volumes(self):
        out = ("/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)\n"
               "/dev/disk4s1 on /Volumes/NO NAME (msdos, local, nodev, nosuid, noowners)\n"
               "/dev/disk5s1 on /Volumes/Backup (apfs, local, journaled)\n")
        run = mock.Mock(return_value=types.SimpleNamespace(stdout=out))
        with mock.patch.object(sys, 'platform', 'darwin'), mock.patch('subprocess.run', run):
            got = config.removable_mounts()
        self.assertEqual(got, [{'path': '/Volumes/NO NAME', 'fstype': 'msdos'},
                               {'path': '/Volumes/Backup', 'fstype': 'apfs'}])

    def test_detection_error_means_no_drives(self):
        with mock.patch.object(sys, 'platform', 'linux'), \
                mock.patch('builtins.open', side_effect=OSError):
            self.assertEqual(config.removable_mounts(), [])


class Targets(unittest.TestCase):
    def test_only_writable_fat_like_sticks_with_free_space(self):
        with tempfile.TemporaryDirectory() as d:
            fat, ext = os.path.join(d, 'fat'), os.path.join(d, 'ext')
            os.makedirs(os.path.join(fat, usb_package.PKG_DIR))
            os.makedirs(ext)
            mounts = [{'path': fat, 'fstype': 'FAT32'}, {'path': ext, 'fstype': 'apfs'}]
            usage = types.SimpleNamespace(total=0, used=0, free=2048 * 1048576)
            with mock.patch.object(config, 'removable_mounts', return_value=mounts), \
                    mock.patch('shutil.disk_usage', return_value=usage):
                got = usb_package.usb_targets()
        self.assertEqual(got, [{'path': fat, 'fstype': 'FAT32', 'free_mb': 2048, 'has_pkg': True}])

    def test_input_dirs_find_image_within_depth(self):
        with tempfile.TemporaryDirectory() as d:
            deep = os.path.join(d, 'SyncMyMod', 'files', 'other')
            os.makedirs(deep)
            open(os.path.join(deep, 'STATION_LOGOS.IMG'), 'wb').close()
            with mock.patch.object(config, 'removable_mounts', return_value=[{'path': d, 'fstype': 'vfat'}]):
                self.assertEqual(config.usb_input_dirs(), [{'path': deep, 'mount': d}])
                self.assertEqual(config.usb_input_dirs(max_depth=2), [])


if __name__ == '__main__':
    unittest.main()
