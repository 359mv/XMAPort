"""Android ext4 system-image unpacker (pure standard library).

Reads an ext4 filesystem image, replays its directory tree onto disk and
emits the metadata files consumed by the repack stage:

  work/config/{partition}_fs_config        ownership / permission rows
  work/config/{partition}_file_contexts    SELinux label rules (if any)
  work/config/{partition}_size.txt         final image byte size
  work/config/{partition}_space.txt        paths containing spaces (if any)

Sparse images are converted to raw form in place before parsing. Images
wrapped in a vendor-specific lead-in header are detected and unwrapped
automatically (the payload is rebuilt starting at the embedded ext4
superblock and copied through to end-of-file).

Public contract (used by tools/extract_img.py):

    from zero.imgextractor import Extractor
    Extractor().main(image, output_dir, work_root, 'img')
"""
from __future__ import annotations

import os
import re
import struct
from dataclasses import dataclass, field
from time import perf_counter

from . import ext4
from .img_init import simg2img as _convert_sparse_to_raw
from .posix import symlink as _place_symlink

_SUPERBLOCK_SIGNATURE = b'\x53\xef'
_MAGIC_TO_SUPERBLOCK = 1080
_HEADER_PROBE_BYTES = 500_000
_STREAM_CHUNK = 1 << 20
_FAILURE_BUDGET = 200
_NAME_BREAKS = ('-', ' ', '+', '{', '(')
_MOUNT_NAME_REJECTS = '.@#'

_UID_ROOT = 0
_UID_VENDOR_ROOT = 2000

def _first_token(raw: str, drop_extension: bool) -> str:
    """Reduce a raw file name to its leading token.

    Splits on each character in :data:`_NAME_BREAKS` and keeps the first
    segment. When ``drop_extension`` is set the final extension is removed
    first (used for partition names derived from image file names).
    """
    token = os.path.basename(raw)
    if drop_extension:
        token = token.rsplit('.', 1)[0]
    for breaker in _NAME_BREAKS:
        token = token.split(breaker)[0]
    return token

def _ls_mode_to_octal(perm_string: str) -> str:
    """Translate an ``ls -l`` permission string into octal digits.

    Handles the usual rwx triplets plus setuid / setgid / sticky bits
    encoded as ``s``/``S`` and ``t``/``T``. Malformed input collapses to
    ``'000'``.
    """
    if len(perm_string) >= 10 and perm_string[0] in '-dlcbps':
        perm_string = perm_string[1:]
    if len(perm_string) != 9:
        return '000'

    def triplet(read: str, write: str, exec_: str) -> int:
        value = 0
        if read == 'r':
            value += 4
        if write == 'w':
            value += 2
        if exec_ in 'xXsStT':
            value += 1
        return value

    digits = (
        triplet(perm_string[0], perm_string[1], perm_string[2]),
        triplet(perm_string[3], perm_string[4], perm_string[5]),
        triplet(perm_string[6], perm_string[7], perm_string[8]),
    )

    special = 0
    if perm_string[2] in 'sS':
        special += 4
    if perm_string[5] in 'sS':
        special += 2
    if perm_string[8] in 'tT':
        special += 1

    prefix = str(special) if special else ''
    return prefix + ''.join(str(d) for d in digits)

def _capability_tail(blob: bytes) -> str:
    """Render a ``security.capability`` xattr as an fs_config suffix.

    Expects the legacy 20-byte ``<5I`` layout and keeps the historical
    combined-capability encoding so downstream Android build tooling
    accepts the value. Returns an empty string for anything unusable.
    """
    if len(blob) != 20:
        return ''
    try:
        fields = struct.unpack('<5I', blob)
    except struct.error:
        return ''
    if fields[1] > 65535:
        packed = (fields[3] << 16) | fields[1]
    else:
        packed = (fields[3] << 32) | (fields[2] << 16) | fields[1]
    return f' capabilities={hex(packed)}'

def _link_destination(inode: 'ext4.Inode') -> str:
    """Decode a symlink inode's payload into its target path."""
    try:
        with inode.open_read() as stream:
            payload = stream.read()
        if payload:
            return payload.decode('utf-8')
    except (OSError, UnicodeDecodeError):
        pass
    return ''

def _emit_text(content: str, destination: str) -> None:
    """Persist ``content`` as UTF-8 / LF with a trailing newline."""
    parent = os.path.dirname(destination)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    with open(destination, 'w', newline='\n', encoding='utf-8') as handle:
        handle.write(str(content).strip() + '\n')

def _enforce_owner(path: str, octal_mode: str, uid: int, gid: int) -> None:
    """Apply permissions and ownership when running as root on POSIX."""
    if os.name == 'posix' and os.geteuid() == 0:
        os.chmod(path, int(octal_mode, 8))
        os.chown(path, uid, gid)

def _discard(path: str) -> None:
    """Best-effort delete used when replacing files."""
    try:
        os.remove(path)
    except OSError:
        pass

@dataclass
class _Session:
    """Mutable state shared by the extraction pipeline stages."""

    image_path: str = ''
    tree_root: str = ''
    config_dir: str = ''
    partition: str = ''
    ownership_rows: list = field(default_factory=list)
    label_rows: list = field(default_factory=list)
    spaced_paths: list = field(default_factory=list)
    failures: int = 0

def _probe_volume_name(mount_point: str) -> str:
    """Reduce a superblock mount point to a usable single name segment.

    Returns an empty string when the mount point is missing or contains
    characters that mark it as junk (``.``, ``@``, ``#``).
    """
    name = mount_point
    if name.startswith('/'):
        name = name[1:]
    if '/' in name:
        name = name.rsplit('/', 1)[-1]
    if any(bad in name for bad in _MOUNT_NAME_REJECTS):
        return ''
    return name

def _shed_wrapper_header(image_path: str) -> None:
    """Rebuild an image whose real content starts behind a wrapper header.

    The first bytes are scanned for the ``MOTO`` marker. For every ext4
    magic occurrence the putative superblock start (magic minus its
    1080-byte distance) must be NUL; the first such offset is where the
    genuine image begins. Everything from that offset through end-of-file
    is streamed into a replacement file which then takes the original's
    place. Files without the marker are left untouched.
    """
    if not os.path.isfile(image_path):
        return

    with open(image_path, 'rb') as handle:
        probe = handle.read(_HEADER_PROBE_BYTES)
    if b'MOTO' not in probe:
        return

    starts = []
    for match in re.finditer(_SUPERBLOCK_SIGNATURE, probe):
        start = match.start() - _MAGIC_TO_SUPERBLOCK
        if start >= 0 and probe[start] == 0:
            starts.append(start)
    if not starts:
        return

    rebuilt = image_path + '_'
    if os.path.exists(rebuilt):
        _discard(rebuilt)
    with open(image_path, 'rb') as src, open(rebuilt, 'wb') as dst:
        src.seek(starts[0])
        while True:
            block = src.read(_STREAM_CHUNK)
            if not block:
                break
            dst.write(block)

    if os.path.exists(rebuilt):
        _discard(image_path)
        os.rename(rebuilt, image_path)

def _grow_to_declared_size(image_path: str) -> None:
    """Pad a truncated image up to the size its superblock declares."""
    on_disk = os.path.getsize(image_path)
    with open(image_path, 'rb+') as handle:
        volume = ext4.Volume(handle)
        declared = volume.get_block_count * volume.block_size
        if on_disk < declared:
            print(
                f'  [W] Image is smaller than its superblock claims, '
                f'growing {on_disk} -> {declared}')
            handle.truncate(declared)

def _harvest(job: _Session, directory: 'ext4.Inode', prefix: str) -> None:
    """Walk one directory inode recursively, materialising every entry."""
    for entry_name, entry_index, entry_kind in directory.open_dir():
        if entry_name in ('.', '..') or entry_name.endswith(' (2)'):
            continue
        if '/' in entry_name or '\\' in entry_name:
            print(f'  [W] Refusing entry with embedded separator: {entry_name!r}')
            job.failures += 1
            continue
        if job.failures >= _FAILURE_BUDGET:
            print('  [W] Failure budget exhausted, aborting extraction.')
            break

        node = directory.volume.get_inode(entry_index, entry_kind)
        relative = prefix + '/' + entry_name

        if os.name == 'nt' and ':' in relative:
            relative = relative.replace(':', '_')

        if relative.endswith('/') and not node.is_dir:
            job.failures += 1
            continue

        octal_mode = _ls_mode_to_octal(node.mode_str)
        uid = node.inode.i_uid
        gid = node.inode.i_gid
        capability = ''
        link_to = ''

        for attr_name, attr_value in node.xattrs():
            if attr_name == 'security.selinux':
                label = attr_value.decode('utf-8').rstrip('\x00')
                job.label_rows.append(
                    f'/{job.partition}{re.escape(relative)} {label}')
            elif attr_name == 'security.capability':
                capability = _capability_tail(attr_value)

        if node.is_symlink:
            link_to = _link_destination(node)

        config_path = job.partition + relative
        if ' ' in config_path[1:]:
            job.spaced_paths.append(config_path)
            config_path = config_path.replace(' ', '_')

        job.ownership_rows.append(
            f'{config_path} {uid} {gid} {octal_mode}{capability} '
            f'{link_to}'.rstrip())

        if node.is_dir:
            landing = job.tree_root + relative.replace(' ', '_').replace('"', '')
            if landing.endswith('.') and os.name == 'nt':
                landing = landing[:-1]
            os.makedirs(landing, exist_ok=True)
            _enforce_owner(landing, octal_mode, uid, gid)
            _harvest(job, node, relative)
        elif node.is_file:
            landing = job.tree_root + relative.replace(' ', '_').replace('"', '')
            parent = os.path.dirname(landing)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            with open(landing, 'wb') as sink, node.open_read() as source:
                while True:
                    block = source.read(_STREAM_CHUNK)
                    if not block:
                        break
                    sink.write(block)
            _enforce_owner(landing, octal_mode, uid, gid)
        elif node.is_symlink:
            landing = job.tree_root + relative.replace(' ', '_')
            if os.path.exists(landing) or os.path.islink(landing):
                _discard(landing)
            _place_symlink(link_to, landing)

