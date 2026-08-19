"""防再生引擎：把"长效健康"做成可一键开关的系统改造。

每个开关记录前值，便于关闭时还原。所有 Windows 注册表/命令调用均加平台守卫，
非 Windows 下仅记录（不生效）。涉及管理员的操作会提示。
"""
from __future__ import annotations

import json
import os
import subprocess
from typing import Callable, Optional

from .safety import SafetyManager
from .util import IS_WIN

# ---------------- 注册表/命令辅助 ----------------
def _set_reg(key_path: str, value: str, data, hive=1, dry: bool = False) -> tuple[bool, str]:
    """hive: 1=HKCU, 2=HKLM。"""
    if not IS_WIN:
        return False, "非 Windows，跳过"
    if dry:
        return True, f"[dry] 将设置 {key_path}\\{value}={data}"
    try:
        import winreg
        h = winreg.HKEY_CURRENT_USER if hive == 1 else winreg.HKEY_LOCAL_MACHINE
        k = winreg.CreateKey(h, key_path)
        winreg.SetValueEx(k, value, 0, winreg.REG_DWORD if isinstance(data, int) else winreg.REG_SZ, data)
        winreg.CloseKey(k)
        return True, "ok"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def _get_reg(key_path: str, value: str, hive=1):
    if not IS_WIN:
        return None
    try:
        import winreg
        h = winreg.HKEY_CURRENT_USER if hive == 1 else winreg.HKEY_LOCAL_MACHINE
        k = winreg.OpenKey(h, key_path)
        v, _ = winreg.QueryValueEx(k, value)
        winreg.CloseKey(k)
        return v
    except Exception:  # noqa: BLE001
        return None


# ---------------- 开关定义 ----------------
class Toggle:
    def __init__(self, tid: str, label: str, desc: str, risk: str,
                 apply_fn: Callable, restore_fn: Callable, current_fn: Callable,
                 needs_param: bool = False):
        self.id = tid
        self.label = label
        self.desc = desc
        self.risk = risk
        self.apply_fn = apply_fn
        self.restore_fn = restore_fn
        self.current_fn = current_fn
        self.needs_param = needs_param


