#!/usr/bin/env python3
"""Pure-Python QNX6 (fs-qnx6 "Power-Safe") reader + writer for Ubuntu.

Pozwala czytać i budować obrazy .img w formacie QNX6 bez QNX SDP ani QEMU.
Zweryfikowane na realnym, montowalnym przez Ford Sync 3.4 obrazie station_logos.img.

Kluczowe fakty o formacie (potwierdzone empirycznie na station_logos.img):
  * bootblock      : 0x2000 bajtów na początku
  * superblock #1  : @0x2000, obszar 0x1000 (struktura 392 B)
  * blok -> offset : 0x3000 + block*blocksize   (czyli BOOTBLOCK+SB_AREA + n*bs)
  * superblock CRC : crc32_be(0, sb[8:512], 504)   <-- ustalone empirycznie
  * inode #1       : katalog root
  * dir entry      : 32 B = de_inode(4) + de_size(1, =len nazwy; 0xff=longname) + name(27)
  * inode          : 128 B (patrz INODE struct)
  * root node      : 80 B = size(8) + ptr[16](64) + levels(1) + mode(1) + spare(6)

Tu zakładamy krótkie nazwy (<=27 B) — bez drzewa longfilename, co wystarcza dla logo.

Public API:
  QNX6Reader(path)     read an image (walk, read_inode_data, iter_inode_data, extract_to);
                       geometry and block trees are validated, a broken image raises QNX6Error.
  QNX6Writer(...)      build an image from a directory tree; write() is atomic (tmp + fsync + rename).
  validate_image(path) -> {'files': n, 'version': str}; walks the whole image (checksum, every
                       directory and block tree) and raises QNX6Error if it is broken or holds no files.
  QNX6Error            ValueError subclass for invalid / truncated / crafted images.
"""
import logging
import struct
import os
import stat

MAGIC = 0x68191122
BOOTBLOCK_SIZE = 0x2000
SUPERBLOCK_AREA = 0x1000
DIR_ENTRY_SIZE = 32
INODE_SIZE = 128
ROOTNODE_SIZE = 80
SHORT_NAME_MAX = 27
ROOT_INO = 1


def crc32_be(data, crc=0, poly=0x04C11DB7):
    """Big-endian CRC32 (non-reflected, init=0, no final xor) — checksum superbloku."""
    for byte in data:
        crc ^= byte << 24
        crc &= 0xFFFFFFFF
        for _ in range(8):
            crc = ((crc << 1) ^ (poly if (crc & 0x80000000) else 0)) & 0xFFFFFFFF
    return crc


def qnx6_lfile_checksum(name):
    """Suma kontrolna nazwy dla long-dirent (pole de_checksum).
    Odwzorowanie qnx6_lfile_checksum z jadra Linux (fs/qnx6): per bajt
    crc = ((crc>>1) + c) ^ (crc & 1 ? 0x80000000 : 0), warunek na STARYM crc.
    Przyklad: 'an_example_name_longer_than_27_bytes.PNG' -> 0x0e0dda93."""
    if isinstance(name, str):
        name = name.encode('utf-8')
    crc = 0
    for b in name:
        crc = (((crc >> 1) + b) ^ (0x80000000 if (crc & 1) else 0)) & 0xFFFFFFFF
    return crc


# ---------- struktury ----------

class RootNode:
    __slots__ = ('size', 'ptr', 'levels', 'mode')

    def __init__(self, raw):
        self.size = struct.unpack_from('<Q', raw, 0)[0]
        self.ptr = list(struct.unpack_from('<16I', raw, 8))
        self.levels = raw[72]
        self.mode = raw[73]


class Inode:
    __slots__ = ('size', 'uid', 'gid', 'ftime', 'mtime', 'atime', 'ctime',
                 'mode', 'ext_mode', 'block_ptr', 'filelevels', 'status')

    def __init__(self, raw):
        (self.size, self.uid, self.gid, self.ftime, self.mtime,
         self.atime, self.ctime, self.mode, self.ext_mode) = \
            struct.unpack_from('<QIIIIIIHH', raw, 0)
        self.block_ptr = list(struct.unpack_from('<16I', raw, 0x24))
        self.filelevels = raw[0x64]
        self.status = raw[0x65]

    def is_dir(self):
        return stat.S_ISDIR(self.mode)

    def is_reg(self):
        return stat.S_ISREG(self.mode)


# ---------- reader ----------

_log = logging.getLogger(__name__)

MAX_DEPTH = 64                     # directory nesting limit when walking an image (stock: 2 levels)
_BAD_NAME_CHARS = ('/', '\\', '\0', ':')


# Windows device names: "CON", "nul.png", "COM1.txt" ... open a device instead of a file there
_WIN_RESERVED = {'CON', 'PRN', 'AUX', 'NUL'} | {f'{p}{i}' for p in ('COM', 'LPT') for i in range(1, 10)}


def _safe_name(name):
    """A directory entry name that is a single, harmless path component on every OS:
    no separators / NUL / ':', not '.'/'..', no Windows device name (with any extension) and no
    trailing dot or space (Windows silently drops those, so two entries could collide)."""
    if not name or name in ('.', '..') or any(c in name for c in _BAD_NAME_CHARS):
        return False
    if name[-1] in '. ':
        return False
    return name.split('.', 1)[0].strip().upper() not in _WIN_RESERVED


def _contained(root, relpath):
    """realpath of root/relpath if it stays inside root (already a realpath), else None."""
    dst = os.path.realpath(os.path.join(root, relpath.lstrip('/')))
    try:
        if os.path.commonpath([root, dst]) != root or dst == root:
            return None
    except ValueError:
        return None
    return dst


