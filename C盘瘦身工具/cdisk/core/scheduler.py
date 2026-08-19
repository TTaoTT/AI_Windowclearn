"""调度引擎：把本程序的 CLI 子命令注册为 Windows 计划任务（定期扫描/报告）。"""
from __future__ import annotations

import subprocess
from typing import Optional

from .safety import SafetyManager
from .util import IS_WIN

CADENCE = {"DAILY": "DAILY", "WEEKLY": "WEEKLY", "MONTHLY": "MONTHLY"}


class Scheduler:
    def __init__(self, safety: SafetyManager | None = None):
        self.safety = safety or SafetyManager()

    def register(self, name: str, cmd: str, cadence: str = "WEEKLY", dry_run: bool = True) -> dict:
        sc = CADENCE.get(cadence.upper(), "WEEKLY")
        self.safety.log("schedule.register", name, detail=f"cadence={sc},cmd={cmd},dry={dry_run}")
        if dry_run:
            return {"status": "ok", "dry": True, "detail": f"[dry] 将注册计划任务 {name}: {cmd}"}
        if not IS_WIN:
            return {"status": "skipped", "detail": "非 Windows，跳过"}
        r = subprocess.run(
            ["schtasks", "/Create", "/SC", sc, "/TN", name, "/TR", cmd, "/F"],
            capture_output=True, text=True, check=False,
        )
        return {"status": "ok" if r.returncode == 0 else "error",
                "detail": (r.stdout or r.stderr)[:200]}

    def remove(self, name: str, dry_run: bool = True) -> dict:
        if dry_run:
            return {"status": "ok", "dry": True, "detail": f"[dry] 将删除计划任务 {name}"}
        if not IS_WIN:
            return {"status": "skipped", "detail": "非 Windows，跳过"}
        r = subprocess.run(["schtasks", "/Delete", "/TN", name, "/F"],
                           capture_output=True, text=True, check=False)
        return {"status": "ok" if r.returncode == 0 else "error",
                "detail": (r.stdout or r.stderr)[:200]}

    def list_tasks(self, name: str = "CDisk*") -> list[str]:
        if not IS_WIN:
            return []
        r = subprocess.run(["schtasks", "/Query", "/TN", name],
                           capture_output=True, text=True, check=False)
        return [l for l in r.stdout.splitlines() if "CDisk" in l]
