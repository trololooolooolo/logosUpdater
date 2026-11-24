"""QNX6 round trip on a small synthetic image: build -> read -> extract -> merge -> build -> read.

Same path as the app's "Build image" (build_logo_img.merge + QNX6Writer with the loaded image as
template), so it also guards the pure-Python file system code on Windows/macOS in CI.
Run: python3 -m unittest tests.test_qnx6_roundtrip
"""
import hashlib
import os
import struct
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from qnx6fs import QNX6Reader, QNX6Writer, BOOTBLOCK_SIZE, crc32_be  # noqa: E402
import build_logo_img  # noqa: E402


def tree_digest(root):
    """{'E2/ABC.PNG': md5} for every file under root (forward slashes on every OS)."""
    out = {}
    for d, _dirs, files in os.walk(root):
        for f in files:
            p = os.path.join(d, f)
            with open(p, 'rb') as fh:
                out[os.path.relpath(p, root).replace(os.sep, '/')] = hashlib.md5(fh.read()).hexdigest()
    return out


def image_digest(img):
    r = QNX6Reader(img)
    try:
        ok = r.checksum_ok
        files = {path.lstrip('/'): hashlib.md5(r.read_inode_data(ino)).hexdigest()
                 for path, ino, kind in r.walk() if kind == 'file'}
    finally:
        r.close()
    return ok, files


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(data)


class RoundTrip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def p(self, *a):
        return os.path.join(self.d, *a)

    def test_build_extract_merge_build(self):
        # 1) a "car" image: one dir with many entries (multi-block directory), a file bigger
        #    than 16 blocks (indirect pointers), bytes that would break in text mode (\r\n, \x1a)
        src = self.p('src')
        for i in range(150):
            write(os.path.join(src, 'E2', f'S{i:03d}.PNG'), os.urandom(200 + i))
        write(os.path.join(src, 'E0', 'BIG.PNG'), os.urandom(40 * 1024))
        write(os.path.join(src, 'E0', 'CRLF.PNG'), b'\x89PNG\r\n\x1a\n' + b'\r\n' * 300)
        write(os.path.join(src, 'Version.inf'), b'Version Number = TEST-1\r\n')
        write(os.path.join(src, 'E0', 'A_LONG_FILE_NAME_OVER_27_BYTES.PNG'), os.urandom(700))   # longfile tree
        tmpl = self.p('tmpl.img')
        sb = bytearray(512)
        struct.pack_into('<I', sb, 0x30, 1024)
        struct.pack_into('<I', sb, 0x44, 1)
        write(tmpl, bytes(BOOTBLOCK_SIZE) + bytes(sb))
        car = self.p('car.img')
        QNX6Writer(src, template_img=tmpl, image_size=2 << 20).write(car)

        ok, files = image_digest(car)
        self.assertTrue(ok, 'superblock checksum')
        # fs-qnx6 looks for superblock #2 in the device's last 0x1000 bytes (with bs=1024 an older formula put it elsewhere)
        with open(car, 'rb') as f:
            f.seek(-0x1000, 2)
            sb2 = f.read(512)
        self.assertEqual(struct.unpack_from('<I', sb2, 0)[0], 0x68191122)
        self.assertEqual(struct.unpack_from('<I', sb2, 4)[0], crc32_be(sb2[8:512]))
        self.assertEqual(files, tree_digest(src))

        # 2) extract, merge new + replaced logos the way the app does, rebuild on the car image
        work = self.p('work')
        r = QNX6Reader(car)
        self.assertEqual(r.extract_to(work), len(files))
        r.close()
        self.assertEqual(tree_digest(work), files)
        sync = self.p('sync5')
        write(os.path.join(sync, 'E2', 'NEW.PNG'), os.urandom(500))
        write(os.path.join(sync, 'E2', 'S001.PNG'), os.urandom(300))   # differs from the image
        added, updated, _ = build_logo_img.merge(sync, work, overwrite=True)
        self.assertEqual((added, updated), (1, 1))
        out = self.p('new.img')
        QNX6Writer(work, template_img=car).write(out)

        ok, got = image_digest(out)
        self.assertTrue(ok)
        self.assertEqual(got, tree_digest(work))
        self.assertEqual(os.path.getsize(out), os.path.getsize(car), 'size taken from the template')
        with open(out, 'rb') as f:
            f.seek(BOOTBLOCK_SIZE)
            sb = f.read(512)
        self.assertEqual(struct.unpack_from('<I', sb, 4)[0], crc32_be(sb[8:512]))


if __name__ == '__main__':
    unittest.main()