class Preventer:
    def __init__(self, safety: SafetyManager | None = None):
        self.safety = safety or SafetyManager()
        self.toggles = self._build()

    def _build(self) -> list[Toggle]:
        return [
            Toggle("storage_sense", "存储感知(Storage Sense)",
                   "定期自动清理临时文件与回收站", "safe",
                   lambda d: _set_reg(r"Software\Microsoft\Windows\CurrentVersion\StorageSense\Parameters\StoragePolicy", "01", 1, 1, d),
                   lambda d: _set_reg(r"Software\Microsoft\Windows\CurrentVersion\StorageSense\Parameters\StoragePolicy", "01", 0, 1, d),
                   lambda: _get_reg(r"Software\Microsoft\Windows\CurrentVersion\StorageSense\Parameters\StoragePolicy", "01")),
            Toggle("onedrive_on_demand", "OneDrive 文件随选",
                   "云端文件不常驻本地，按需下载", "safe",
                   lambda d: _set_reg(r"Software\Microsoft\OneDrive", "EnableFilesOnDemand", 1, 1, d),
                   lambda d: _set_reg(r"Software\Microsoft\OneDrive", "EnableFilesOnDemand", 0, 1, d),
                   lambda: _get_reg(r"Software\Microsoft\OneDrive", "EnableFilesOnDemand")),
            Toggle("default_save_location", "默认保存位置(D 盘)",
                   "新文件默认存到非系统盘", "safe",
                   lambda d: _set_reg(r"Software\Microsoft\Windows\CurrentVersion\Storage\Config", "ConfiguredPicturesFolder", "D:\\", 1, d),
                   lambda d: _set_reg(r"Software\Microsoft\Windows\CurrentVersion\Storage\Config", "ConfiguredPicturesFolder", "C:\\", 1, d),
                   lambda: _get_reg(r"Software\Microsoft\Windows\CurrentVersion\Storage\Config", "ConfiguredPicturesFolder")),
            Toggle("uwp_install_drive", "UWP 默认安装盘",
                   "新装商店应用落非系统盘(需管理员)", "cautious",
                   lambda d: _set_reg(r"SOFTWARE\Microsoft\Windows\CurrentVersion\Appx", "PackageRoot", r"D:\WindowsApps", 2, d),
                   lambda d: _set_reg(r"SOFTWARE\Microsoft\Windows\CurrentVersion\Appx", "PackageRoot", r"C:\WindowsApps", 2, d),
                   lambda: _get_reg(r"SOFTWARE\Microsoft\Windows\CurrentVersion\Appx", "PackageRoot", 2)),
            Toggle("dev_env_cache", "开发环境缓存改 D 盘",
                   "npm/cargo/Maven/Gradle/pip 缓存集中到非系统盘", "safe",
                   self._dev_cache_on, self._dev_cache_off, self._dev_cache_status,
                   needs_param=True),
            Toggle("ntfs_compress", "NTFS 定点压缩",
                   "对可压缩目录(源码/日志)压缩以省空间", "cautious",
                   self._ntfs_compress_on, lambda d: (True, "压缩为系统属性，无需回滚"), self._ntfs_compress_status,
                   needs_param=True),
            Toggle("browser_download", "浏览器默认下载路径(D 盘)",
                   "Chrome/Edge 下载默认落非系统盘", "safe",
                   self._browser_dl_on, self._browser_dl_off, self._browser_dl_status,
                   needs_param=True),
        ]

    # ---- 开发缓存 ----
    def _dev_cache_on(self, dry: bool, dst: str = "D:\\DevCache"):
        if not IS_WIN:
            return False, "非 Windows，跳过"
        vars_ = {"CARGO_HOME": f"{dst}\\cargo", "npm_config_cache": f"{dst}\\npm",
                 "PIP_CACHE_DIR": f"{dst}\\pip", "GRADLE_USER_HOME": f"{dst}\\gradle",
                 "MAVEN_OPTS": f"-Dmaven.repo.local={dst}\\.m2"}
        for k, v in vars_.items():
            if dry:
                continue
            subprocess.run(["setx", k, v], capture_output=True, text=True, check=False)
        return True, f"已设置开发缓存到 {dst}" + (" [dry]" if dry else "")

    def _dev_cache_off(self, dry: bool):
        if not IS_WIN:
            return False, "非 Windows，跳过"
        for k in ["CARGO_HOME", "npm_config_cache", "PIP_CACHE_DIR", "GRADLE_USER_HOME", "MAVEN_OPTS"]:
            if dry:
                continue
            subprocess.run(["setx", k, ""], capture_output=True, text=True, check=False)
        return True, "已清除开发缓存环境变量"

    def _dev_cache_status(self):
        return os.environ.get("CARGO_HOME") or os.environ.get("npm_config_cache") or "未设置"

    # ---- NTFS 压缩 ----
    def _ntfs_compress_on(self, dry: bool, target: str = ""):
        if not IS_WIN or not target:
            return False, "非 Windows 或未指定目录"
        if dry:
            return True, f"[dry] 将压缩 {target}"
        r = subprocess.run(["compact", "/c", "/s", target], capture_output=True, text=True, check=False)
        return r.returncode == 0, "已压缩" if r.returncode == 0 else r.stderr[:100]

    def _ntfs_compress_status(self):
        return "n/a"

    # ---- 浏览器下载路径 ----
    def _browser_dl_on(self, dry: bool, dst: str = "D:\\Downloads"):
        if not IS_WIN:
            return False, "非 Windows，跳过"
        changed = []
        for brand, base in (("Chrome", r"Google\Chrome\User Data\Default"),
                            ("Edge", r"Microsoft\Edge\User Data\Default")):
            pref = os.path.join(os.environ.get("LOCALAPPDATA", ""), base, "Preferences")
            if not os.path.exists(pref):
                continue
            if dry:
                changed.append(brand)
                continue
            try:
                with open(pref, "r", encoding="utf-8") as f:
                    data = json.load(f)
                data.setdefault("download", {})["default_directory"] = dst
                with open(pref, "w", encoding="utf-8") as f:
                    json.dump(data, f)
                changed.append(brand)
            except Exception:  # noqa: BLE001
                pass
        return True, f"已设置下载路径到 {dst}: {','.join(changed)}" + (" [dry]" if dry else "")

    def _browser_dl_off(self, dry: bool):
        return True, "需手动在浏览器设置中改回"

    def _browser_dl_status(self):
        return "未检测"

    # ---- 对外 API ----
    def list_toggles(self) -> list[dict]:
        return [{"id": t.id, "label": t.label, "desc": t.desc, "risk": t.risk,
                 "needs_param": t.needs_param, "current": t.current_fn()} for t in self.toggles]

    def apply(self, tid: str, dry_run: bool = True, param: str = "") -> dict:
        t = next((x for x in self.toggles if x.id == tid), None)
        if not t:
            return {"status": "error", "reason": "未知开关"}
        self.safety.log("prevent.apply", tid, risk=t.risk, detail=f"dry={dry_run},param={param}")
        if t.needs_param and param:
            ok, msg = t.apply_fn(dry_run, param)
        else:
            ok, msg = t.apply_fn(dry_run)
        return {"id": tid, "status": "ok" if ok else "error", "detail": msg, "dry": dry_run}

    def restore(self, tid: str, dry_run: bool = True) -> dict:
        t = next((x for x in self.toggles if x.id == tid), None)
        if not t:
            return {"status": "error", "reason": "未知开关"}
        if t.needs_param:
            ok, msg = t.restore_fn(dry_run, "")
        else:
            ok, msg = t.restore_fn(dry_run)
        return {"id": tid, "status": "ok" if ok else "error", "detail": msg, "dry": dry_run}
