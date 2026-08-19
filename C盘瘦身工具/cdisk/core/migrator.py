"""迁移引擎：物理文件搬到非系统盘，原路径用目录联接(junction)/注册表重定向"留替身"。

标准三步（关进程 → 复制 → 建链），全部经安全日志与回滚清单。
L1 走微软官方「用户文件夹重定向」(改注册表)；L2/L3 走 junction(目录联接)。
杀软/驱动/VPN/反作弊类不在迁移规则内（规则层已排除）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime
from typing import Optional

from .rules import RuleEngine
from .safety import SafetyManager
from .util import IS_WIN, normalize, nowin_kw, reparse_target, is_reparse_point
from .config_patcher import ConfigPatcher

# 已知文件夹 -> 注册表 User Shell Folders 值名
SHELL_FOLDER_REG = {
    "Desktop": "Desktop",
    "Documents": "Personal",
    "Pictures": "My Pictures",
    "Videos": "My Video",
    "Music": "My Music",
    "Downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
}


class Migrator:
    def __init__(self, rules: RuleEngine | None = None, safety: SafetyManager | None = None,
                 manifest_path: str | None = None):
        self.rules = rules or RuleEngine.default()
        self.safety = safety or SafetyManager()
        self.patcher = ConfigPatcher(self.safety)
        if manifest_path is None:
            app = os.path.join(os.path.expandvars("%LOCALAPPDATA%"), "CDisk") if IS_WIN \
                else os.path.expanduser("~/.cdisk")
            os.makedirs(app, exist_ok=True)
            manifest_path = os.path.join(app, "junction_manifest.json")
        self.manifest_path = os.path.abspath(manifest_path)

    # ---------- 公共入口 ----------
    def migrate(self, target: str, dst: str, method: str,
                associated_processes: Optional[list[str]] = None,
                pre_command: Optional[str] = None, dry_run: bool = True,
                on_progress=None, cancel_event: Optional[threading.Event] = None) -> dict:
        associated_processes = associated_processes or []
        rid = self.safety.log("migrate.pre", target, detail=f"method={method},dst={dst},dry={dry_run}")
        try:
            if method == "shell_folder_redirect":
                out = self._shell_redirect(target, dst, dry_run, on_progress)
            elif method == "junction":
                out = self._junction(target, dst, associated_processes, pre_command, dry_run,
                                     on_progress, cancel_event)
            else:
                out = {"status": "skipped", "detail": f"未知方法 {method}"}
            out["dry"] = dry_run
            self.safety.log("migrate", target, result=out.get("status", "ok"),
                            detail=out.get("detail", ""), rollback="见 manifest / 删除链接")
            return out
        except Exception as e:  # noqa: BLE001
            self.safety.log("migrate", target, result="error", detail=str(e))
            return {"status": "error", "reason": str(e)}

    # ---------- 已知应用画像迁移 ----------
    def migrate_app(self, profile: dict, dst: str, method: str, dry_run: bool = True,
                    on_progress=None, cancel_event: Optional[threading.Event] = None) -> dict:
        """按应用画像迁移：method=config 走"移动+改配置"，否则走 junction 透明迁移。"""
        srcs = profile.get("sources", [])
        src = next((s for s in (os.path.expandvars(x) for x in srcs) if os.path.exists(s)), None)
        if not src:
            return {"status": "skipped", "detail": "源不存在：" + " / ".join(srcs)}
        if method == "config":
            return self._config_redirect(src, dst, profile, dry_run, on_progress, cancel_event)
        return self._junction(src, dst, profile.get("associated_processes", []),
                              profile.get("pre_command"), dry_run, on_progress, cancel_event)

    def config_rebases(self, profile: dict, dst: str) -> list[tuple[str, str]]:
        """计算"源内配置文件 -> dst"的重映射，用于迁移后改写仍指向旧路径的配置。"""
        dst_root = os.path.dirname(dst)
        rebases = []
        for s in profile.get("sources", []):
            se = os.path.expandvars(s)
            rebases.append((normalize(se),
                            normalize(os.path.join(dst_root, os.path.basename(se.rstrip("\\"))))))
        return rebases

    def _config_redirect(self, src: str, dst: str, profile: dict, dry_run: bool,
                         on_progress=None, cancel_event: Optional[threading.Event] = None) -> dict:
        """移动文件夹到 dst，并改写该程序配置使其指向新位置（无需 junction）。"""
        if self.rules.is_protected(src):
            return {"status": "blocked", "reason": "红线目录，禁止迁移"}
        if is_reparse_point(src):
            return {"status": "skipped", "detail": "该目录已是级联(已重定向)目录，禁止再次迁移"}
        if not os.path.exists(src):
            return {"status": "skipped", "detail": "源不存在"}
        # 先算重映射（此时源还存在），否则删除后路径失效导致改写到旧位置
        rebases = self.config_rebases(profile, dst)
        if on_progress:
            on_progress("close", 0, 1, "关闭相关进程")
        for p in profile.get("associated_processes", []):
            self._stop_proc(p, dry_run)
        if not dry_run:
            if on_progress:
                on_progress("copy", 0, 1, "复制文件到目标盘")
            self._copy(src, dst, dry_run=False, on_progress=on_progress, cancel_event=cancel_event)
            if on_progress:
                on_progress("delete", 0, 1, "删除源目录")
            try:
                shutil.rmtree(src, ignore_errors=False)
            except Exception as e:  # noqa: BLE001
                return {"status": "error", "reason": f"删除源失败: {e}"}
        if on_progress:
            on_progress("patch", 0, 1, "改写程序配置指向新位置")
        patch_results = self.patcher.apply(profile.get("config_patch"), src, dst, dry_run, rebases)
        self._record(src, dst, "config_redirect")
        ok = all(r.get("status") == "ok" for r in patch_results) if patch_results else True
        if on_progress:
            on_progress("done", 1, 1, "完成")
        return {"status": "ok" if ok else "partial", "detail": "已移动并改写配置",
                "patch": patch_results, "dry": dry_run}

    # ---------- junction 路径 ----------
    def _junction(self, src: str, dst: str, procs: list[str], pre_command: Optional[str],
                  dry_run: bool, on_progress=None,
                  cancel_event: Optional[threading.Event] = None) -> dict:
        if self.rules.is_protected(src):
            return {"status": "blocked", "reason": "红线目录，禁止迁移"}
        if is_reparse_point(src):
            return {"status": "skipped", "detail": "该目录已是级联(已重定向)目录，禁止再次迁移"}
        if not os.path.exists(src):
            return {"status": "skipped", "detail": "源不存在"}
        if pre_command and not dry_run and IS_WIN:
            subprocess.run(pre_command, shell=True, capture_output=True, text=True, check=False,
                           encoding="utf-8", errors="replace", **nowin_kw())
        if on_progress:
            on_progress("close", 0, 1, "关闭相关进程")
        for p in procs:
            self._stop_proc(p, dry_run)
        # 复制（带进度）
        if on_progress:
            on_progress("copy", 0, 1, "复制文件到目标盘")
        self._copy(src, dst, dry_run, on_progress=on_progress, cancel_event=cancel_event)
        if dry_run:
            return {"status": "ok", "detail": f"[dry] 将复制 {src} -> {dst} 后建 junction"}
        if on_progress:
            on_progress("delete", 0, 1, "删除源目录")
        try:
            shutil.rmtree(src, ignore_errors=False)
        except Exception as e:  # noqa: BLE001
            return {"status": "error", "reason": f"删除源失败: {e}"}
        if on_progress:
            on_progress("link", 0, 1, "建立目录联接(junction)")
        ok, err = self._mklink_junction(src, dst)
        if not ok:
            # 安全兜底：数据已完整复制到 dst，尝试移回原位置，避免程序路径悬空
            restore = ""
            try:
                if os.path.exists(dst) and not os.path.lexists(src):
                    shutil.move(dst, src)
                    restore = "；已把数据移回原位置"
                elif os.path.exists(dst):
                    restore = f"；数据仍在 {dst}，未回移"
            except Exception as e:  # noqa: BLE001
                restore = f"；数据回移失败（{e}），文件仍在 {dst}"
            return {"status": "error",
                    "reason": f"建立目录联接失败: {err}{restore}"}
        self._record(src, dst, "junction")
        # 自检
        verify = self._verify(src)
        if on_progress:
            on_progress("done", 1, 1, "迁移完成")
        return {"status": "ok", "detail": "已迁移并建立 junction", "verify": verify}

    @staticmethod
    def _stop_proc(name: str, dry_run: bool) -> None:
        if dry_run or not IS_WIN:
            return
        subprocess.run(["taskkill", "/F", "/IM", name], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", check=False, **nowin_kw())

    @staticmethod
    def _dir_size(path: str) -> int:
        """统计目录下文件总字节数（用于复制进度分母）。"""
        total = 0
        for root, _dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        return total

    @classmethod
    def _copy(cls, src: str, dst: str, dry_run: bool = False,
              on_progress=None, cancel_event: Optional[threading.Event] = None) -> int:
        """带进度的 Python 复制（无 DOS 弹窗）。进度回调 cb(phase, done, total, msg)。

        相比 robocopy：进度透明、可取消、跨平台；用 copy2 保留时间戳等属性。
        """
        if dry_run:
            return 0
        total = cls._dir_size(src)
        if os.path.exists(dst):
            shutil.rmtree(dst, ignore_errors=True)
        os.makedirs(dst, exist_ok=True)
        src_abs = os.path.abspath(src)
        dst_abs = os.path.abspath(dst)
        done = 0
        n = 0
        last = 0.0
        for root, _dirs, files in os.walk(src_abs):
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("已取消")
            rel = os.path.relpath(root, src_abs)
            target_dir = dst_abs if rel == "." else os.path.join(dst_abs, rel)
            os.makedirs(target_dir, exist_ok=True)
            for f in files:
                if cancel_event is not None and cancel_event.is_set():
                    raise RuntimeError("已取消")
                sp = os.path.join(root, f)
                dp = os.path.join(target_dir, f)
                try:
                    shutil.copy2(sp, dp)
                except OSError as e:
                    raise RuntimeError(f"复制失败（目标盘可能空间不足或权限不足）: {e}") from e
                try:
                    done += os.path.getsize(sp)
                except OSError:
                    pass
                n += 1
                if on_progress is not None:
                    now = time.monotonic()
                    if n % 25 == 0 or (now - last) > 0.12:
                        last = now
                        on_progress("copy", done, total, f"已复制 {n} 个文件")
        if on_progress is not None:
            on_progress("copy", total, total, "复制完成")
        return 0

    @staticmethod
    def _mklink_junction(src: str, dst: str) -> tuple[bool, str]:
        """创建目录联接(junction)。优先 _winapi.CreateJunction（原生 API：无 cmd 弹窗、
        无编码问题、不依赖 shell 解析），失败回退 cmd mklink。带重试与真实错误信息。

        返回 (ok, error_detail)。
        """
        if not IS_WIN:
            return False, "非 Windows"
        # junction 必须建在"不存在的路径"上；若残留则强制清掉再试
        if os.path.lexists(src):
            try:
                if is_reparse_point(src):
                    # 残留的是链接：只用 rd 删链接本身，绝不 rmtree（否则会顺着链接删掉目标盘数据）
                    subprocess.run(["cmd", "/c", "rd", f'"{src}"'],
                                   capture_output=True, text=True, check=False,
                                   encoding="utf-8", errors="replace", **nowin_kw())
                else:
                    shutil.rmtree(src, ignore_errors=True)
            except Exception:  # noqa: BLE001
                pass
            if os.path.lexists(src):
                return False, f"源路径仍存在（{src}），无法在其上建立联接"
        last_err = ""
        for attempt in range(3):
            if attempt:
                time.sleep(0.4)
            # 1) 原生 API（无子进程）。注意实测参数顺序：第一参=目标目录(被指向)，第二参=要创建的链接路径
            try:
                import _winapi
                _winapi.CreateJunction(dst, src)
                return True, ""
            except Exception as e1:  # noqa: BLE001
                last_err = f"CreateJunction: {e1}"
            # 2) 回退 cmd mklink
            try:
                r = subprocess.run(
                    ["cmd", "/c", "mklink", "/J", f'"{src}"', f'"{dst}"'],
                    capture_output=True, text=True, check=False,
                    encoding="utf-8", errors="replace", **nowin_kw(),
                )
                if r.returncode == 0:
                    return True, ""
                last_err = (r.stderr or r.stdout or "").strip()[:200] or f"mklink rc={r.returncode}"
            except Exception as e2:  # noqa: BLE001
                last_err = f"{last_err}; cmd: {e2}"
        return False, last_err

    @staticmethod
    def _verify(link_path: str) -> bool:
        try:
            # 通过链接访问目标应成功
            os.listdir(link_path)
            return True
        except Exception:  # noqa: BLE001
            return False

    # ---------- shell 文件夹重定向（L1） ----------
    def _shell_redirect(self, target: str, dst: str, dry_run: bool,
                        on_progress=None) -> dict:
        name = os.path.basename(os.path.normpath(target))
        reg_name = SHELL_FOLDER_REG.get(name)
        if not reg_name:
            return {"status": "skipped", "detail": f"未知已知文件夹 {name}"}
        if dry_run:
            return {"status": "ok", "detail": f"[dry] 将注册表 {reg_name} 指向 {dst} 并迁移数据"}
        if not IS_WIN:
            return {"status": "skipped", "detail": "非 Windows，仅记录"}
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
                0, winreg.KEY_SET_VALUE,
            )
            winreg.SetValueEx(key, reg_name, 0, winreg.REG_EXPAND_SZ, dst)
            winreg.CloseKey(key)
        except Exception as e:  # noqa: BLE001
            return {"status": "error", "reason": f"注册表写入失败: {e}"}
        # 迁移现有数据
        if on_progress:
            on_progress("copy", 0, 1, "复制数据到新位置")
        self._copy(target, dst, dry_run=False, on_progress=on_progress)
        self._record(target, dst, "shell_redirect")
        if on_progress:
            on_progress("done", 1, 1, "完成")
        return {"status": "ok", "detail": f"已重定向 {name} -> {dst}（建议重启资源管理器生效）"}

    # ---------- 回滚 ----------
    def rollback(self, src: str, dry_run: bool = False) -> dict:
        entry = self._find_entry(src)
        if not entry:
            return {"status": "skipped", "detail": "manifest 中无此源"}
        if entry.get("type") == "junction":
            if dry_run:
                return {"status": "ok", "detail": f"[dry] 将删除链接 {src}（数据仍在 {entry['dst']}）"}
            if IS_WIN:
                subprocess.run(["cmd", "/c", "rmdir", f'"{src}"'], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", check=False, **nowin_kw())
            self._remove_entry(src)
            return {"status": "ok", "detail": "已删除 junction，数据保留在目标盘"}
        return {"status": "skipped", "detail": "shell_redirect 回滚需手动改回注册表"}

    # ---------- manifest ----------
    def _record(self, src: str, dst: str, typ: str) -> None:
        data = self._load_manifest()
        data = [d for d in data if d.get("src") != src]
        data.append({"src": src, "dst": dst, "type": typ,
                     "created": datetime.now().isoformat(timespec="seconds")})
        with open(self.manifest_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _find_entry(self, src: str) -> Optional[dict]:
        for d in self._load_manifest():
            if d.get("src") == src:
                return d
        return None

    def _remove_entry(self, src: str) -> None:
        data = [d for d in self._load_manifest() if d.get("src") != src]
        with open(self.manifest_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _load_manifest(self) -> list[dict]:
        if not os.path.exists(self.manifest_path):
            return []
        try:
            with open(self.manifest_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            return []
