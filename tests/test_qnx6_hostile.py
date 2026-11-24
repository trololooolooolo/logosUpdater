"""Truncated, corrupt and crafted QNX6 images must raise QNX6Error quickly, without eating memory or
disk, while every real and generated image still reads. Data blocks carry no CRC, so the patched
images below still pass the superblock checksum: the reader's geometry and tree checks stop them.
Run: python3 -m unittest tests.test_qnx6_hostile
"""
import os
import struct
import sys
import tempfile
import time
import tracemalloc
import unittest

try:
    import resource                     # POSIX only
except ImportError:
    resource = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from qnx6fs import (QNX6Reader, QNX6Writer, QNX6Error, validate_image, crc32_be,  # noqa: E402
                    BOOTBLOCK_SIZE, SUPERBLOCK_AREA, INODE_SIZE, PTR_NONE)

REAL = os.path.join(ROOT, 'work', 'station_logos.img')
SB = BOOTBLOCK_SIZE
# a hostile image must fail fast; Windows timers and antivirus are noisier, so allow more there
PARSE_LIMIT_S = 10.0 if os.name == 'nt' else 1.0
RN = 0x48                       # root node of the inode table inside the superblock


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(data)


def read(path):
    with open(path, 'rb') as f:
        return f.read()


class Hostile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        src = self.p('src')
        write(self.p('src', 'Version.inf'), b'Version Number = TEST-1\n')
        write(self.p('src', 'E2', 'BIG.PNG'), bytes(range(256)) * 2400)       # 600 KB, 600 blocks: levels=1
        write(self.p('src', 'E2', 'A.PNG'), b'\x89PNG a')
        write(self.p('src', 'E2', 'B.PNG'), b'\x89PNG b')
        write(self.p('src', 'E2', 'C.PNG'), b'\x89PNG c')
        sb = bytearray(512)
        struct.pack_into('<I', sb, 0x30, 1024)
        struct.pack_into('<I', sb, 0x44, 1)
        tmpl = self.p('tmpl.img')
        write(tmpl, bytes(BOOTBLOCK_SIZE) + bytes(sb))
        self.img = self.p('car.img')
        QNX6Writer(src, template_img=tmpl, image_size=2 << 20).write(self.img)
        self.data = bytearray(read(self.img))
        with QNX6Reader(self.img) as r:
            self.bs = r.blocksize
            self.nb = r.num_blocks
            self.itable = r._inode_blocks
            self.ino_of = {}
            for name, ino in r.read_dir(r.get_inode(1)):
                if name == 'E2':
                    for n2, i2 in r.read_dir(r.get_inode(ino)):
                        self.ino_of[n2] = i2
            big = r.get_inode(self.ino_of['BIG.PNG'])
            self.big_levels = big.filelevels
            self.big_ptr0 = big.block_ptr[0]

    def tearDown(self):
        self.tmp.cleanup()

    def p(self, *a):
        return os.path.join(self.d, *a)

    def off(self, block):
        return BOOTBLOCK_SIZE + SUPERBLOCK_AREA + block * self.bs

    def inode_off(self, ino):
        idx, inner = divmod((ino - 1) * INODE_SIZE, self.bs)
        return self.off(self.itable[idx]) + inner

    def save(self, data=None, name='evil.img', fix_sb=False):
        data = bytearray(self.data if data is None else data)
        if fix_sb:
            struct.pack_into('<I', data, SB + 4, crc32_be(bytes(data[SB + 8:SB + 512])))
        path = self.p(name)
        write(path, bytes(data))
        return path

    def assertBroken(self, path, msg=None):
        # Read the fresh file once before timing: on Windows the on-access antivirus scan of a new file
        # happens at open() and can take seconds under load. Only the parse is timed.
        read(path)
        if tracemalloc.is_tracing():
            tracemalloc.reset_peak()        # the warm-up read is not the parser's memory
        t = time.perf_counter()
        with self.assertRaises(QNX6Error) as cm:
            validate_image(path)
        self.assertLess(time.perf_counter() - t, PARSE_LIMIT_S)
        if msg:
            self.assertIn(msg, str(cm.exception))
        return cm.exception

    # --- sanity ---------------------------------------------------------------------
    def test_clean_image_validates(self):
        self.assertEqual(self.big_levels, 1)
        self.assertEqual(validate_image(self.img), {'files': 5, 'version': 'TEST-1'})
        with QNX6Reader(self.img) as r:
            self.assertEqual(r.extract_to(self.p('out')), 5)
        self.assertEqual(read(self.p('out', 'E2', 'BIG.PNG')), read(self.p('src', 'E2', 'BIG.PNG')))

    def test_errors_are_value_errors(self):
        self.assertTrue(issubclass(QNX6Error, ValueError))

    # --- truncation (C2) --------------------------------------------------------------
    def test_truncated_half(self):
        self.assertBroken(self.save(self.data[:len(self.data) // 2]), 'truncated')

    def test_truncated_inside_data(self):
        cut = self.off(self.big_ptr0) + 3 * self.bs + 100      # in the middle of BIG.PNG
        self.assertBroken(self.save(self.data[:cut]), 'truncated')

    def test_truncated_inside_inode_table(self):
        cut = self.off(self.itable[0]) + 200
        self.assertBroken(self.save(self.data[:cut]), 'truncated')

    def test_truncated_before_superblock(self):
        self.assertBroken(self.save(self.data[:SB + 100]))

    def test_short_read_block(self):
        with QNX6Reader(self.img) as r:
            with self.assertRaises(QNX6Error):
                r.read_block(self.nb)

    # --- crafted geometry ---------------------------------------------------------------
    def test_bad_blocksize(self):
        struct.pack_into('<I', self.data, SB + 0x30, 1000)
        self.assertBroken(self.save(fix_sb=True), 'block size')

    def test_too_many_inodes(self):
        struct.pack_into('<I', self.data, SB + 0x34, self.nb * self.bs)
        self.assertBroken(self.save(fix_sb=True), 'inodes')

    def test_checksum_mismatch(self):
        self.data[SB + 0x10] ^= 0xFF          # ctime: harmless, but the checksum no longer matches
        self.assertEqual(validate_image(self.save())['files'], 5, 'a damaged SB1 falls back to SB2')
        self.data[len(self.data) - SUPERBLOCK_AREA + 0x10] ^= 0xFF
        self.assertBroken(self.save(), 'checksum')

    # --- crafted block trees (I2) ---------------------------------------------------------
    def bomb(self, levels, size=1 << 40):
        """bomb.py from the review: inode-table root points at a block full of pointers to itself."""
        x = 5
        o = self.off(x)
        self.data[o:o + self.bs] = struct.pack('<%dI' % (self.bs // 4), *([x] * (self.bs // 4)))
        struct.pack_into('<Q', self.data, SB + RN, size)
        struct.pack_into('<16I', self.data, SB + RN + 8, x, *([PTR_NONE] * 15))
        self.data[SB + RN + 72] = levels
        return self.save(fix_sb=True)

    def test_bomb_fails_fast_small_memory(self):
        for levels, size in ((4, 1 << 40), (2, 1 << 40), (2, 64 * 1024), (5, 4096)):
            with self.subTest(levels=levels, size=size):
                self.data = bytearray(read(self.img))
                path = self.bomb(levels, size)
                before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss if resource else 0
                tracemalloc.start()
                try:
                    self.assertBroken(path)
                    peak_mb = tracemalloc.get_traced_memory()[1] / (1 << 20)
                finally:
                    tracemalloc.stop()
                self.assertLess(peak_mb, 50, 'Python allocations while parsing (all systems)')
                if resource:        # ru_maxrss is KiB on Linux (bytes on macOS: the bound only gets looser)
                    grown_mb = (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before) / 1024
                    self.assertLess(grown_mb, 50)

    def test_self_referencing_block_in_file(self):
        # BIG.PNG (levels=1): make its first indirect block point at itself
        ib = self.big_ptr0
        struct.pack_into('<I', self.data, self.off(ib), ib)
        self.assertBroken(self.save(), 'referenced twice')

    def test_levels_5_in_file(self):
        self.data[self.inode_off(self.ino_of['A.PNG']) + 0x64] = 5
        self.assertBroken(self.save(), 'levels')

    def test_pointer_beyond_num_blocks(self):
        struct.pack_into('<I', self.data, self.inode_off(self.ino_of['A.PNG']) + 0x24, self.nb + 7)
        self.assertBroken(self.save(), 'beyond')

    def test_file_size_beyond_file_system(self):
        struct.pack_into('<Q', self.data, self.inode_off(self.ino_of['A.PNG']), self.nb * self.bs + 1)
        self.assertBroken(self.save(), 'larger than')

    def test_missing_blocks(self):
        # A.PNG claims 3 blocks but has one pointer
        struct.pack_into('<Q', self.data, self.inode_off(self.ino_of['A.PNG']), 3 * self.bs)
        self.assertBroken(self.save(), 'data blocks')

    def test_inode_number_outside_table(self):
        with QNX6Reader(self.img) as r:
            with self.assertRaises(QNX6Error):
                r.get_inode(10 ** 6)
            with self.assertRaises(QNX6Error):
                r.get_inode(0)

    def test_extract_capped_at_image_size(self):
        # A/B/C entries all point at BIG.PNG's inode: 4 x 600 KB > the 2 MB image
        e2 = self.data
        big = self.ino_of['BIG.PNG']
        for n in (b'A.PNG', b'B.PNG', b'C.PNG'):
            i = e2.find(bytes([len(n)]) + n)
            self.assertGreater(i, 4)
            struct.pack_into('<I', e2, i - 4, big)
        path = self.save()
        with QNX6Reader(path) as r:
            with self.assertRaises(QNX6Error) as cm:
                r.extract_to(self.p('out'))
        self.assertIn('more file data', str(cm.exception))
        self.assertBroken(path, 'more file data')

    def test_validate_empty_image(self):
        empty = self.p('empty')
        os.makedirs(empty)
        out = self.p('empty.img')
        QNX6Writer(empty, template_img=self.img, image_size=1 << 20).write(out)
        self.assertBroken(out, 'no files')


class WriterAtomic(unittest.TestCase):
    def test_failed_write_leaves_no_file(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, 'src')
            write(os.path.join(src, 'E2', 'A.PNG'), b'png')
            tmpl = os.path.join(d, 'tmpl.img')
            sb = bytearray(512)
            struct.pack_into('<I', sb, 0x30, 1024)
            write(tmpl, bytes(BOOTBLOCK_SIZE) + bytes(sb))
            out = os.path.join(d, 'out.img')
            w = QNX6Writer(src, template_img=tmpl, image_size=1 << 20)
            with mock.patch('qnx6fs.os.fsync', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    w.write(out)
            self.assertEqual(sorted(os.listdir(d)), ['src', 'tmpl.img'])
            QNX6Writer(src, template_img=tmpl, image_size=1 << 20).write(out)
            self.assertEqual(validate_image(out)['files'], 1)
            self.assertFalse(os.path.exists(out + '.tmp'))


class RealImages(unittest.TestCase):
    def test_demo_set_validates(self):
        sys.path.insert(0, os.path.join(ROOT, 'tools'))
        import demo_set
        with tempfile.TemporaryDirectory() as d:
            s = demo_set.build(os.path.join(d, 'set'))
            info = validate_image(os.path.join(s, 'station_logos.img'))
        self.assertGreater(info['files'], 0)
        self.assertEqual(info['version'], demo_set.VERSION)

    @unittest.skipUnless(os.path.isfile(REAL), 'no work/station_logos.img here')
    def test_real_work_image(self):
        before = os.stat(REAL)
        info = validate_image(REAL)
        with QNX6Reader(REAL) as r:
            files = sum(1 for _, _, k in r.walk() if k == 'file')
        self.assertEqual(info['files'], files)
        self.assertGreater(files, 0)
        self.assertEqual(os.stat(REAL).st_mtime, before.st_mtime, 'read only')


if __name__ == '__main__':
    unittest.main()
