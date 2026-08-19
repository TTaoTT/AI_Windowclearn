"""扫描引擎：构建目录大小树，自动过滤系统保护目录，结合规则打标签。

生产路径为 os.scandir 递归（正确、跨平台可测）；NTFS MFT 直读作为性能优化，
默认关闭（设环境变量 ENABLE_MFT=1 且 Windows 时尝试，失败自动回退）。

本模块支持：
- on_progress 回调：扫描过程中周期性上报 {phase, path, dirs, files, size}
- pause_event / cancel_event：支持运行时暂停(继续)与取消
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from .rules import RuleEngine, default
from .util import IS_WIN, is_reparse_point, reparse_target


# 扫描时直接跳过的系统目录（非清理分析目标，且常需特殊权限）
SKIP_NAMES = {"$recycle.bin", "system volume information", "$sysreset", "$windows.~bt"}


class ScanCancelled(Exception):
    """用户取消扫描时抛出，由上层捕获并终止。"""


@dataclass
class Node:
    path: str
    name: str
    size: int = 0
    is_dir: bool = True
    children: list["Node"] = field(default_factory=list, repr=False)
    clean_tags: list[dict] = field(default_factory=list, repr=False)
    migrate_tags: list[dict] = field(default_factory=list, repr=False)
    risk: str = ""
    cascade: bool = False        # 已重定向（junction/符号链接），数据实际在其他盘
    reparse_target: str = ""     # 级联指向的目标路径

    def tag_text(self) -> str:
        parts = []
        if self.cascade:
            parts.append("级联(已重定向)")
        if self.clean_tags:
            parts.append("清理:" + "/".join(t.get("risk", "") for t in self.clean_tags))
        if self.migrate_tags:
            parts.append("迁移:" + "/".join(t.get("risk", "") for t in self.migrate_tags))
        return " ".join(parts)


class Scanner:
    def __init__(self, rules: Optional[RuleEngine] = None):
        self.rules = rules or default()
        self.method = "walk"
        self._dirs = 0
        self._file_count = 0
        self._size_count = 0
        self._last_report = 0.0
        self._since_report = 0

    # ---------- 主入口 ----------
    def scan(self, root: str, tag: bool = True,
             on_progress=None, pause_event: Optional[threading.Event] = None,
             cancel_event: Optional[threading.Event] = None) -> Node:
        root = os.path.abspath(root)
        self._dirs = 0
        self._file_count = 0
        self._size_count = 0
        self._last_report = 0.0
        self._since_report = 0
        if IS_WIN and os.environ.get("ENABLE_MFT") == "1":
            try:
                return self._scan_mft(root, tag, on_progress, pause_event, cancel_event)
            except Exception:  # noqa: BLE001
                pass  # 回退到 walk
        return self._scan_walk(root, tag, on_progress, pause_event, cancel_event)

    # ---------- 暂停 / 取消检查 ----------
    def _check_pause(self, pause_event, cancel_event):
        if cancel_event is not None and cancel_event.is_set():
            raise ScanCancelled()
        if pause_event is not None:
            while pause_event.is_set():
                if cancel_event is not None and cancel_event.is_set():
                    raise ScanCancelled()
                time.sleep(0.1)

    # ---------- 进度上报（内置节流） ----------
    def _report(self, phase: str, path: str, on_progress):
        if on_progress is None:
            return
        self._since_report += 1
        now = time.monotonic()
        # 节流：未达 16 次且距上次上报不足 150ms 则跳过，避免刷屏拖慢扫描
        if self._since_report < 16 and (now - self._last_report) < 0.15:
            return
        self._last_report = now
        self._since_report = 0
        try:
            on_progress({
                "phase": phase,
                "path": path,
                "dirs": self._dirs,
                "files": self._file_count,
                "size": self._size_count,
            })
        except Exception:  # noqa: BLE001
            pass

    def _report_final(self, on_progress, root_node: Node):
        if on_progress is None:
            return
        try:
            on_progress({
                "phase": "done",
                "path": root_node.path,
                "dirs": self._dirs,
                "files": self._file_count,
                "size": root_node.size,
            })
        except Exception:  # noqa: BLE001
            pass

    # ---------- os.scandir 递归（生产路径） ----------
    def _scan_walk(self, root, tag, on_progress, pause_event, cancel_event) -> Node:
        self.method = "walk"
        nodes: dict[str, Node] = {}

        def ensure(path: str) -> Node:
            n = nodes.get(path)
            if n is None:
                n = Node(path=path, name=os.path.basename(path) or path)
                nodes[path] = n
            return n

        root_node = ensure(root)
        stack = [(root, False)]
        while stack:
            self._check_pause(pause_event, cancel_event)
            path, processed = stack.pop()
            node = nodes[path]
            if not processed:
                stack.append((path, True))
                try:
                    with os.scandir(path) as it:
                        for e in it:
                            try:
                                if e.is_symlink():
                                    continue
                                if e.is_dir(follow_symlinks=False):
                                    if e.name.lower() in SKIP_NAMES:
                                        continue
                                    child = ensure(e.path)
                                    node.children.append(child)
                                    if is_reparse_point(e.path):
                                        # 级联文件夹（junction/符号链接）：数据在别的盘，
                                        # 不递归、体积记 0，只做标记，避免把目标盘数据算进 C 盘
                                        child.cascade = True
                                        child.reparse_target = reparse_target(e.path)
                                        self._dirs += 1
                                        self._report("scan", path, on_progress)
                                        continue
                                    stack.append((e.path, False))
                                    self._dirs += 1
                                    self._report("scan", path, on_progress)
                                else:
                                    self._file_count += 1
                                    try:
                                        self._size_count += e.stat(follow_symlinks=False).st_size
                                    except (PermissionError, OSError):
                                        pass
                                    self._report("scan", path, on_progress)
                            except (PermissionError, OSError):
                                continue
                except (PermissionError, OSError):
                    pass
            else:
                total = 0
                try:
                    with os.scandir(path) as it:
                        for e in it:
                            try:
                                if e.is_symlink():
                                    continue
                                if e.is_dir(follow_symlinks=False):
                                    total += nodes.get(e.path, Node(path=e.path, name="", size=0)).size
                                else:
                                    try:
                                        total += e.stat(follow_symlinks=False).st_size
                                    except (PermissionError, OSError):
                                        pass
                            except (PermissionError, OSError):
                                continue
                except (PermissionError, OSError):
                    pass
                node.size = total
                if tag:
                    cls = self.rules.classify_dir(path)
                    node.clean_tags = cls["clean"]
                    node.migrate_tags = cls["migrate"]
                    # 补上已知应用画像（让分析页能显示"VS Code / Maven / Godot 等"）
                    for p in self.rules.match_app(path):
                        node.migrate_tags.append({
                            "id": p.get("id"), "risk": p.get("risk", ""),
                            "description": f"{p.get('name', '')}：{p.get('description', '')}",
                            "app": True,
                        })
                    node.risk = self._best_risk(cls)
                self._report("tag", path, on_progress)
        self._report_final(on_progress, root_node)
        return root_node

    @staticmethod
    def _best_risk(cls: dict) -> str:
        risks = [t.get("risk", "") for t in cls["clean"]] + [t.get("risk", "") for t in cls["migrate"]]
        order = {"danger": 3, "L3": 3, "cautious": 2, "L2": 2, "safe": 1, "L1": 1}
        best = max((order.get(r, 0) for r in risks), default=0)
        inv = {v: k for k, v in order.items()}
        return inv.get(best, "")

    # ---------- NTFS MFT 直读（实验性，默认关闭） ----------
    def _scan_mft(self, root, tag, on_progress, pause_event, cancel_event) -> Node:
        # 完整 MFT 枚举需解析 USN 日志并重建路径树，体积大且易错。
        # 本项目以 os.walk 为正确路径；如需极致性能可在此实现
        # DeviceIoControl(FSCTL_ENUM_USN_DATA) 并构建 ref->path 映射。
        # 此处直接回退，保证正确性。
        self.method = "mft(fallback->walk)"
        raise NotImplementedError("MFT 直读未启用，使用 walk 路径")

    # ---------- 快照 / diff ----------
    def save_snapshot(self, root_node: Node, path: str) -> None:
        snap: dict[str, int] = {}

        def walk(n: Node):
            snap[n.path] = n.size
            for c in n.children:
                walk(c)

        walk(root_node)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snap, f)

    def diff(self, root_node: Node, snapshot_path: str) -> list[tuple[str, int, int]]:
        """对比当前树与历史快照，返回 [(path, old_size, new_size)] 中增长的项。"""
        if not os.path.exists(snapshot_path):
            return []
        with open(snapshot_path, "r", encoding="utf-8") as f:
            old = json.load(f)
        cur: dict[str, int] = {}
        res: list[tuple[str, int, int]] = []

        def walk(n: Node):
            cur[n.path] = n.size
            for c in n.children:
                walk(c)

        walk(root_node)
        for p, sz in cur.items():
            o = old.get(p)
            if o is not None and sz > o:
                res.append((p, o, sz))
        res.sort(key=lambda x: x[2] - x[1], reverse=True)
        return res
