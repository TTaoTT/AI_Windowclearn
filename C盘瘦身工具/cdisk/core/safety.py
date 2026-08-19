"""安全与回滚管理器：操作日志(SQLite)、系统还原点、白黑名单。

所有"危险操作"应经本管理器登记，保证可审计、可回滚。
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
from datetime import datetime
from typing import Optional

from .util import IS_WIN

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS operations (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    op_type   TEXT NOT NULL,
    target    TEXT NOT NULL,
    size      INTEGER,
    risk      TEXT,
    result    TEXT,
    detail    TEXT,
    rollback  TEXT
);
"""


class SafetyManager:
    def __init__(self, db_path: str | None = None):
        if db_path is None:
            app = os.path.join(os.path.expandvars("%LOCALAPPDATA%"), "CDisk") if IS_WIN \
                else os.path.expanduser("~/.cdisk")
            os.makedirs(app, exist_ok=True)
            db_path = os.path.join(app, "operation_log.db")
        self.db_path = os.path.abspath(db_path)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as con:
            con.execute(DB_SCHEMA)

    # ---------- 日志 ----------
    def log(self, op_type: str, target: str, size: Optional[int] = None,
            risk: str = "", result: str = "ok", detail: str = "",
            rollback: str = "") -> int:
        ts = datetime.now().isoformat(timespec="seconds")
        with sqlite3.connect(self.db_path) as con:
            cur = con.execute(
                "INSERT INTO operations(ts,op_type,target,size,risk,result,detail,rollback)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (ts, op_type, target, size, risk, result, detail, rollback),
            )
            return cur.lastrowid or 0

    def recent(self, limit: int = 100) -> list[dict]:
        with sqlite3.connect(self.db_path) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT * FROM operations ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- 系统还原点 ----------
    def create_restore_point(self, name: str = "C盘瘦身工具") -> tuple[bool, str]:
        """创建系统还原点（需 Windows + 管理员 + pywin32）。失败返回 (False, reason)。"""
        if not IS_WIN:
            return False, "非 Windows 平台，跳过还原点"
        try:
            import win32com.client  # pywin32
        except Exception as e:  # noqa: BLE001
            return False, f"未安装 pywin32({e})，建议手动创建还原点"
        try:
            sr = win32com.client.GetObject("winmgmts:\\\\.\\root\\default:Systemrestore")
            # 0=APPLICATION_INSTALL, 100=MODULE_INSTALL
            sr.CreateRestorePoint(name, 0, 100)
            return True, "已创建系统还原点"
        except Exception as e:  # noqa: BLE001
            return False, f"创建还原点失败: {e}"

    # ---------- 还原点（vssadmin 备选） ----------
    def create_shadow_copy(self, drive: str = "C:") -> tuple[bool, str]:
        if not IS_WIN:
            return False, "非 Windows 平台"
        try:
            subprocess.run(
                ["vssadmin", "create", "shadow", f"/for={drive}"],
                check=True, capture_output=True, text=True,
            )
            return True, "已创建卷影副本"
        except Exception as e:  # noqa: BLE001
            return False, f"卷影副本创建失败: {e}"
