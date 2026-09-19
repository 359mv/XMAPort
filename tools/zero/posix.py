"""跨平台符号链接辅助（供 ext4 镜像解包/打包流程使用）。

POSIX 平台直接使用 os.symlink / os.readlink；Windows 平台（NTFS、
无管理员权限）无法创建真实的符号链接，改用占位文件方案：把链接
目标写入普通文件，并附加 SYSTEM 文件属性。这样链接元数据可以在
解包 -> 重打包管线中无损往返。

占位文件格式（对外契约，不可更改）：
    b'!<symlink>' + 目标路径(UTF-16-LE) + b'\\x00\\x00'
"""
import os

if os.name == "nt":
    from ctypes import windll
    from ctypes.wintypes import DWORD, LPCWSTR
    from stat import FILE_ATTRIBUTE_SYSTEM

_TAG = b"!<symlink>"
_TAG_TAIL = b"\x00\x00"

def _as_link_file(raw: bytes) -> bytes:
    """按占位文件格式拼装完整字节内容。"""
    return _TAG + raw.encode("utf-16-le") + _TAG_TAIL

def _mark_as_system(path: str) -> None:
    """为占位文件附加 SYSTEM 属性（仅限 Windows，失败时静默忽略）。

    必须使用 Unicode 版本的 SetFileAttributesW，含非 ASCII 字符的
    路径才能正确处理；ANSI 版本不支持。
    """
    try:
        windll.kernel32.SetFileAttributesW(
            LPCWSTR(path), DWORD(FILE_ATTRIBUTE_SYSTEM))
    except Exception:
        pass

def _write_placeholder(link_path: str, link_target: str) -> None:
    """在 Windows 上写入占位文件并设置 SYSTEM 属性。"""
    normalized = link_path.replace("/", os.sep)
    with open(normalized, "wb") as handle:
        handle.write(_as_link_file(link_target))
    _mark_as_system(normalized)

def _decode_placeholder(path: str) -> str:
    """尝试把文件解析为占位文件，返回其中保存的链接目标。

    不是占位文件或读取失败时返回空串；目录一律返回空串。
    """
    if os.path.isdir(path):
        return ""
    try:
        with open(path, "rb") as handle:
            head = handle.read(len(_TAG))
            if head != _TAG:
                return ""
            body = handle.read()
    except OSError:
        return ""
    return body.decode("utf-16-le", errors="replace").rstrip("\x00")

def symlink(link_target, link_path):
    """创建符号链接（POSIX）或占位文件（Windows）。

    父目录不存在时会先自动创建。
    """
    parent = os.path.dirname(link_path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)

    if os.name == "posix":
        os.symlink(link_target, link_path)
        return
    _write_placeholder(link_path, link_target)

def readlink(path):
    """读取符号链接目标（POSIX）或占位文件内容（Windows）。

    Windows 上非占位文件、目录或读取失败均返回空串。
    """
    if os.name != "nt":
        return os.readlink(path)
    return _decode_placeholder(path)
