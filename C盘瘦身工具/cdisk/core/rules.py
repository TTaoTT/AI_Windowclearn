"""规则引擎：加载 YAML 规则库，按路径匹配分类，内置黑名单硬拦截。

规则文件：
  cdisk/rules/clean_rules.yaml   可清理规则
  cdisk/rules/migrate_rules.yaml 可迁移规则
"""
from __future__ import annotations

import os
from typing import Any

import yaml

from .util import IS_WIN, expand, is_subpath, normalize, pattern_matches

# 不可触碰的红线前缀（Windows 规范化小写）
PROTECTED_PREFIXES = [
    "C:\\Windows\\Installer",
    "C:\\Windows\\System32",
    "C:\\Windows\\Servicing",
    "C:\\Windows\\WinSxS",
    "C:\\Program Files",
    "C:\\Program Files (x86)",
    "C:\\Windows\\System32\\DriverStore",
    os.path.expandvars("%LOCALAPPDATA%\\Packages"),
]

RISK_ORDER = {"safe": 0, "cautious": 1, "danger": 2, "L1": 0, "L2": 1, "L3": 2}


class RuleEngine:
    def __init__(self, rules_dir: str | None = None):
        if rules_dir is None:
            rules_dir = os.path.join(os.path.dirname(__file__), "..", "rules")
        self.rules_dir = os.path.abspath(rules_dir)
        self.clean_rules: list[dict[str, Any]] = []
        self.migrate_rules: list[dict[str, Any]] = []
        self.app_profiles: list[dict[str, Any]] = []
        self.load()

    # ---------- 加载 ----------
    def load(self) -> None:
        self.clean_rules = self._load_file("clean_rules.yaml")
        self.migrate_rules = self._load_file("migrate_rules.yaml")
        self.app_profiles = self._load_file("app_profiles.yaml", key="profiles")

    def _load_file(self, name: str, key: str | None = None) -> list[dict[str, Any]]:
        path = os.path.join(self.rules_dir, name)
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or []
        if isinstance(data, list):
            return data
        return data.get(key or "rules", [])

    # ---------- 匹配 ----------
    def match_clean(self, path: str, is_dir: bool = False) -> list[dict[str, Any]]:
        out = []
        for r in self.clean_rules:
            pats = (r.get("match") or {}).get("paths", [])
            for p in pats:
                if pattern_matches(p, path):
                    out.append(r)
                    break
        return out

    def match_migrate(self, path: str, is_dir: bool = True) -> list[dict[str, Any]]:
        out = []
        for r in self.migrate_rules:
            pats = (r.get("match") or {}).get("paths", [])
            for p in pats:
                if pattern_matches(p, path):
                    out.append(r)
                    break
        return out

    def classify_dir(self, path: str) -> dict[str, list[dict[str, Any]]]:
        """返回某目录命中的清理/迁移规则，供 UI 标签与扫描标记。"""
        return {
            "clean": self.match_clean(path, is_dir=True),
            "migrate": self.match_migrate(path, is_dir=True),
        }

    # ---------- 安全 ----------
    def is_protected(self, path: str) -> bool:
        """该路径是否位于红线前缀内（直接文件增删禁区）。"""
        np = normalize(path)
        for pre in PROTECTED_PREFIXES:
            if is_subpath(np, expand(pre)):
                return True
        # UWP 沙箱整体（按用户展开后判断）
        if IS_WIN:
            pkg = normalize(os.path.expandvars("%LOCALAPPDATA%\\Packages"))
            if is_subpath(np, pkg):
                return True
        return False

    @staticmethod
    def risk_rank(risk: str) -> int:
        return RISK_ORDER.get(risk, 9)

    # ---------- 应用画像匹配 ----------
    def match_app(self, path: str) -> list[dict[str, Any]]:
        """返回某路径命中的已知应用画像（用于按程序归类迁移项）。"""
        np = normalize(path)
        out = []
        for p in self.app_profiles:
            for s in p.get("sources", []):
                if np == expand(s) or is_subpath(np, expand(s)):
                    out.append(p)
                    break
        return out


# 便捷单例
_default: RuleEngine | None = None


def default() -> RuleEngine:
    global _default
    if _default is None:
        _default = RuleEngine()
    return _default