class QNX6Error(ValueError):
    """The file is not a valid, complete QNX6 image (truncated, corrupt or crafted)."""


VALID_BLOCKSIZES = (512, 1024, 2048, 4096)
MAX_LEVELS = 3                     # indirection levels of a block tree (Linux qnx6: QNX6_PTR_MAX_LEVELS)
PTR_NONE = 0xFFFFFFFF              # "no block" in a pointer slot
_ICACHE_BLOCKS = 32                # inode-table blocks kept in memory (the table is read lazily)


class QNX6Reader:
    """Reads a QNX6 image. Every structure is checked against the superblock geometry before it is
    used, so a truncated or crafted image raises QNX6Error instead of returning short data, looping or
    eating memory: the file must hold all num_blocks blocks, pointers must stay below num_blocks,
    trees have at most MAX_LEVELS levels, a block appears only once per tree and a tree yields
    exactly ceil(size / blocksize) data blocks. The inode table is read block by block on demand."""
    _bad_name_logged = False        # warn once per reader about unsafe entry names

    def __init__(self, path):
        self.path = path
        self.f = open(path, 'rb')
        try:
            self.file_size = os.fstat(self.f.fileno()).st_size
            self._read_superblock()
            self._check_geometry()
            self._load_inode_table()
        except BaseException:
            self.f.close()          # an open handle blocks deleting/replacing the file on Windows
            raise

    def close(self):
        self.f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _read_superblock(self):
        # QNX6 jest power-safe: dwa superbloki (0x2000 i koniec-0x1000), aktywny = wyższy serial (tak montuje
        # fs-qnx6). Po zapisie przez system auta nowy stan bywa TYLKO w SB2.
        if self.file_size < BOOTBLOCK_SIZE + SUPERBLOCK_AREA:
            raise QNX6Error(f"{self.path}: {self.file_size} bytes is too small for a QNX6 image")
        self.f.seek(BOOTBLOCK_SIZE)
        sb = self.f.read(512)

        def valid(s):
            return (len(s) == 512 and struct.unpack_from('<I', s, 0)[0] == MAGIC and
                    struct.unpack_from('<I', s, 4)[0] == crc32_be(s[8:512]))

        end = self.file_size
        if end > BOOTBLOCK_SIZE + 2 * SUPERBLOCK_AREA:
            self.f.seek(end - SUPERBLOCK_AREA)
            sb2 = self.f.read(512)
            # the copy with the higher serial is the active one; a damaged SB1 falls back to a good SB2
            if valid(sb2) and (not valid(sb) or
                               struct.unpack_from('<Q', sb2, 8)[0] > struct.unpack_from('<Q', sb, 8)[0]):
                sb = sb2
        magic = struct.unpack_from('<I', sb, 0)[0]
        if magic != MAGIC:
            raise QNX6Error(f"bad magic {magic:#x}, expected {MAGIC:#x}: not a QNX6 image")
        stored = struct.unpack_from('<I', sb, 4)[0]
        calc = crc32_be(sb[8:512])
        self.checksum_ok = (stored == calc)
        self.serial = struct.unpack_from('<Q', sb, 8)[0]
        self.flags = struct.unpack_from('<I', sb, 0x18)[0]
        self.volumeid = sb[0x20:0x30]
        self.blocksize = struct.unpack_from('<I', sb, 0x30)[0]
        self.num_inodes = struct.unpack_from('<I', sb, 0x34)[0]
        self.free_inodes = struct.unpack_from('<I', sb, 0x38)[0]
        self.num_blocks = struct.unpack_from('<I', sb, 0x3c)[0]
        self.free_blocks = struct.unpack_from('<I', sb, 0x40)[0]
        self.allocgroup = struct.unpack_from('<I', sb, 0x44)[0]
        self.root_inode = RootNode(sb[0x48:0x48 + ROOTNODE_SIZE])
        self.root_bitmap = RootNode(sb[0x48 + 80:0x48 + 160])
        self.root_longfile = RootNode(sb[0x48 + 160:0x48 + 240])

    def _check_geometry(self):
        bs = self.blocksize
        if bs not in VALID_BLOCKSIZES:
            raise QNX6Error(f"unsupported block size {bs} (expected one of {VALID_BLOCKSIZES})")
        if self.num_blocks == 0 or self.num_inodes == 0:
            raise QNX6Error(f"empty file system ({self.num_blocks} blocks, {self.num_inodes} inodes)")
        self.data_bytes = self.num_blocks * bs
        need = BOOTBLOCK_SIZE + SUPERBLOCK_AREA + self.data_bytes
        if self.file_size < need:
            raise QNX6Error(f"image is truncated: {self.file_size} bytes, the file system needs "
                            f"at least {need} ({self.num_blocks} blocks of {bs} B)")
        if self.num_inodes * INODE_SIZE > self.data_bytes:
            raise QNX6Error(f"{self.num_inodes} inodes do not fit in {self.num_blocks} blocks")
        if not INODE_SIZE <= self.root_inode.size <= self.data_bytes:
            raise QNX6Error(f"inode table size {self.root_inode.size} out of range")

    def block_offset(self, b):
        return BOOTBLOCK_SIZE + SUPERBLOCK_AREA + b * self.blocksize

    def read_block(self, b):
        if not 0 <= b < self.num_blocks:
            raise QNX6Error(f"block {b} beyond the file system ({self.num_blocks} blocks)")
        self.f.seek(self.block_offset(b))
        data = self.f.read(self.blocksize)
        if len(data) != self.blocksize:
            raise QNX6Error(f"short read of block {b}: {len(data)} of {self.blocksize} bytes "
                            f"(image truncated?)")
        return data

    def tree_blocks(self, ptrs, levels, size, what='file'):
        """Data block numbers of a tree (root node or inode): exactly ceil(size/blocksize) of them,
        in file order. Raises QNX6Error on a bad level count, an out-of-range pointer, a block used
        twice (a loop) or missing blocks. Only indirect blocks are read."""
        bs = self.blocksize
        if levels > MAX_LEVELS:
            raise QNX6Error(f"{what}: {levels} indirection levels (max {MAX_LEVELS})")
        if size > self.data_bytes:
            raise QNX6Error(f"{what}: size {size} larger than the file system ({self.data_bytes} B)")
        need = (size + bs - 1) // bs
        out = []
        if need == 0:
            return out
        ppb = bs // 4
        fmt = '<%dI' % ppb
        seen = set()
        nb = self.num_blocks

        def descend(p, lv):
            if p >= nb:
                raise QNX6Error(f"{what}: block pointer {p} beyond the file system ({nb} blocks)")
            if p in seen:
                raise QNX6Error(f"{what}: block {p} referenced twice (corrupt or looping block tree)")
            seen.add(p)
            if lv == 0:
                out.append(p)
                return
            for q in struct.unpack(fmt, self.read_block(p)):
                if len(out) >= need:
                    return
                if q != PTR_NONE:
                    descend(q, lv - 1)

        for p in ptrs:
            if len(out) >= need:
                break
            if p != PTR_NONE:
                descend(p, levels)
        if len(out) < need:
            raise QNX6Error(f"{what}: only {len(out)} of {need} data blocks present")
        return out

    def _iter_tree(self, blocks, size):
        left = size
        for b in blocks:
            data = self.read_block(b)
            if left < len(data):
                data = data[:left]
            left -= len(data)
            yield data

    def _read_tree_data(self, ptrs, levels, size, what='file'):
        """Zwraca `size` bajtów danych z drzewa (root node lub inode)."""
        return b''.join(self._iter_tree(self.tree_blocks(ptrs, levels, size, what), size))

    def _load_inode_table(self):
        r = self.root_inode
        self._inode_blocks = self.tree_blocks(r.ptr, r.levels, r.size, 'inode table')
        self._inode_count = min(self.num_inodes, r.size // INODE_SIZE)
        self._icache = {}

    def get_inode(self, ino):
        """Inody numerowane od 1."""
        if not 1 <= ino <= self._inode_count:
            raise QNX6Error(f"inode {ino} outside the inode table (1..{self._inode_count})")
        off = (ino - 1) * INODE_SIZE
        idx, inner = divmod(off, self.blocksize)
        raw = self._icache.get(idx)
        if raw is None:
            if len(self._icache) >= _ICACHE_BLOCKS:
                self._icache.pop(next(iter(self._icache)))
            raw = self._icache[idx] = self.read_block(self._inode_blocks[idx])
        return Inode(raw[inner:inner + INODE_SIZE])

    def inode_blocks(self, inode, what='file'):
        """Validated data block list of an inode (see tree_blocks)."""
        return self.tree_blocks(inode.block_ptr, inode.filelevels, inode.size, what)

    def iter_inode_data(self, inode, what='file'):
        """The inode's data block by block (validated first, never more than inode.size bytes)."""
        return self._iter_tree(self.inode_blocks(inode, what), inode.size)

    def read_inode_data(self, inode, what='file'):
        return b''.join(self.iter_inode_data(inode, what))

    def read_dir(self, inode, what='directory'):
        """Zwraca [(name, child_ino), ...] dla katalogu."""
        data = self.read_inode_data(inode, what)
        out = []
        for off in range(0, len(data), DIR_ENTRY_SIZE):
            ent = data[off:off + DIR_ENTRY_SIZE]
            if len(ent) < DIR_ENTRY_SIZE:
                break
            child = struct.unpack_from('<I', ent, 0)[0]
            sz = ent[4]
            if child == 0 or sz == 0:
                continue
            if sz == 0xFF:          # long name (>27 B): entry index into the longfile tree
                name = self._long_name(struct.unpack_from('<I', ent, 8)[0])
                if name is None:
                    continue
            else:
                name = ent[5:5 + sz].decode('latin-1')
            if name in ('.', '..'):
                continue
            if not _safe_name(name):
                if not self._bad_name_logged:
                    self._bad_name_logged = True
                    _log.warning("QNX6 image %s: skipping unsafe directory entry name %r "
                                 "(further ones not logged)", getattr(self, 'path', '?'), name)
                continue
            out.append((name, child))
        return out

    def _long_name(self, idx):
        """Name number `idx` from the longfile tree (one entry per block: u16 length + name)."""
        if not hasattr(self, '_lf_blocks'):
            r = self.root_longfile
            self._lf_blocks = self.tree_blocks(r.ptr, r.levels, r.size, 'long name table') if r.size else []
        if idx >= len(self._lf_blocks):
            return None
        raw = self.read_block(self._lf_blocks[idx])
        n = struct.unpack_from('<H', raw, 0)[0]
        return raw[2:2 + n].decode('utf-8', 'replace')

    def walk(self, ino=ROOT_INO, prefix='', _seen=None, _depth=0):
        """Generator (path, inode) dla wszystkich plików/katalogów.
        A directory reached twice (loop / hard-linked dir in a crafted image) or nested deeper than
        MAX_DEPTH is skipped with a warning."""
        if _seen is None:
            _seen = {ino}
        inode = self.get_inode(ino)
        for name, child in self.read_dir(inode, prefix or '/'):
            cino = self.get_inode(child)
            path = prefix + '/' + name
            if cino.is_dir():
                if child in _seen or _depth + 1 > MAX_DEPTH:
                    _log.warning("QNX6 image %s: skipping directory %r (loop or too deep)",
                                 getattr(self, 'path', '?'), path)
                    continue
                _seen.add(child)
                yield (path, cino, 'dir')
                yield from self.walk(child, path, _seen, _depth + 1)
            else:
                yield (path, cino, 'file')

    def extract_to(self, outdir):
        """Extract every file under outdir; returns the number of files. A valid image cannot hold
        more file data than its own size, so more than that (e.g. one big file linked from many
        directory entries) raises QNX6Error before the disk fills up."""
        os.makedirs(outdir, exist_ok=True)
        root = os.path.realpath(outdir)
        nfiles = 0
        total = 0
        for path, inode, kind in self.walk():
            dst = _contained(root, path)
            if dst is None:
                _log.warning("QNX6 image %s: refusing to extract %r outside %s",
                             getattr(self, 'path', '?'), path, outdir)
                continue
            if kind == 'dir':
                os.makedirs(dst, exist_ok=True)
            else:
                total += inode.size
                if total > self.file_size:
                    raise QNX6Error(f"image holds more file data than its own size "
                                    f"({self.file_size} B): corrupt or crafted")
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with open(dst, 'wb') as g:
                    for chunk in self.iter_inode_data(inode, path):
                        g.write(chunk)
                nfiles += 1
        return nfiles


def validate_image(path):
    """Open and walk a whole QNX6 image without extracting it: superblock checksum, geometry, every
    directory and every file's block tree (pointers, levels, loops, sizes). File data blocks are not
    read (the file is known to hold all of them). Raises QNX6Error if anything is broken or the image
    holds no files; returns {'files': n, 'version': 'Version Number' from /Version.inf or ''}."""
    with QNX6Reader(path) as r:
        if not r.checksum_ok:
            raise QNX6Error(f"{path}: QNX6 superblock checksum mismatch")
        files = 0
        total = 0
        version = ''
        for p, inode, kind in r.walk():
            if kind != 'file':
                continue
            files += 1
            total += inode.size
            if total > r.file_size:
                raise QNX6Error(f"{path}: image holds more file data than its own size: corrupt or crafted")
            r.inode_blocks(inode, p)
            if p == '/Version.inf' and inode.size <= 65536:
                for line in r.read_inode_data(inode, p).decode('latin-1').splitlines():
                    if line.lower().startswith('version number') and '=' in line:
                        version = line.split('=', 1)[1].strip()
        if files == 0:
            raise QNX6Error(f"{path}: the image holds no files")
    return {'files': files, 'version': version}


# ---------- writer (mkfs) ----------

INODES_PER_BLOCK_DEFAULT = 8       # 1024/128
# Konwencja inode'ów ODCZYTANA z fabrycznego station_logos.img (Ford-stock).
# Katalogi z logo: mode 0o40775, ext_mode 0o2, status 3. Pliki PNG: 0o100664/0o1/3.
# Sterownik qnx6 Linuksa ignoruje ext_mode/status, ale Ford fs-qnx6 ich WYMAGA do
# wejścia w katalog — stąd wcześniej puste logo mimo poprawnej zawartości.
DIR_MODE = 0o040775
FILE_MODE = 0o100664
DIR_STATUS = 0x3
FILE_STATUS = 0x3
DIR_EXT_MODE = 0x2
FILE_EXT_MODE = 0x1


class _Obj:
    __slots__ = ('ino', 'name', 'parent', 'is_dir', 'src', 'size',
                 'children', 'mtime', 'path')

    def __init__(self, ino, name, parent, is_dir):
        self.ino = ino
        self.name = name
        self.parent = parent
        self.is_dir = is_dir
        self.src = None
        self.size = 0
        self.children = []
        self.mtime = 0
        self.path = '/'


class QNX6Writer:
    """Buduje obraz QNX6 z drzewa katalogów, replikując konwencje z obrazu-szablonu.

    template_img: istniejący obraz QNX6 (np. oryginał) z którego kopiujemy
                  bootblock, volumeid, flags, wersje i allocgroup — dla maksymalnej
                  zgodności z czytnikiem docelowym (Ford fs-qnx6.so).
    """

    def __init__(self, srcdir, template_img='station_logos.img',
                 image_size=None, build_time=None, num_blocks=None,
                 allocgroup=None):
        self.srcdir = srcdir.rstrip('/\\') or srcdir
        self.build_time = build_time if build_time is not None else 0x67ef1190
        self._req_image_size = image_size       # None => rozmiar pliku szablonu
        self._req_num_blocks = num_blocks        # None => z szablonu/rozmiaru
        self._force_allocgroup = allocgroup      # None => z szablonu

        # geometria/konwencje z szablonu (w tym blocksize!)
        self._load_template(template_img)

        # bufory wypełniane podczas budowy
        self.block_content = {}        # blocknum -> bytes(blocksize)
        self.next_block = 0            # mkqnx6fs używa bloku 0 (bitmapa) — nie rezerwujemy
        self.objs = []                 # lista _Obj wg ino-1
        self.file_srcblocks = {}       # ino -> (start_block, nblocks, src_path, size)

    def _load_template(self, template_img):
        with open(template_img, 'rb') as t:
            self.bootblock = t.read(BOOTBLOCK_SIZE)
            t.seek(BOOTBLOCK_SIZE)
            sb = t.read(512)
        # blocksize bierzemy Z SZABLONU (obrazy bywają 1024 lub 4096!)
        self.blocksize = struct.unpack_from('<I', sb, 0x30)[0]
        self.inodes_per_block = self.blocksize // INODE_SIZE
        self.ptr_per_block = self.blocksize // 4
        self.volumeid = sb[0x20:0x30]
        self.flags = struct.unpack_from('<I', sb, 0x18)[0]
        self.version1 = struct.unpack_from('<H', sb, 0x1c)[0]
        self.version2 = struct.unpack_from('<H', sb, 0x1e)[0]
        tmpl_ag = struct.unpack_from('<I', sb, 0x44)[0]
        self.allocgroup = self._force_allocgroup if self._force_allocgroup is not None else tmpl_ag
        self.template_num_blocks = struct.unpack_from('<I', sb, 0x3c)[0]
        # rozmiar obrazu: zadany albo = rozmiar pliku szablonu (round-trip)
        self.image_size = self._req_image_size if self._req_image_size is not None \
            else os.path.getsize(template_img)
        # liczba bloków: zadana, albo z szablonu (round-trip), albo dobrana do rozmiaru
        if self._req_num_blocks is not None:
            self.num_blocks = self._req_num_blocks
        elif self._req_image_size is None:
            self.num_blocks = self.template_num_blocks
        else:
            # zostaw miejsce na BOOTBLOCK + SB1 + SB2 (po 0x1000 kazdy superblok)
            usable = self.image_size - BOOTBLOCK_SIZE - 2 * SUPERBLOCK_AREA
            self.num_blocks = usable // self.blocksize
        # fs-qnx6 szuka DRUGIEGO superbloku w OSTATNIM bloku 0x1000 urzadzenia = (rozmiar - 0x1000),
        # a NIE na 0x3000+num_blocks*bs. Dla bs=4096 obie formuly sie zbiegaja; dla bs=1024 stara
        # dawala rozmiar-0x400 i fs-qnx6 zglaszal "Corrupted".
        self.sb2_offset = self.image_size - SUPERBLOCK_AREA
        self._load_template_meta(template_img)

    def _load_template_meta(self, template_img):
        """Mapa ścieżka -> (mode, ext_mode, status, ftime, mtime, atime, ctime)
        z obrazu-szablonu. Pozwala wiernie odtworzyć metadane inode'ów (zwłaszcza
        ext_mode/status katalogów), których wymaga Ford fs-qnx6, a które gubi
        ekstrakcja do zwykłego drzewa katalogów na Ubuntu."""
        self.template_meta = {}
        try:
            with QNX6Reader(template_img) as tr:
                root = tr.get_inode(ROOT_INO)
                self.template_meta['/'] = (root.mode, root.ext_mode, root.status,
                                           root.ftime, root.mtime, root.atime, root.ctime)
                for path, inode, kind in tr.walk():
                    self.template_meta[path] = (inode.mode, inode.ext_mode, inode.status,
                                                inode.ftime, inode.mtime, inode.atime, inode.ctime)
        except Exception:
            self.template_meta = {}

    # ---- alokacja bloków ----
    def _alloc(self, n):
        start = self.next_block
        self.next_block += n
        return start

    def _put_block(self, blocknum, data):
        assert len(data) <= self.blocksize
        self.block_content[blocknum] = data.ljust(self.blocksize, b'\x00')

    def _alloc_struct(self, ndata):
        """Alokuje strukturę systemową w kolejności [bloki pośrednie, POTEM dane] —
        jak mkqnx6fs: blok pośredni dostaje NIŻSZY numer niż jego bloki danych.
        Dzięki temu bitmapa (alokowana pierwsza) ma swój blok pośredni w bloku 0,
        a tablica inode ląduje zaraz za nią — układ wymagany przez Ford fs-qnx6
        (writer "inode-first" dawał chkqnx6fs: invalid blocks + bitmap inconsistent).
        Zwraca (root_ptr16, levels, data_blocks); wypełnia bloki pośrednie."""
        if ndata <= 16:
            data = [self._alloc(1) for _ in range(ndata)]
            return data + [PTR_NONE] * (16 - ndata), 0, data
        ppb = self.ptr_per_block
        # Policz liczbę bloków na każdym poziomie: counts[0]=dane, counts[k]=pośrednie poz. k.
        counts = [ndata]
        while counts[-1] > 16:
            counts.append((counts[-1] + ppb - 1) // ppb)
        levels = len(counts) - 1                       # 1 dla małych, 2 dla dużych (np. tablica inode 1 GB)
        # Bloki POŚREDNIE wszystkich poziomów NAJPIERW (niższe numery), POTEM dane —
        # zachowuje konwencję "pośredni < dane" (wymaganą przez fs-qnx6; bitmapa -> blok 0).
        ind = {}
        for lv in range(levels, 0, -1):
            ind[lv] = [self._alloc(1) for _ in range(counts[lv])]
        data = [self._alloc(1) for _ in range(ndata)]
        # Wypełnij: poziom 1 wskazuje na dane, poziom k na poziom k-1.
        lower = data
        for lv in range(1, levels + 1):
            for i, ib in enumerate(ind[lv]):
                chunk = lower[i * ppb:(i + 1) * ppb]
                padded = chunk + [PTR_NONE] * (ppb - len(chunk))
                self._put_block(ib, struct.pack('<%dI' % ppb, *padded))
            lower = ind[lv]
        top = ind[levels]
        return top + [PTR_NONE] * (16 - len(top)), levels, data

    def _build_indirect(self, data_blocks):
        """Buduje strukturę pośrednią. Zwraca (ptr16, levels).
        Alokuje bloki pośrednie i zapisuje ich zawartość."""
        if len(data_blocks) <= 16:
            ptr = list(data_blocks) + [PTR_NONE] * (16 - len(data_blocks))
            return ptr, 0
        level = 0
        cur = list(data_blocks)
        while len(cur) > 16:
            level += 1
            nxt = []
            for i in range(0, len(cur), self.ptr_per_block):
                chunk = cur[i:i + self.ptr_per_block]
                blk = self._alloc(1)
                padded = chunk + [PTR_NONE] * (self.ptr_per_block - len(chunk))
                self._put_block(blk, struct.pack('<%dI' % self.ptr_per_block, *padded))
                nxt.append(blk)
            cur = nxt
        ptr = list(cur) + [PTR_NONE] * (16 - len(cur))
        return ptr, level

    # ---- skan drzewa, przydział ino ----
    def _scan(self):
        root = _Obj(1, '', -1, True)
        root.path = '/'
        self.objs = [root]
        next_ino = [2]

        def walk(parent_obj, path):
            entries = sorted(os.listdir(path))
            for name in entries:
                full = os.path.join(path, name)
                isdir = os.path.isdir(full)
                ino = next_ino[0]; next_ino[0] += 1
                o = _Obj(ino, name, parent_obj.ino, isdir)
                o.path = (parent_obj.path.rstrip('/') + '/' + name)
                self.objs.append(o)
                parent_obj.children.append(ino)
                if isdir:
                    walk(o, full)
                else:
                    st = os.stat(full)
                    o.src = full
                    o.size = st.st_size
                    o.mtime = int(st.st_mtime)
        walk(root, self.srcdir)
        self.num_objects = len(self.objs)
        # num_inodes: jak mkqnx6fs = max(potrzebne, num_blocks/8), zaokrąglone w górę
        # do pełnego bloku inode'ów. mkqnx6fs domyślnie rezerwuje ~num_blocks/8 inodów.
        ipb = self.inodes_per_block
        need = ((self.num_objects + ipb - 1) // ipb) * ipb
        default_min = ((self.num_blocks // 8 + ipb - 1) // ipb) * ipb
        # Ograniczenie: utrzymaj tablicę inode w levels=1 (<= 16*ptr_per_block bloków),
        # by budowa dużych partycji (np. 1 GiB) nie tworzyła 16 MiB tablicy i nie
        # wymuszała levels=2 przy montażu. 16*ppb*ipb inodów to i tak nadmiar dla modów
        # (bs1024: 32768). Writer obsługuje levels=2 (patrz _alloc_struct) jako rezerwę.
        cap = 16 * self.ptr_per_block * ipb
        self.num_inodes = min(max(need, default_min, ipb), max(need, cap))

    # ---- budowa ----
    def build(self):
        self._scan()
        bs = self.blocksize

        # Kolejność jak mkqnx6fs/fabryka: NAJPIERW bitmapa (blok 0 = jej blok pośredni),
        # POTEM tablica inode, POTEM dane plików. Odwrotna kolejność (inode-first)
        # dawała "Corrupted file system detected" na realnym Foncie mimo poprawnej treści.
        inode_table_blocks = self.num_inodes // self.inodes_per_block
        bitmap_bytes = (self.num_blocks + 7) // 8
        bitmap_blocks_cnt = (bitmap_bytes + bs - 1) // bs

        # 1) bitmapa — pierwsza (blok 0 = bitmapa, jak mkqnx6fs)
        bitmap_root_ptr, bitmap_levels, bitmap_data_blocks = self._alloc_struct(bitmap_blocks_cnt)
        # 2) tablica inode — zaraz za bitmapą
        inode_root_ptr, inode_levels, inode_data_blocks = self._alloc_struct(inode_table_blocks)
        # 3) REZERWA gen-2 (Power-Safe COW): mkqnx6fs rezerwuje DRUGI komplet bloków
        # metadanych (równy gen-1) — puste, ale oznaczone USED — i dopiero ZA nimi
        # umieszcza dane. Bez tego fs-qnx6 uznaje obraz za niespójny.
        meta_blocks = self.next_block            # bitmapa + tablica inode (gen-1)
        self.gen2_reserve = self._alloc(meta_blocks)   # gen-2: bloki puste, zarezerwowane

        # 2b) DRZEWO LONGFILE (nazwy > 27 bajtow). Format qnx6 (zgodny z fs/qnx6 jadra Linux):
        #   - long-dirent (32B): de_inode(4) de_size=0xff(1) unk[3] de_long_inode(4) de_checksum(4)+0
        #   - de_long_inode = LOGICZNY indeks bloku w pliku longfile (0,1,2,...)
        #   - kazdy blok pliku longfile = jeden wpis: lf_size(u16) + nazwa (do 510 B)
        #   - RootNode Longfile w superbloku (@0x48+160) = (n_wpisow*bs, ptr, levels)
        self.long_blockidx = {}   # ino -> logiczny indeks bloku longfile
        long_names = []           # nazwy (bytes) w kolejnosci indeksow
        for o in self.objs:
            nb = o.name.encode('utf-8')
            if len(nb) > SHORT_NAME_MAX:
                self.long_blockidx[o.ino] = len(long_names)
                long_names.append(nb)
        if long_names:
            lf_data_blocks = []
            for nb in long_names:
                if len(nb) > 510:
                    raise QNX6Error("names longer than 510 bytes are not supported: " + nb.decode('utf-8', 'replace'))
                blk = self._alloc(1)
                self._put_block(blk, struct.pack('<H', len(nb)) + nb)
                lf_data_blocks.append(blk)
            lf_ptr, lf_levels = self._build_indirect(lf_data_blocks)
            self.longfile_root = (len(long_names) * bs, lf_ptr, lf_levels)
        else:
            self.longfile_root = (0, [PTR_NONE] * 16, 0)

        # 3) dane katalogów i plików -> budowa inode entries
        inode_entries = {}   # ino -> bytes(128)

        # najpierw katalogi (potrzebują ino dzieci — już znane), potem pliki
        for o in self.objs:
            if o.is_dir:
                entries = bytearray()
                # '.' i '..'
                entries += self._dir_entry(o.ino, '.')
                parent = o.parent if o.parent != -1 else o.ino
                entries += self._dir_entry(parent, '..')
                for cino in o.children:
                    c = self.objs[cino - 1]
                    entries += self._dir_entry(c.ino, c.name)
                nblocks = max(1, (len(entries) + bs - 1) // bs)
                start = self._alloc(nblocks)
                blks = list(range(start, start + nblocks))
                buf = bytes(entries).ljust(nblocks * bs, b'\x00')
                for i, b in enumerate(blks):
                    self._put_block(b, buf[i * bs:(i + 1) * bs])
                ptr, levels = self._build_indirect(blks)
                mode, ext_mode, status, ftime, mtime, atime, ctime = self._meta_for(o, True)
                inode_entries[o.ino] = self._make_inode(
                    size=nblocks * bs, mode=mode, ext_mode=ext_mode,
                    status=status, ptr=ptr, levels=levels,
                    mtime=mtime, ftime=ftime, atime=atime, ctime=ctime)
            else:
                size = o.size
                nblocks = (size + bs - 1) // bs
                if nblocks == 0:
                    ptr = [PTR_NONE] * 16; levels = 0; start = None
                else:
                    start = self._alloc(nblocks)
                ptr_blocks = list(range(start, start + nblocks)) if nblocks else []
                ptr, levels = self._build_indirect(ptr_blocks)
                if nblocks:
                    self.file_srcblocks[o.ino] = (start, nblocks, o.src, size)
                mode, ext_mode, status, ftime, mtime, atime, ctime = self._meta_for(o, False)
                inode_entries[o.ino] = self._make_inode(
                    size=size, mode=mode, ext_mode=ext_mode,
                    status=status, ptr=ptr, levels=levels,
                    mtime=mtime, ftime=ftime, atime=atime, ctime=ctime)

        # 4) zapis tablicy inode do zarezerwowanych bloków
        ipb = self.inodes_per_block
        empty = b'\x00' * INODE_SIZE
        table = bytearray()
        for ino in range(1, self.num_inodes + 1):
            table += inode_entries.get(ino, empty)
        for i, b in enumerate(inode_data_blocks):
            self._put_block(b, bytes(table[i * bs:(i + 1) * bs]))

        used_blocks = self.next_block
        if used_blocks > self.num_blocks:
            raise QNX6Error(f"too many files for the image: {used_blocks} blocks needed > "
                            f"{self.num_blocks} available (increase image_size)")
        self.free_blocks = self.num_blocks - used_blocks
        self.free_inodes = self.num_inodes - self.num_objects

        # 5) bitmapa: bity 0..used_blocks-1 = USED (1), bity 0..num_blocks-1 wg użycia.
        # Jak mkqnx6fs: WSZYSTKIE bity POZA num_blocks (dopełnienie) ustawione na 1,
        # a bajty poza rozmiarem bitmapy = 0xFF.
        bitmap_bytes = (self.num_blocks + 7) // 8
        bm = bytearray(b'\xff' * (bitmap_blocks_cnt * bs))   # domyślnie 0xFF (rezerwa/padding)
        for b in range(self.num_blocks):                     # realny zakres: ustaw wg użycia
            if b < used_blocks:
                bm[b >> 3] |= (1 << (b & 7))
            else:
                bm[b >> 3] &= ~(1 << (b & 7)) & 0xFF
        for i, b in enumerate(bitmap_data_blocks):
            self._put_block(b, bytes(bm[i * bs:(i + 1) * bs]))

        # 6) superblok
        self.inode_root = (self.num_inodes * INODE_SIZE, inode_root_ptr, inode_levels)
        self.bitmap_root = (bitmap_bytes, bitmap_root_ptr, bitmap_levels)
        return self._assemble()

    def _dir_entry(self, ino, name):
        nb = name.encode('utf-8')
        if len(nb) <= SHORT_NAME_MAX:
            return struct.pack('<IB', ino, len(nb)) + nb.ljust(SHORT_NAME_MAX, b'\x00')
        # long name: de_size=0xff, de_long_inode = logiczny indeks bloku longfile, de_checksum
        idx = self.long_blockidx[ino]
        chk = qnx6_lfile_checksum(nb)
        # <I B 3x I I> = de_inode, 0xff, 3 pad, de_long_inode, de_checksum = 16 B, reszta 0
        return struct.pack('<IB3xII', ino, 0xff, idx, chk) + b'\x00' * 16

    def _make_inode(self, size, mode, ext_mode, status, ptr, levels, mtime,
                    ftime=None, atime=None, ctime=None):
        ftime = self.build_time if ftime is None else ftime
        atime = mtime if atime is None else atime
        ctime = self.build_time if ctime is None else ctime
        return struct.pack(
            '<QIIIIIIHH16IBBH6I',
            size, 0, 0,                       # size, uid, gid
            ftime, mtime, atime, ctime,
            mode, ext_mode,
            *ptr,
            levels, status, 0,                # filelevels, status, unknown2(H)
            0, 0, 0, 0, 0, 0)                 # zero2[6]

    def _meta_for(self, obj, is_dir):
        """Zwraca (mode, ext_mode, status, ftime, mtime, atime, ctime) dla obiektu —
        z szablonu jeśli ścieżka istniała, inaczej wg konwencji fabrycznej."""
        m = self.template_meta.get(obj.path)
        if m is not None:
            return m
        if is_dir:
            return (DIR_MODE, DIR_EXT_MODE, DIR_STATUS, None, self.build_time, None, None)
        # Zachowaj bit wykonywalny ze zrodla (binaria/skrypty modow, np. FMods
        # instutility, usb_installer.sh, launch_fmodslauncher.sh musza byc +x,
        # bo startup_gf.sh uruchamia je bezposrednio). ext_mode/status zostaja
        # fabryczne (wymaga ich Ford fs-qnx6). Pliki bez +x -> stock 0o100664.
        mode = FILE_MODE
        try:
            if obj.src and (os.stat(obj.src).st_mode & 0o111):
                mode |= 0o111
        except OSError:
            pass
        return (mode, FILE_EXT_MODE, FILE_STATUS, None, obj.mtime, None, None)

    def _root_node(self, root):
        size, ptr, levels = root
        return struct.pack('<Q16IBB6s', size, *ptr, levels, 0x1, b'\x00' * 6)

    def _superblock(self, serial):
        sb = bytearray(512)
        struct.pack_into('<I', sb, 0x00, MAGIC)
        # checksum @0x04 wpiszemy po policzeniu
        struct.pack_into('<Q', sb, 0x08, serial)
        struct.pack_into('<I', sb, 0x10, self.build_time)   # ctime
        struct.pack_into('<I', sb, 0x14, self.build_time)   # atime
        struct.pack_into('<I', sb, 0x18, self.flags)
        struct.pack_into('<H', sb, 0x1c, self.version1)
        struct.pack_into('<H', sb, 0x1e, self.version2)
        sb[0x20:0x30] = self.volumeid
        struct.pack_into('<I', sb, 0x30, self.blocksize)
        struct.pack_into('<I', sb, 0x34, self.num_inodes)
        struct.pack_into('<I', sb, 0x38, self.free_inodes)
        struct.pack_into('<I', sb, 0x3c, self.num_blocks)
        struct.pack_into('<I', sb, 0x40, self.free_blocks)
        struct.pack_into('<I', sb, 0x44, self.allocgroup)
        off = 0x48
        sb[off:off + 80] = self._root_node(self.inode_root); off += 80
        sb[off:off + 80] = self._root_node(self.bitmap_root); off += 80
        # Longfile (drzewo dlugich nazw; pusty gdy brak) i Unknown (pusty)
        empty_root = (0, [PTR_NONE] * 16, 0)
        sb[off:off + 80] = self._root_node(getattr(self, 'longfile_root', empty_root)); off += 80
        sb[off:off + 80] = self._root_node(empty_root); off += 80
        # checksum
        ck = crc32_be(bytes(sb[8:512]))
        struct.pack_into('<I', sb, 0x04, ck)
        return bytes(sb)

    def _assemble(self):
        img = bytearray(self.image_size)
        # bootblock
        img[0:BOOTBLOCK_SIZE] = self.bootblock
        # superblok #1
        sb1 = self._superblock(serial=2)
        img[BOOTBLOCK_SIZE:BOOTBLOCK_SIZE + 512] = sb1
        # bloki danych
        base = BOOTBLOCK_SIZE + SUPERBLOCK_AREA
        for b, data in self.block_content.items():
            off = base + b * self.blocksize
            img[off:off + self.blocksize] = data
        # bloki plików — kopiujemy zawartość ze źródła
        for ino, (start, nblocks, src, size) in self.file_srcblocks.items():
            off = base + start * self.blocksize
            with open(src, 'rb') as fsrc:
                data = fsrc.read()
            img[off:off + len(data)] = data
        # superblok #2 (kopia, niższy serial)
        sb2 = self._superblock(serial=1)
        img[self.sb2_offset:self.sb2_offset + 512] = sb2
        return bytes(img)

    def write(self, out_path):
        """Build and write the image atomically: <out>.tmp, fsync, then rename over out_path, so a
        crash or a full disk never leaves a partial image under the final name."""
        data = self.build()
        tmp = out_path + '.tmp'
        try:
            with open(tmp, 'wb') as g:
                g.write(data)
                g.flush()
                os.fsync(g.fileno())
            os.replace(tmp, out_path)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        return {
            'objects': self.num_objects, 'inodes': self.num_inodes,
            'free_inodes': self.free_inodes, 'num_blocks': self.num_blocks,
            'used_blocks': self.next_block, 'free_blocks': self.free_blocks,
            'size': len(data),
        }


if __name__ == '__main__':
    import sys
    img = sys.argv[1] if len(sys.argv) > 1 else 'station_logos.img'
    r = QNX6Reader(img)
    print(f"magic OK, checksum_ok={r.checksum_ok}, serial={r.serial}, "
          f"bs={r.blocksize}, inodes={r.num_inodes} (free {r.free_inodes}), "
          f"blocks={r.num_blocks} (free {r.free_blocks}), allocgroup={r.allocgroup}, "
          f"flags={r.flags:#x}")
    dirs = files = 0
    total = 0
    samples = []
    for path, inode, kind in r.walk():
        if kind == 'dir':
            dirs += 1
        else:
            files += 1
            total += inode.size
            if len(samples) < 5:
                samples.append((path, inode.size))
    print(f"dirs={dirs}, files={files}, total size={total} B")
    for p, s in samples:
        print(f"   {p}  ({s} B)")
