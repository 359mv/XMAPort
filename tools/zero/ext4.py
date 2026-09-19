"""Pure-Python ext4 volume reader (on-disk format based implementation).

Public surface: Volume, Inode, BlockReader, MappingEntry, InodeType,
Ext4Error / EndOfStreamError / MagicError.

Supported data layouts: extent trees (incl. uninitialized extents), classic
direct/singly/doubly/triply indirect blocks, inline data, fast symlinks.
"""
from bisect import bisect_right
from functools import cmp_to_key
import io
from math import log as log_math

_MAGIC_E2FS = 0xEF53
_MAGIC_EXTENT = 0xF30A
_MAGIC_XATTR = 0xEA020000
_MAX_UNINIT_EXTENT_LEN = 32768

_FLAG_INCOMPAT_FILETYPE = 0x2
_FLAG_INCOMPAT_64BIT = 0x80
_FLAG_EXTENTS = 0x80000
_FLAG_EA_INODE = 0x200000
_FLAG_INLINE = 0x10000000
_FLAG_ENCRYPT = 0x800

_FT_UNKNOWN, FT_REG, FT_DIR, FT_CHR, FT_BLK, FT_FIFO, FT_SOCK, FT_LNK = 0, 1, 2, 3, 4, 5, 6, 7
_FT_CHECKSUM = 0xDE

_S_IFMT = 0o170000
_S_IFDIR, _S_IFREG, _S_IFLNK = 0o040000, 0o100000, 0o120000

class Ext4Error(Exception):
    ...

class EndOfStreamError(Ext4Error):
    ...

class MagicError(Ext4Error):
    ...

def _u16(raw: bytes, off: int) -> int:
    return raw[off] | (raw[off + 1] << 8)

def _u32(raw: bytes, off: int) -> int:
    return raw[off] | (raw[off + 1] << 8) | (raw[off + 2] << 16) | (raw[off + 3] << 24)

class InodeType:
    UNKNOWN = 0x0
    FILE = 0x1
    DIRECTORY = 0x2
    CHARACTER_DEVICE = 0x3
    BLOCK_DEVICE = 0x4
    FIFO = 0x5
    SOCKET = 0x6
    SYMBOLIC_LINK = 0x7
    CHECKSUM = 0xDE

class MappingEntry:
    def __init__(self, file_block_idx, disk_block_idx, block_count=1):
        self.file_block_idx = file_block_idx
        self.disk_block_idx = disk_block_idx
        self.block_count = block_count

    def __iter__(self):
        yield self.file_block_idx
        yield self.disk_block_idx
        yield self.block_count

    def __repr__(self):
        return f"{type(self).__name__:s}({self.file_block_idx!r:s}, {self.disk_block_idx!r:s}, {self.block_count!r:s})"

    def copy(self):
        return MappingEntry(self.file_block_idx, self.disk_block_idx, self.block_count)

    @staticmethod
    def create_mapping(*entries):
        file_block_idx = 0
        result = []
        for disk_block_idx, block_count in entries:
            result.append(MappingEntry(file_block_idx, disk_block_idx, block_count))
            file_block_idx += block_count
        return result

    @staticmethod
    def optimize(entries):
        entries.sort(key=lambda entry: entry.file_block_idx)
        idx = 0
        while idx < len(entries):
            while idx + 1 < len(entries) \
                    and entries[idx].file_block_idx + entries[idx].block_count == entries[idx + 1].file_block_idx \
                    and entries[idx].disk_block_idx + entries[idx].block_count == entries[idx + 1].disk_block_idx:
                merged = entries.pop(idx + 1)
                entries[idx].block_count += merged.block_count
            idx += 1

