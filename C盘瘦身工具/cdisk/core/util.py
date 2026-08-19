"""跨平台路径工具。"""
from __future__ import annotations

import os

IS_WIN = os.name == "nt"


def normalize(path: str) -> str:
    """规范化路径：统一分隔符、normpath、Windows 下转小写用于比较。"""
    p = path.replace("/", "\\")
    p = os.path.normpath(p)
    if IS_WIN:
        p = p.lower()
    return p


def expand(pattern: str) -> str:
    """展开环境变量（%VAR%）并规范化。"""
    return normalize(os.path.expandvars(pattern))


def is_subpath(path: str, base: str) -> bool:
    """path 是否等于 base 或位于 base 之下。"""
    p = normalize(path)
    b = normalize(base)
    if p == b:
        return True
    return p.startswith(b + "\\")


def pattern_matches(pattern: str, path: str) -> bool:
    """判断 path 是否命中 pattern。pattern 支持 %ENV% 与结尾 '*' 前缀匹配。"""
    pat = expand(pattern)
    p = normalize(path)
    # 结尾通配：前缀匹配
    if pat.endswith("\\*"):
        prefix = pat[:-2]
        return p == prefix or p.startswith(prefix + "\\")
    # 中间通配：fnmatch
    import fnmatch
    return fnmatch.fnmatch(p, pat) or p == pat


def human_size(num: int) -> str:
    """字节数转人类可读。"""
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(num) < 1024.0:
            return f"{num:.1f} {unit}" if unit != "B" else f"{num} B"
        num /= 1024.0
    return f"{num:.1f} EB"


def drives() -> list[str]:
    """返回本机固定盘符列表（Windows）；其它平台返回 ['/']。"""
    if not IS_WIN:
        return ["/"]
    import string
    result = []
    for d in string.ascii_uppercase:
        root = f"{d}:\\"
        try:
            if os.path.exists(root):
                result.append(root)
        except OSError:
            continue
    return result


def is_reparse_point(path: str) -> bool:
    """路径是否为 reparse point（目录联接 junction / 符号链接）。

    Windows 用 GetFileAttributes 检查 FILE_ATTRIBUTE_REPARSE_POINT(0x400)。
    注意：junction 不会被 os.path.islink 识别，必须走这个检测。
    """
    if not IS_WIN:
        return os.path.islink(path)
    try:
        import ctypes
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return attrs != 0xFFFFFFFF and bool(attrs & 0x400)
    except Exception:  # noqa: BLE001
        return False


def reparse_target(path: str) -> str:
    """reparse point 指向的真实目标（解析到最终路径）；非链接返回空串。"""
    if not is_reparse_point(path):
        return ""
    try:
        return os.path.realpath(path)
    except OSError:
        return ""


def nowin_kw() -> dict:
    """子进程隐藏控制台窗口的参数（Windows：STARTUPINFO 隐藏 + CREATE_NO_WINDOW），
    避免迁移/清理时弹出 DOS 黑窗；非 Windows 返回空 dict。"""
    if not IS_WIN:
        return {}
    import subprocess
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE
    return {
        "startupinfo": si,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }
