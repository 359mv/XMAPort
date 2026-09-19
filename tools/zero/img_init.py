"""Android sparse 镜像转换辅助。

调用随包分发的 simg2img.exe，将 sparse 镜像就地替换为等价的 raw
镜像。调用方（imgextractor）依赖的容错语义：输入不是 sparse 镜像、
工具缺失或转换失败时，仅打印 [ERROR] 日志并返回，不抛出异常。
"""
import os
import subprocess
import time

_MAGIC = b"\x3a\xff\x26\xed"
_TEMP_SUFFIX = ".unsparse"
_TOOL_NAME = "simg2img.exe"

_RESET = "\033[0m"
_GREEN = "\033[92m"
_RED = "\033[91m"

def _log_ok(message: str) -> None:
    """以绿色 [INFO] 前缀输出一行日志。"""
    print(f"  {_GREEN}[INFO]{_RESET}  {message}")

def _log_fail(message: str) -> None:
    """以红色 [ERROR] 前缀输出一行日志。"""
    print(f"  {_RED}[ERROR]{_RESET} {message}")

def _locate_tool() -> str:
    """在本模块上上级目录查找转换工具，找到返回绝对路径，否则返回空串。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidate = os.path.join(root, _TOOL_NAME)
    return candidate if os.path.isfile(candidate) else ""

def _has_sparse_header(file_path: str) -> bool:
    """读取文件头 4 字节，判断是否为 sparse 镜像。"""
    try:
        with open(file_path, "rb") as handle:
            return handle.read(4) == _MAGIC
    except OSError:
        return False

def _run_tool(tool: str, source: str, dest: str) -> int:
    """以列表参数调用外部转换工具，屏蔽其输出，返回退出码。"""
    proc = subprocess.run(
        [tool, source, dest],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc.returncode

def _discard(temp_path: str) -> None:
    """尽力删除中间产物，忽略删除失败。"""
    try:
        os.remove(temp_path)
    except OSError:
        pass

def simg2img(path: str):
    """将 Android sparse 镜像就地转换为 raw 镜像。

    - 非 sparse 镜像：打印 [ERROR] 后直接返回，不抛异常。
    - 转换失败：清理临时文件，打印 [ERROR] 后返回。
    - 成功：原文件被 raw 版本覆盖，打印耗时。
    """
    name = os.path.basename(path)
    if not _has_sparse_header(path):
        _log_fail(f"{name}: not a sparse image, skipping")
        return

    tool = _locate_tool()
    if not tool:
        _log_fail(f"{_TOOL_NAME} not found")
        return

    work_file = path + _TEMP_SUFFIX
    size_mb = os.path.getsize(path) / (1024 * 1024)
    _log_ok(f"Converting {name} ({size_mb:.1f}MB) sparse->raw...")

    started = time.time()
    code = _run_tool(tool, path, work_file)
    took = time.time() - started

    if code != 0:
        _log_fail(f"{_TOOL_NAME} failed (exit code {code})")
        _discard(work_file)
        return

    os.remove(path)
    os.rename(work_file, path)
    _log_ok(f"{name} converted ({took:.1f}s)")