class Superblock:
    """Parsed superblock fields (offsets per the ext4 spec)."""

    def __init__(self, raw: bytes):
        self.s_magic = _u16(raw, 0x38)
        self.s_inodes_count = _u32(raw, 0x00)
        self.s_blocks_count = _u32(raw, 0x04) | (_u32(raw, 0x150) << 32)
        self.s_free_blocks_count = _u32(raw, 0x0C) | (_u32(raw, 0x158) << 32)
        self.s_free_inodes_count = _u32(raw, 0x10)
        self.s_first_data_block = _u32(raw, 0x14)
        self.s_log_block_size = _u32(raw, 0x18)
        self.s_blocks_per_group = _u32(raw, 0x20)
        self.s_inodes_per_group = _u32(raw, 0x28)
        self.s_rev_level = _u32(raw, 0x4C)
        self.s_first_ino = _u32(raw, 0x54) if self.s_rev_level >= 1 else 11
        self.s_inode_size = _u16(raw, 0x58) if self.s_rev_level >= 1 else 128
        self.s_feature_compat = _u32(raw, 0x5C)
        self.s_feature_incompat = _u32(raw, 0x60)
        self.s_feature_ro_compat = _u32(raw, 0x64)
        self.s_uuid = raw[0x68:0x78]
        self.s_volume_name = raw[0x78:0x88].split(b"\x00", 1)[0]
        self.s_last_mounted = raw[0x88:0xC8].split(b"\x00", 1)[0]
        self.s_mtime = _u32(raw, 0x2C)
        self.s_mkfs_time = _u32(raw, 0x108)
        self.s_reserved_gdt_blocks = _u16(raw, 0xCE)
        self.s_state = _u16(raw, 0x3A)
        self.s_blocks_per_group = _u32(raw, 0x20)
        self.s_clusters_per_group = _u32(raw, 0x24)

        desc = _u16(raw, 0xFE) if self.s_rev_level >= 1 else 0
        if desc == 0:
            desc = 0x20
        if (self.s_feature_incompat & _FLAG_INCOMPAT_64BIT) and desc < 0x40:
            desc = 0x40
        self.s_desc_size = desc
        self.block_size = 1 << (10 + self.s_log_block_size)

class _GroupDescriptor:
    __slots__ = ("block_bitmap", "inode_bitmap", "inode_table")

    def __init__(self, raw: bytes, desc_size: int):
        self.block_bitmap = _u32(raw, 0)
        self.inode_bitmap = _u32(raw, 4)
        self.inode_table = _u32(raw, 8)
        if desc_size >= 0x40:
            self.block_bitmap |= _u32(raw, 32) << 32
            self.inode_bitmap |= _u32(raw, 36) << 32
            self.inode_table |= _u32(raw, 40) << 32

class _RawInode:
    """Field access over the 128+ byte inode record."""

    def __init__(self, raw: bytes):
        self.raw = raw
        self.i_mode = _u16(raw, 0)
        self.i_uid = _u16(raw, 2) | (_u16(raw, 120) << 16)
        self.i_gid = _u16(raw, 24) | (_u16(raw, 122) << 16)
        self.i_size = _u32(raw, 4) | (_u32(raw, 108) << 32)
        self.i_atime = _u32(raw, 8)
        self.i_ctime = _u32(raw, 12)
        self.i_mtime = _u32(raw, 16)
        self.i_links_count = _u16(raw, 26)
        self.i_blocks = _u32(raw, 28)
        self.i_flags = _u32(raw, 32)
        self.i_block = raw[40:100]
        self.i_file_acl = _u32(raw, 104) | (_u16(raw, 118) << 32)
        self.i_extra_isize = _u16(raw, 128) if len(raw) > 130 else 0

