"""清理引擎：回收站删除 / 硬删除 / 系统 API 通道，全部经安全日志与红线拦截。

设计：默认 dry_run=True（仅预览），确认后再执行；删除默认走回收站/本地回收目录，
硬删除仅用于系统通道返回的路径；被规则红线(is_protected)命中的路径绝不删除。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from datetime import datetime, timedelta
from typing import Iterable

from .rules import RuleEngine
from .safety import SafetyManager
from .util import IS_WIN

FO_DELETE = 3
FOF_ALLOWUNDO = 0x40
FOF_NOCONFIRMATION = 0x10


class Cleaner:
    def __init__(self, rules: RuleEngine | None = None, safety: SafetyManager | None = None):
        self.rules = rules or RuleEngine.default()
        self.safety = safety or SafetyManager()

    # ---------- 公共入口 ----------
    def clean_targets(self, targets: Iterable[dict], dry_run: bool = True) -> list[dict]:
        """targets: [{"path": str, "rule": dict}, ...]
        返回执行结果列表。
        """
        results = []
        for t in targets:
            path = t["path"]
            rule = t.get("rule", {}) or {}
            action = rule.get("action", "recycle")
            risk = rule.get("risk", "")
            age_days = (rule.get("match") or {}).get("age_days", 0) or 0
            rid = self.safety.log("clean.pre", path, risk=risk,
                                  detail=f"action={action},dry={dry_run}")
            if self.rules.is_protected(path):
                results.append({"path": path, "status": "blocked", "reason": "红线目录"})
                self.safety.log("clean.blocked", path, risk=risk, result="blocked")
                continue
            try:
                if action == "system_api":
                    out = self._system_api(rule.get("system_cmd", ""), dry_run)
                elif action == "delete":
                    out = self._delete(path, age_days, dry_run)
                else:  # recycle
                    out = self._recycle(path, age_days, dry_run)
                out["rule_id"] = rule.get("id")
                results.append(out)
                self.safety.log("clean", path, risk=risk, result=out.get("status", "ok"),
                                detail=out.get("detail", ""), rollback="回收站/本地回收目录")
            except Exception as e:  # noqa: BLE001
                results.append({"path": path, "status": "error", "reason": str(e)})
                self.safety.log("clean", path, risk=risk, result="error", detail=str(e))
        return results

    # ---------- recycle ----------
    def _recycle(self, path: str, age_days: int, dry_run: bool) -> dict:
        if age_days and os.path.isdir(path):
            files = self._collect_files(path, age_days)
            if not files:
                return {"path": path, "status": "skipped", "detail": f"无早于{age_days}天的文件"}
            moved = 0
            for f in files:
                if self._recycle_one(f, dry_run):
                    moved += 1
            return {"path": path, "status": "ok", "detail": f"回收 {moved} 个文件", "dry": dry_run}
        ok = self._recycle_one(path, dry_run)
        return {"path": path, "status": "ok" if ok else "error", "dry": dry_run}

    def _recycle_one(self, path: str, dry_run: bool) -> bool:
        if dry_run:
            return True
        if IS_WIN:
            return self._recycle_windows(path)
        return self._recycle_trash(path)

    @staticmethod
    def _recycle_windows(path: str) -> bool:
        try:
            import ctypes
            from ctypes import wintypes

            class SHFILEOPSTRUCTW(ctypes.Structure):
                _fields_ = [
                    ("hwnd", wintypes.HWND),
                    ("wFunc", wintypes.UINT),
                    ("pFrom", ctypes.c_wchar_p),
                    ("pTo", ctypes.c_wchar_p),
                    ("fFlags", wintypes.UINT),
                    ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p),
                    ("lpszProgressTitle", ctypes.c_wchar_p),
                ]

            struc = SHFILEOPSTRUCTW()
            struc.hwnd = 0
            struc.wFunc = FO_DELETE
            struc.pFrom = path + "\0\0"
            struc.pTo = None
            struc.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION
            res = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(struc))
            return res == 0
        except Exception:  # noqa: BLE001
            return Cleaner._recycle_trash(path)

    @staticmethod
    def _recycle_trash(path: str) -> bool:
        """非 Windows / 无回收站时的可恢复兜底：移动到本地 .trash。"""
        trash = os.path.join(os.path.dirname(__file__), "..", "data", ".trash")
        os.makedirs(trash, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        dest = os.path.join(trash, f"{os.path.basename(path)}_{stamp}")
        try:
            if os.path.isdir(path):
                shutil.move(path, dest)
            else:
                shutil.move(path, dest)
            return True
        except Exception:  # noqa: BLE001
            return False

    # ---------- 硬删除 ----------
    def _delete(self, path: str, age_days: int, dry_run: bool) -> dict:
        if dry_run:
            return {"path": path, "status": "ok", "dry": True, "detail": "硬删除预览"}
        if age_days and os.path.isdir(path):
            for f in self._collect_files(path, age_days):
                try:
                    os.remove(f)
                except OSError:
                    pass
            return {"path": path, "status": "ok", "detail": "按年龄硬删除"}
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            return {"path": path, "status": "ok", "detail": "硬删除"}
        except OSError as e:
            return {"path": path, "status": "error", "reason": str(e)}

    # ---------- 系统 API 通道 ----------
    def _system_api(self, cmd: str, dry_run: bool) -> dict:
        if dry_run:
            return {"path": cmd, "status": "ok", "dry": True, "detail": f"system_api:{cmd}"}
        if not IS_WIN:
            return {"path": cmd, "status": "skipped", "detail": "非 Windows，跳过系统清理"}
        if cmd == "stop_wuauserv":
            return self._clean_updater_cache()
        if cmd == "dism_startcomponentcleanup":
            return self._run(["DISM", "/Online", "/Cleanup-Image", "/StartComponentCleanup"], "WinSxS")
        if cmd == "powercfg_hibernate_off":
            return self._run(["powercfg", "-h", "off"], "休眠文件")
        return {"path": cmd, "status": "skipped", "detail": "未知系统命令"}

    @staticmethod
    def _clean_updater_cache() -> dict:
        dl = os.path.join(os.environ.get("SystemRoot", "C:\\Windows"), "SoftwareDistribution", "Download")
        try:
            subprocess.run(["net", "stop", "wuauserv"], capture_output=True, text=True, check=False)
            subprocess.run(["net", "stop", "bits"], capture_output=True, text=True, check=False)
            if os.path.isdir(dl):
                shutil.rmtree(dl, ignore_errors=True)
                os.makedirs(dl, exist_ok=True)
            subprocess.run(["net", "start", "wuauserv"], capture_output=True, text=True, check=False)
            subprocess.run(["net", "start", "bits"], capture_output=True, text=True, check=False)
            return {"path": dl, "status": "ok", "detail": "更新缓存已清理"}
        except Exception as e:  # noqa: BLE001
            return {"path": dl, "status": "error", "reason": str(e)}

    @staticmethod
    def _run(cmd: list[str], label: str) -> dict:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, check=False)
            return {"path": label, "status": "ok" if r.returncode == 0 else "error",
                    "detail": (r.stdout or r.stderr)[:200]}
        except Exception as e:  # noqa: BLE001
            return {"path": label, "status": "error", "reason": str(e)}

    # ---------- 工具 ----------
    @staticmethod
    def _collect_files(dirpath: str, age_days: int) -> list[str]:
        cutoff = time.time() - age_days * 86400
        out = []
        for root, dirs, files in os.walk(dirpath):
            for f in files:
                p = os.path.join(root, f)
                try:
                    if os.path.getmtime(p) < cutoff:
                        out.append(p)
                except OSError:
                    continue
        return out
