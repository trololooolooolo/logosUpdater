"""A crafted QNX6 image must not write outside the extraction directory (path traversal),
nor loop forever on a directory cycle. Data blocks carry no CRC, so the patched images still pass
the superblock checksum: the reader's own guards are what stop them.
Run: python3 -m unittest tests.test_security_image
"""
import os
import struct
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import qnx6fs  # noqa: E402
from qnx6fs import QNX6Reader, QNX6Writer, BOOTBLOCK_SIZE, ROOT_INO  # noqa: E402

LONG = b'A_LONG_FILE_NAME_OVER_27_BYTES.PNG'


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(data)


def all_files(root):
    return sorted(os.path.relpath(os.path.join(d, f), root).replace(os.sep, '/')
                  for d, _dirs, files in os.walk(root) for f in files)


class CraftedImage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        src = self.p('src')
        write(self.p('src', 'E2', 'DE000A01.PNG'), b'\x89PNG one')
        write(self.p('src', 'E2', 'OK.PNG'), b'\x89PNG ok')
        write(self.p('src', 'E0', 'CRLF.PNG'), b'\x89PNG crlf')
        write(self.p('src', 'E0', LONG.decode()), b'\x89PNG long')
        tmpl = self.p('tmpl.img')
        sb = bytearray(512)
        struct.pack_into('<I', sb, 0x30, 1024)
        struct.pack_into('<I', sb, 0x44, 1)
        write(tmpl, bytes(BOOTBLOCK_SIZE) + bytes(sb))
        self.img = self.p('car.img')
        QNX6Writer(src, template_img=tmpl, image_size=2 << 20).write(self.img)
        with open(self.img, 'rb') as f:
            self.data = bytearray(f.read())
        # extraction target nested so that ../../ still lands inside the temp dir
        self.out = self.p('x', 'a', 'b')
        os.makedirs(self.out)

    def tearDown(self):
        self.tmp.cleanup()

    def p(self, *a):
        return os.path.join(self.d, *a)

    def patched(self, old, new):
        self.assertEqual(len(old), len(new))
        i = self.data.find(old)
        self.assertGreater(i, 0, old)
        self.data[i:i + len(old)] = new
        path = self.p('evil.img')
        write(path, bytes(self.data))
        return path

    def extract(self, img):
        with QNX6Reader(img) as r:
            self.assertTrue(r.checksum_ok, 'patched image still passes the checksum')
            with self.assertLogs('qnx6fs', 'WARNING'):
                n = r.extract_to(self.out)
        return n

    def outside(self):
        return [f for f in all_files(self.d) if not f.startswith(('x/a/b/', 'src/'))
                and f not in ('tmpl.img', 'car.img', 'evil.img')]

    def test_clean_image_extracts(self):
        with QNX6Reader(self.img) as r:
            self.assertEqual(r.extract_to(self.out), 4)
        self.assertEqual(all_files(self.out), sorted(['E0/CRLF.PNG', 'E0/' + LONG.decode(),
                                                      'E2/DE000A01.PNG', 'E2/OK.PNG']))

    def test_short_name_traversal(self):
        img = self.patched(bytes([12]) + b'DE000A01.PNG', bytes([12]) + b'../../PWNED1')
        self.assertEqual(self.extract(img), 3)
        self.assertEqual(self.outside(), [])
        self.assertNotIn('PWNED1', ''.join(all_files(self.d)))
        self.assertIn('E2/OK.PNG', all_files(self.out))

    def test_separators_in_short_name(self):
        clean = bytes(self.data)
        for name in (b'E2\\..\\..\\X1', b'..\\..\\PWNED', b'C:PWNED.PNG', b'..', b'A\0B'):
            with self.subTest(name=name):
                self.data = bytearray(clean)
                img = self.patched(bytes([12]) + b'DE000A01.PNG', bytes([len(name)]) + name.ljust(12, b'\0'))
                with QNX6Reader(img) as r:
                    names = [p for p, _, _ in r.walk()]
                self.assertEqual(sorted(names), sorted(['/E0', '/E0/CRLF.PNG', '/E0/' + LONG.decode(), '/E2', '/E2/OK.PNG']))

    def test_long_name_traversal(self):
        evil = b'../../../PWNED_LONG'.ljust(len(LONG), b'X')
        img = self.patched(LONG, evil)
        self.assertEqual(self.extract(img), 3)
        self.assertEqual(self.outside(), [])
        self.assertNotIn('PWNED', ''.join(all_files(self.d)))

    def test_contained_helper(self):
        root = os.path.realpath(self.out)
        self.assertIsNone(qnx6fs._contained(root, '/../../x'))
        self.assertIsNone(qnx6fs._contained(root, '/'))
        self.assertEqual(qnx6fs._contained(root, '/E2/A.PNG'), os.path.join(root, 'E2', 'A.PNG'))
        # a symlink already inside outdir pointing outside is not followed
        try:
            os.symlink(self.d, os.path.join(self.out, 'link'))
        except (OSError, NotImplementedError):  # Windows without admin rights or Developer Mode
            self.skipTest('symlinks need admin rights or Developer Mode here (path checks above passed)')
        self.assertIsNone(qnx6fs._contained(root, '/link/PWNED'))

    def test_directory_loop_terminates(self):
        # point the E0/CRLF.PNG entry at the root directory inode -> cycle
        i = self.data.find(bytes([8]) + b'CRLF.PNG')
        self.assertGreater(i, 4)
        struct.pack_into('<I', self.data, i - 4, ROOT_INO)
        img = self.p('loop.img')
        write(img, bytes(self.data))
        with QNX6Reader(img) as r:
            self.assertTrue(r.checksum_ok)
            with self.assertLogs('qnx6fs', 'WARNING'):
                paths = [p for p, _, _ in r.walk()]
        self.assertNotIn('/E0/CRLF.PNG', paths)
        self.assertIn('/E2/OK.PNG', paths)


if __name__ == '__main__':
    unittest.main()