class Volume:
    ROOT_INODE = 2

    def __init__(self, stream, offset=0, ignore_flags=False, ignore_magic=False):
        self.ignore_flags = ignore_flags
        self.ignore_magic = ignore_magic
        self.offset = offset
        self.stream = stream

        raw = self.read(0x400, 0x400)
        if len(raw) < 0x400:
            raise EndOfStreamError("superblock extends past end of stream")
        self.superblock = Superblock(raw)
        if not ignore_magic and self.superblock.s_magic != _MAGIC_E2FS:
            raise MagicError(
                f"Invalid magic value in superblock: 0x{self.superblock.s_magic:04X} (expected 0x{_MAGIC_E2FS:04X})")
        if self.superblock.s_inode_size == 0:
            raise Ext4Error("corrupt superblock: inode size is zero")

        sb = self.superblock
        if sb.s_inodes_per_group == 0:
            raise Ext4Error("corrupt superblock: inodes per group is zero")
        groups_by_blocks = max(0, -(-(sb.s_blocks_count - sb.s_first_data_block) // sb.s_blocks_per_group))
        groups_by_inodes = max(0, -(-sb.s_inodes_count // sb.s_inodes_per_group))
        group_count = max(groups_by_blocks, groups_by_inodes, 1)

        gdt_offset = (sb.s_first_data_block + 1) * sb.block_size
        gdt_raw = self.read(gdt_offset, group_count * sb.s_desc_size)
        if len(gdt_raw) < group_count * sb.s_desc_size:
            raise EndOfStreamError("group descriptor table extends past end of stream")
        self.group_descriptors = [
            _GroupDescriptor(gdt_raw[i * sb.s_desc_size:(i + 1) * sb.s_desc_size], sb.s_desc_size)
            for i in range(group_count)
        ]

    def __repr__(self):
        return (f"{type(self).__name__:s}(volume_name = {self.superblock.s_volume_name!r:s}, "
                f"uuid = {self.uuid!r:s}, last_mounted = {self.superblock.s_last_mounted!r:s})")

    @property
    def block_size(self):
        return self.superblock.block_size

    @property
    def get_block_count(self):
        return self.superblock.s_blocks_count

    @property
    def get_mount_point(self):
        return self.superblock.s_last_mounted.decode()

    @property
    def get_free_blocks_count(self):
        return self.superblock.s_free_blocks_count

    @property
    def get_info_list(self):
        sb = self.superblock
        return [
            ['Magic number', hex(sb.s_magic).upper()],
            ["Volume name", sb.s_volume_name.decode()],
            ["UUID", self.uuid],
            ['Last mounted on', sb.s_last_mounted.decode()],
            ["Block size", sb.block_size],
            ["Block count", sb.s_blocks_count],
            ["Free inodes", sb.s_free_inodes_count],
            ["Free blocks", sb.s_free_blocks_count],
            ["Inodes per group", sb.s_inodes_per_group],
            ['Blocks per group', sb.s_blocks_per_group],
            ['Inode count', sb.s_inodes_count],
            ['Reserved GDT blocks', sb.s_reserved_gdt_blocks],
            ["Inode size", sb.s_inode_size],
            ['Filesystem created', sb.s_mkfs_time],
            ["Current Size", self.get_block_count * self.block_size]]

    def get_inode_group(self, inode_idx):
        per_group = self.superblock.s_inodes_per_group
        return (inode_idx - 1) // per_group, (inode_idx - 1) % per_group

    def get_inode(self, inode_idx, file_type=InodeType.UNKNOWN):
        if inode_idx < 1 or inode_idx > self.superblock.s_inodes_count:
            raise Ext4Error(f"inode number out of range: {inode_idx:d}")
        group_idx, table_entry = self.get_inode_group(inode_idx)
        if group_idx >= len(self.group_descriptors):
            raise Ext4Error(f"inode {inode_idx:d} points outside the group descriptor table")
        desc = self.group_descriptors[group_idx]
        inode_offset = desc.inode_table * self.block_size + table_entry * self.superblock.s_inode_size
        return Inode(self, inode_offset, inode_idx, file_type)

    def read(self, offset, byte_len):
        if self.offset + offset != self.stream.tell():
            self.stream.seek(self.offset + offset, io.SEEK_SET)
        return self.stream.read(byte_len)

    @property
    def root(self):
        return self.get_inode(Volume.ROOT_INODE, InodeType.DIRECTORY)

    @property
    def uuid(self):
        parts = (self.superblock.s_uuid[:4], self.superblock.s_uuid[4:6],
                 self.superblock.s_uuid[6:8], self.superblock.s_uuid[8:10], self.superblock.s_uuid[10:])
        return "-".join("".join(f"{c:02X}" for c in part) for part in parts)

_XATTR_PREFIXES = {
    0: "",
    1: "user.",
    2: "system.posix_acl_access",
    3: "system.posix_acl_default",
    4: "trusted.",
    6: "security.",
    7: "system.",
    8: "system.richacl",
}

def _wcs_cmp(str_a, str_b):
    for a, b in zip(str_a, str_b):
        diff = ord(a) - ord(b)
        if diff != 0:
            return -1 if diff < 0 else 1
    diff = len(str_a) - len(str_b)
    return -1 if diff < 0 else 1 if diff > 0 else 0

def _dir_entry_comparator(dir_a, dir_b):
    file_name_a, _, file_type_a = dir_a
    file_name_b, _, file_type_b = dir_b
    if file_type_a == InodeType.DIRECTORY == file_type_b or file_type_a != InodeType.DIRECTORY != file_type_b:
        first = _wcs_cmp(file_name_a.lower(), file_name_b.lower())
        return first if first != 0 else _wcs_cmp(file_name_a, file_name_b)
    return -1 if file_type_a == InodeType.DIRECTORY else 1

class Inode:
    def __init__(self, volume, offset, inode_idx, file_type=InodeType.UNKNOWN):
        self.inode_idx = inode_idx
        self.offset = offset
        self.volume = volume
        self.file_type = file_type
        size = volume.superblock.s_inode_size
        raw = volume.read(offset, size)
        if len(raw) < size:
            raise EndOfStreamError(f"inode {inode_idx:d} extends past end of stream")
        self.inode = _RawInode(raw)

    def __len__(self):
        return self.inode.i_size

    def __repr__(self):
        if self.inode_idx is not None:
            return (f"{type(self).__name__:s}(inode_idx = {self.inode_idx!r:s}, "
                    f"offset = 0x{self.offset:X}, volume_uuid = {self.volume.uuid!r:s})")
        return f"{type(self).__name__:s}(offset = 0x{self.offset:X}, volume_uuid = {self.volume.uuid!r:s})"

    @staticmethod
    def directory_entry_comparator(dir_a, dir_b):
        return _dir_entry_comparator(dir_a, dir_b)

    directory_entry_key = cmp_to_key(_dir_entry_comparator)

    def get_inode(self, *relative_path, decode_name=None):
        if not self.is_dir:
            raise Ext4Error(f"Inode {self.inode_idx:d} is not a directory.")
        current = self
        for i, part in enumerate(relative_path):
            if not self.volume.ignore_flags and not current.is_dir:
                raise Ext4Error(f"{'/'.join(relative_path[:i])!r:s} (Inode {current.inode_idx:d}) is not a directory.")
            found = next((entry for entry in current.open_dir(decode_name) if entry[0] == part), None)
            if found is None:
                raise FileNotFoundError(
                    f"{part!r:s} not found in {'/'.join(relative_path[:i])!r:s} (Inode {current.inode_idx:d}).")
            current = current.volume.get_inode(found[1], found[2])
        return current

    @property
    def is_dir(self):
        if (self.volume.superblock.s_feature_incompat & _FLAG_INCOMPAT_FILETYPE) == 0:
            return (self.inode.i_mode & _S_IFMT) == _S_IFDIR
        return self.file_type == InodeType.DIRECTORY

    @property
    def is_file(self):
        if (self.volume.superblock.s_feature_incompat & _FLAG_INCOMPAT_FILETYPE) == 0:
            return (self.inode.i_mode & _S_IFMT) == _S_IFREG
        return self.file_type == InodeType.FILE

    @property
    def is_symlink(self):
        if (self.volume.superblock.s_feature_incompat & _FLAG_INCOMPAT_FILETYPE) == 0:
            return (self.inode.i_mode & _S_IFMT) == _S_IFLNK
        return self.file_type == InodeType.SYMBOLIC_LINK

    @property
    def is_in_use(self):
        group_idx, bitmap_bit = self.volume.get_inode_group(self.inode_idx)
        desc = self.volume.group_descriptors[group_idx]
        byte = self.volume.read(desc.inode_bitmap * self.volume.block_size + bitmap_bit // 8, 1)[0]
        return ((byte >> (7 - bitmap_bit % 8)) & 1) != 0

    def _is_fast_symlink(self):
        if not self.is_symlink or (self.inode.i_flags & (_FLAG_EXTENTS | _FLAG_INLINE)) != 0:
            return False
        acl_sectors = self.volume.block_size // 512 if self.inode.i_file_acl != 0 else 0
        return self.inode.i_size <= 60 and self.inode.i_blocks == acl_sectors

    @property
    def mode_str(self):
        mode = self.inode.i_mode
        if (self.volume.superblock.s_feature_incompat & _FLAG_INCOMPAT_FILETYPE) == 0:
            kind = mode & _S_IFMT
            device_type = {_S_IFDIR: "d", _S_IFREG: "-", _S_IFLNK: "l",
                           0o020000: "c", 0o060000: "b", 0o010000: "p", 0o140000: "s"}.get(kind, "?")
        else:
            device_type = {
                InodeType.FILE: "-", InodeType.DIRECTORY: "d", InodeType.CHARACTER_DEVICE: "c",
                InodeType.BLOCK_DEVICE: "b", InodeType.FIFO: "p", InodeType.SOCKET: "s",
                InodeType.SYMBOLIC_LINK: "l"}.get(self.file_type, "?")

        def special(letter, execute, special_bit):
            if special_bit:
                return letter if execute else letter.upper()
            return "x" if execute else "-"

        rwx = "rwx"
        masks = ((0o400, 0o200, 0o100), (0o040, 0o020, 0o010), (0o004, 0o002, 0o001))
        bits = ["".join(letter if mode & mask else "-" for letter, mask in zip(rwx, group)) for group in masks]
        bits[0] = bits[0][:2] + special("s", bool(mode & 0o100), bool(mode & 0o4000))
        bits[1] = bits[1][:2] + special("s", bool(mode & 0o010), bool(mode & 0o2000))
        bits[2] = bits[2][:2] + special("t", bool(mode & 0o001), bool(mode & 0o1000))
        return device_type + "".join(bits)

    def open_dir(self, decode_name=None):
        if decode_name is None:
            decode_name = lambda raw: raw.decode("utf8")
        if not self.volume.ignore_flags and not self.is_dir:
            raise Ext4Error(f"Inode ({self.inode_idx:d}) is not a directory.")
        data = self.open_read().read()
        offset, total = 0, len(data)
        while offset < total:
            if offset + 8 > total:
                raise Ext4Error(f"Truncated directory entry at offset {offset:d} of inode {self.inode_idx:d}")
            entry_ino = _u32(data, offset)
            rec_len = _u16(data, offset + 4)
            name_len = data[offset + 6]
            file_type = data[offset + 7]
            if rec_len < 8 or offset + rec_len > total:
                raise Ext4Error(
                    f"Invalid directory entry length {rec_len:d} at offset {offset:d} of inode {self.inode_idx:d}")
            if entry_ino != 0 and file_type != InodeType.CHECKSUM and 0 < name_len <= rec_len - 8:
                yield decode_name(data[offset + 8:offset + 8 + name_len]), entry_ino, file_type
            offset += rec_len

    def _extent_mapping(self):
        mapping = []
        header_offset = self.offset + 40
        pending = [header_offset]
        while pending:
            header_offset = pending.pop()
            header = self.volume.read(header_offset, 12)
            if len(header) < 12:
                raise EndOfStreamError(f"extent header extends past end of stream (inode {self.inode_idx:d})")
            if not self.volume.ignore_magic and _u16(header, 0) != _MAGIC_EXTENT:
                raise MagicError(
                    f"Invalid magic value in extent header at offset 0x{header_offset:X} of "
                    f"inode {self.inode_idx:d}: 0x{_u16(header, 0):04X} (expected 0x{_MAGIC_EXTENT:04X})")
            entries, depth = _u16(header, 2), _u16(header, 6)
            body = self.volume.read(header_offset + 12, entries * 12)
            for i in range(entries):
                entry = body[i * 12:(i + 1) * 12]
                if len(entry) < 12:
                    break
                if depth == 0:
                    logical = _u32(entry, 0)
                    length = _u16(entry, 4)
                    physical = _u32(entry, 8) | (_u16(entry, 6) << 32)
                    if length > _MAX_UNINIT_EXTENT_LEN:
                        continue
                    if length and physical:
                        mapping.append(MappingEntry(logical, physical, length))
                else:
                    leaf = _u32(entry, 4) | (_u16(entry, 8) << 32)
                    if leaf:
                        pending.append(leaf * self.volume.block_size)
        return mapping

    def _indirect_mapping(self, n_blocks):
        bs = self.volume.block_size
        pointers_per_block = bs // 4
        block_ptrs = self.inode.i_block
        mapping = []

        def read_pointers(block):
            data = self.volume.read(block * bs, bs)
            return [_u32(data, i * 4) for i in range(pointers_per_block)]

        def emit(logical, physical):
            if logical < n_blocks and physical:
                mapping.append(MappingEntry(logical, physical, 1))

        def walk(level, block, logical):
            if logical >= n_blocks:
                return logical
            if level == 0:
                if block:
                    emit(logical, block)
                return logical + 1
            if block == 0:
                return min(logical + pointers_per_block ** level, n_blocks)
            for ptr in read_pointers(block):
                logical = walk(level - 1, ptr, logical)
                if logical >= n_blocks:
                    break
            return logical

        logical = 0
        for i in range(12):
            emit(logical, _u32(block_ptrs, i * 4))
            logical += 1

        cursor = 12
        singly = _u32(block_ptrs, 48)
        if singly:
            for ptr in read_pointers(singly):
                logical = walk(0, ptr, logical)
                if logical >= n_blocks:
                    break
        cursor += pointers_per_block

        doubly = _u32(block_ptrs, 52)
        if doubly:
            logical = cursor
            for mid in read_pointers(doubly):
                logical = walk(1, mid, logical)
                if logical >= n_blocks:
                    break
        cursor += pointers_per_block * pointers_per_block

        triply = _u32(block_ptrs, 56)
        if triply:
            logical = cursor
            for top in read_pointers(triply):
                logical = walk(2, top, logical)
                if logical >= n_blocks:
                    break
        return mapping

    def open_read(self):
        inode = self.inode
        if (inode.i_flags & _FLAG_ENCRYPT) != 0:
            raise Ext4Error(f"inode {self.inode_idx:d} holds encrypted data")
        if (inode.i_flags & _FLAG_INLINE) != 0:
            return io.BytesIO(inode.i_block[:inode.i_size])
        if self._is_fast_symlink():
            return io.BytesIO(inode.i_block[:inode.i_size])

        size = inode.i_size
        block_size = self.volume.block_size
        n_blocks = (size + block_size - 1) // block_size if size else 0
        if (inode.i_flags & _FLAG_EXTENTS) != 0:
            mapping = self._extent_mapping()
        else:
            mapping = self._indirect_mapping(n_blocks)
        MappingEntry.optimize(mapping)
        return BlockReader(self.volume, size, mapping)

    @property
    def size_readable(self):
        size = self.inode.i_size
        if size < 1024:
            return f"{size:d} bytes" if size != 1 else "1 byte"
        units = ["KiB", "MiB", "GiB", "TiB", "PiB", "EiB", "ZiB", "YiB"]
        unit_idx = min(int(log_math(size, 1024)), len(units))
        return f"{size / (1024 ** unit_idx):.2f} {units[unit_idx - 1]:s}"

    def _read_xattr_inum(self, entry_raw):
        value_inum = _u32(entry_raw, 4)
        if value_inum == 0:
            return None
        store = self.volume.get_inode(value_inum, InodeType.FILE)
        if not self.volume.ignore_flags and (store.inode.i_flags & _FLAG_EA_INODE) == 0:
            raise Ext4Error(
                f"Inode {value_inum:d} associated with an extended attribute of inode {self.inode_idx:d} "
                f"is not marked as large extended attribute value.")
        return store.open_read().read()

    def _parse_xattrs(self, entries_raw, value_base, value_base_off):
        i, total = 0, len(entries_raw)
        while i + 4 <= total:
            name_len = entries_raw[i]
            if name_len == 0:
                break
            if i + 16 + name_len > total:
                raise Ext4Error(f"Truncated xattr entry in inode {self.inode_idx:d}")
            name_index = entries_raw[i + 1]
            if name_index not in _XATTR_PREFIXES:
                raise Ext4Error(f"Unknown attribute prefix {name_index:d} in inode {self.inode_idx:d}")
            value_offs = _u16(entries_raw, i + 2)
            value_size = _u32(entries_raw, i + 8)

            value = self._read_xattr_inum(entries_raw[i:i + 16])
            if value is None:
                start = value_base_off + value_offs
                end = start + value_size
                value = value_base[start:end] if 0 <= start <= end <= len(value_base) else b""

            name = _XATTR_PREFIXES[name_index] + entries_raw[i + 16:i + 16 + name_len].decode("iso-8859-2")
            yield name, value
            i += 4 * ((16 + name_len + 3) // 4)

    def xattrs(self, check_inline=True, check_block=True, force_inline=False):
        inode = self.inode
        inline_base = 128 + inode.i_extra_isize
        inline_len = self.volume.superblock.s_inode_size - inline_base
        if check_inline and inline_len > 4:
            inline_raw = self.volume.read(self.offset + inline_base, inline_len)
            if force_inline or inline_raw[:4] == b"\x00\x00\x02\xea":
                try:
                    yield from self._parse_xattrs(inline_raw[4:], inode.raw, inline_base)
                except Exception:
                    ...

        if check_block and inode.i_file_acl != 0:
            block_start = inode.i_file_acl * self.volume.block_size
            block = self.volume.read(block_start, self.volume.block_size)
            if block:
                if not self.volume.ignore_magic and _u32(block, 0) != _MAGIC_XATTR:
                    print(f"Invalid magic value in xattrs block header at offset 0x{block_start:X} of "
                          f"inode {self.inode_idx:d}: 0x{_u32(block, 0):08X} (expected 0x{_MAGIC_XATTR:08X})")
                    return
                if _u32(block, 8) != 1:
                    print(f"Invalid number of xattr blocks at offset 0x{block_start:X} "
                          f"of inode {self.inode_idx:d}: {_u32(block, 8):d} (expected 1)")
                    return
                yield from self._parse_xattrs(block[32:], block, 0)

class BlockReader:
    EINVAL = 22

    def __init__(self, volume, byte_size, block_map):
        self.byte_size = byte_size
        self.volume = volume
        self.cursor = 0
        self.block_map = list(map(MappingEntry.copy, block_map))
        MappingEntry.optimize(self.block_map)
        self._map_starts = [entry.file_block_idx for entry in self.block_map]

    def __repr__(self):
        return (f"{type(self).__name__:s}(byte_size = {self.byte_size!r:s}, "
                f"block_map = {self.block_map!r:s}, volume_uuid = {self.volume.uuid!r:s})")

    def close(self):
        self.block_map = []
        self._map_starts = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def get_block_mapping(self, file_block_idx):
        entry_idx = bisect_right(self._map_starts, file_block_idx) - 1
        if entry_idx < 0:
            return None
        entry = self.block_map[entry_idx]
        if file_block_idx < entry.file_block_idx + entry.block_count:
            return entry.disk_block_idx + (file_block_idx - entry.file_block_idx)
        return None

    def read_block(self, file_block_idx):
        disk_block_idx = self.get_block_mapping(file_block_idx)
        if disk_block_idx is not None:
            return self.volume.read(disk_block_idx * self.volume.block_size, self.volume.block_size)
        return bytes(self.volume.block_size)

    def read(self, byte_len=-1):
        if byte_len < -1:
            raise ValueError("byte_len must be non-negative or -1")
        bytes_remaining = self.byte_size - self.cursor
        requested = bytes_remaining if byte_len == -1 else max(0, min(byte_len, bytes_remaining))
        if requested == 0:
            return b""

        block_size = self.volume.block_size
        start_block_idx = self.cursor // block_size
        end_block_idx = (self.cursor + requested - 1) // block_size
        blocks = [self.read_block(i) for i in range(start_block_idx, end_block_idx + 1)]

        start_offset = self.cursor % block_size
        if start_offset != 0:
            blocks[0] = blocks[0][start_offset:]
        last_block_len = (requested + start_offset - block_size - 1) % block_size + 1
        blocks[-1] = blocks[-1][:last_block_len]

        result = b"".join(blocks)
        if len(result) != requested:
            raise EndOfStreamError(f"The volume's underlying stream ended {requested - len(result):d} bytes before EOF.")
        self.cursor += len(result)
        return result

    def seek(self, seek, seek_mode=io.SEEK_SET):
        if seek_mode == io.SEEK_CUR:
            seek += self.cursor
        elif seek_mode == io.SEEK_END:
            seek += self.byte_size
        if seek < 0:
            raise OSError(BlockReader.EINVAL, "Invalid argument")
        self.cursor = seek
        return seek

    def tell(self):
        return self.cursor