def _prepend_root_rows(job: _Session) -> None:
    """Insert the mandatory /, lost+found and partition-root rows."""
    name = job.partition
    root_uid = _UID_VENDOR_ROOT if name == 'vendor' else _UID_ROOT
    job.ownership_rows.insert(0, f'/ {_UID_ROOT} {root_uid} 0755')

    if name == 'vendor':
        job.ownership_rows.insert(1, f'{name} {_UID_ROOT} {_UID_VENDOR_ROOT} 0755')
    else:
        job.ownership_rows.insert(1, f'/lost+found {_UID_ROOT} {_UID_ROOT} 0700')

    job.ownership_rows.insert(
        2 if name == 'system' else 1, f'{name} {_UID_ROOT} {_UID_ROOT} 0755')

def _prepend_label_rules(job: _Session) -> None:
    """Seed file_contexts with root-level rules built from a known label."""
    seed = ''
    for row in job.label_rows:
        if 'build.prop' in row or '/lost+found' in row:
            seed = row.split(maxsplit=1)[1]
            break
    if not seed:
        return
    name = job.partition
    job.label_rows.insert(0, f'/ {seed}')
    job.label_rows.insert(1, f'/{name}(/.*)? {seed}')
    job.label_rows.insert(2, f'/{name} {seed}')
    job.label_rows.insert(3, f'/{name}/lost+\\found {seed}')

def _flush_metadata(job: _Session) -> None:
    """Write every generated config file into the work config directory."""
    _emit_text(
        '\n'.join(job.ownership_rows),
        os.path.join(job.config_dir, f'{job.partition}_fs_config'))
    if job.spaced_paths:
        _emit_text(
            '\n'.join(job.spaced_paths),
            os.path.join(job.config_dir, f'{job.partition}_space.txt'))
    if job.label_rows:
        _prepend_label_rules(job)
        job.label_rows.sort()
        _emit_text(
            '\n'.join(job.label_rows),
            os.path.join(job.config_dir, f'{job.partition}_file_contexts'))

def _run_pipeline(job: _Session) -> None:
    """Extract the whole image and persist all metadata files."""
    os.makedirs(job.config_dir, exist_ok=True)
    _emit_text(
        os.path.getsize(job.image_path),
        os.path.join(job.config_dir, f'{job.partition}_size.txt'))

    with open(job.image_path, 'rb') as handle:
        _harvest(job, ext4.Volume(handle).root, '')

    _prepend_root_rows(job)
    _flush_metadata(job)

class Extractor:
    """Compatibility facade: no-argument construction, single ``main`` call."""

    def main(
        self,
        target: str,
        output_dir: str,
        work: str,
        target_type: str = 'img',
    ) -> None:
        """Unpack ``target`` into ``output_dir`` with metadata under ``work``.

        ``target_type`` accepts ``'img'`` (raw ext4) or ``'s_img'``
        (Android sparse, converted in place first). The extraction tree is
        ``realpath(dirname(output_dir))/<name>`` where ``<name>`` derives
        from ``output_dir``'s base name unless the image superblock
        advertises a more trustworthy mount-point name.
        """
        job = _Session()
        out_parent = os.path.realpath(os.path.dirname(output_dir))
        tree_name = _first_token(os.path.basename(output_dir), drop_extension=False)
        job.tree_root = out_parent + os.sep + tree_name
        job.image_path = (
            os.path.realpath(os.path.dirname(target))
            + os.sep + os.path.basename(target))
        job.partition = _first_token(os.path.basename(target), drop_extension=True)
        job.config_dir = os.path.join(work, 'config')

        if target_type == 's_img':
            _convert_sparse_to_raw(target)
            target_type = 'img'

        with open(job.image_path, 'rb+') as handle:
            volume = ext4.Volume(handle)
            candidate = _probe_volume_name(volume.get_mount_point)
        if candidate and candidate != tree_name and job.partition != 'mi_ext':
            print(f'  [N] Image name looks wrong, extracting as {candidate}')
            job.tree_root = out_parent + os.sep + candidate
            job.partition = candidate

        if target_type == 'img':
            with open(job.image_path, 'rb') as handle:
                head = handle.read(_HEADER_PROBE_BYTES)
            if b'MOTO' in head:
                print('  [N] Wrapper header detected, rebuilding image...')
                _shed_wrapper_header(job.image_path)
            _grow_to_declared_size(job.image_path)

            print(
                f'  [..] Extracting {os.path.basename(target)} '
                f'--> {os.path.basename(job.tree_root)}',
                end='', flush=True)
            started = perf_counter()
            _run_pipeline(job)
            print(
                f'\r  [OK] Extracted {os.path.basename(target)} '
                f'({perf_counter() - started:.2f}s)')
