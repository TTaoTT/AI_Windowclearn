"""通用配置改写引擎：迁移已知程序后，一键把它们的配置路径指到新位置。

支持的规格（config_patch 列表项）：
  - set_json:    {file, set: {json路径: 模板}}          如 VS Code settings.json 的 extensions.path
  - set_xml:     {file, tag, text: 模板}                如 Maven settings.xml 的 <localRepository>
  - set_props:   {file, key, value: 模板}               如 gradle.properties / .npmrc 的 key=value
  - set_ini:     {file, section, key, value: 模板}       如 Firefox profiles.ini
  - set_tres:    {file, key, value: 模板}               如 Godot editor_settings.tres
  - replace_text:{file 或 glob, }                        把文件里的旧路径(<OLD>)替换为新路径(<NEW>)
  - env:         {name, value: 模板}                     写入用户环境变量（HKCU\\Environment / setx）

模板里的 <NEW> 会被替换为目标路径，<OLD> 替换为原路径（处理正反斜杠）。
所有写操作前自动备份原文件；dry_run 仅返回将要做的改动，不落盘。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any

from .util import IS_WIN, expand, nowin_kw


class ConfigPatcher:
    def __init__(self, safety=None):
        self.safety = safety

    # ---------- 对外入口 ----------
    def apply(self, specs: list[dict], src: str, dst: str, dry_run: bool = True,
              rebases: list[tuple[str, str]] | None = None) -> list[dict]:
        """rebases: [(旧前缀, 新前缀), ...]，用于迁移后把"源内"的配置文件路径改指到 dst。"""
        self._rebases = rebases or []
        out = []
        for spec in (specs or []):
            try:
                out.extend(self._apply_one(spec, src, dst, dry_run))
            except Exception as e:  # noqa: BLE001
                out.append({"type": spec.get("type", "?"), "status": "error",
                            "detail": f"{spec.get('file', spec.get('name', ''))}: {e}"})
        return out

    def describe(self, specs: list[dict], src: str, dst: str,
                 rebases: list[tuple[str, str]] | None = None) -> list[str]:
        """返回人类可读的"哪些配置会被改"清单（用于预览对话框）。"""
        self._rebases = rebases or []
        lines = []
        for spec in (specs or []):
            lines.extend(self._describe_one(spec, src, dst))
        return lines or ["（该方式无需修改任何配置文件，junction 对程序完全透明）"]

    def _rebase(self, path: str) -> str:
        for old, new in getattr(self, "_rebases", []):
            if path.startswith(old):
                return new + path[len(old):]
        return path

    # ---------- 占位符 ----------
    @staticmethod
    def _render(template: str, src: str, dst: str) -> str:
        return (template or "").replace("<NEW>", dst).replace("<OLD>", src)

    # ---------- 单条规格 ----------
    def _apply_one(self, spec: dict, src: str, dst: str, dry_run: bool) -> list[dict]:
        t = spec.get("type")
        if t == "set_json":
            return [self._set_json(spec["file"], spec.get("set", {}), src, dst, dry_run)]
        if t == "set_xml":
            return [self._set_xml(spec["file"], spec["tag"], spec["text"], src, dst, dry_run)]
        if t == "set_props":
            return [self._set_kv(spec["file"], spec["key"], spec["value"], src, dst, dry_run, sep="=")]
        if t == "set_ini":
            return [self._set_ini(spec["file"], spec["section"], spec["key"],
                                  spec["value"], src, dst, dry_run)]
        if t == "set_tres":
            return [self._set_kv(spec["file"], spec["key"], spec["value"], src, dst, dry_run, sep="=")]
        if t == "replace_text":
            return self._replace_text(spec, src, dst, dry_run)
        if t == "env":
            return [self._set_env(spec["name"], spec["value"], src, dst, dry_run)]
        return [{"type": t or "?", "status": "skipped", "detail": "未知规格"}]

    def _describe_one(self, spec: dict, src: str, dst: str) -> list[str]:
        t = spec.get("type")
        f = spec.get("file")
        if t == "set_json":
            return [f"编辑 {self._xp(f)}：设置 " + "、".join(f"{k} = {self._render(v, src, dst)}"
                                                            for k, v in spec.get("set", {}).items())]
        if t == "set_xml":
            return [f"编辑 {self._xp(f)}：设置 <{spec['tag']}> = {self._render(spec['text'], src, dst)}"]
        if t == "set_props":
            return [f"编辑 {self._xp(f)}：设置 {spec['key']} = {self._render(spec['value'], src, dst)}"]
        if t == "set_ini":
            return [f"编辑 {self._xp(f)}：[{spec['section']}] {spec['key']} = {self._render(spec['value'], src, dst)}"]
        if t == "set_tres":
            return [f"编辑 {self._xp(f)}：设置 {spec['key']} = {self._render(spec['value'], src, dst)}"]
        if t == "replace_text":
            targets = self._replace_targets(spec, src)
            return [f"改写 {os.path.basename(t)}：将路径 {src} 替换为 {dst}" for t in targets] or \
                   [f"改写 {self._xp(f)}：将路径 {src} 替换为 {dst}"]
        if t == "env":
            return [f"设置用户环境变量 {spec['name']} = {self._render(spec['value'], src, dst)}（需重新登录生效）"]
        return [f"（未知规格 {t}）"]

    @staticmethod
    def _xp(p):
        return os.path.expandvars(p) if p else p

    def _resolve(self, file_t: str) -> str:
        return self._rebase(expand(file_t))

    # ---------- 备份 ----------
    def _backup(self, path: str) -> None:
        if not os.path.exists(path):
            return
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        bak = f"{path}.cdisk_bak_{stamp}"
        try:
            shutil.copy2(path, bak)
        except OSError:
            pass

    # ---------- set_json ----------
    def _set_json(self, file_t: str, mapping: dict, src: str, dst: str, dry_run: bool) -> dict:
        path = self._resolve(file_t)
        detail = []
        try:
            data = {}
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            new_data = dict(data)
            for k, v in mapping.items():
                val = self._render(v, src, dst)
                self._set_nested(new_data, k.split("."), val)
                detail.append(f"{k}={val}")
            if not dry_run:
                self._backup(path)
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(new_data, f, ensure_ascii=False, indent=2)
            return {"type": "set_json", "file": path, "status": "ok",
                    "detail": "、".join(detail) + ("（预览）" if dry_run else "")}
        except Exception as e:  # noqa: BLE001
            return {"type": "set_json", "file": path, "status": "error", "detail": str(e)}

    @staticmethod
    def _set_nested(d: dict, keys: list[str], val: Any) -> None:
        for k in keys[:-1]:
            d = d.setdefault(k, {})
            if not isinstance(d, dict):
                return
        d[keys[-1]] = val

    # ---------- set_xml ----------
    def _set_xml(self, file_t: str, tag: str, text_t: str, src: str, dst: str, dry_run: bool) -> dict:
        path = self._resolve(file_t)
        text = self._render(text_t, src, dst)
        try:
            if os.path.exists(path):
                tree = ET.parse(path)
                root = tree.getroot()
            else:
                root = ET.Element("settings", {"xmlns": "http://maven.apache.org/SETTINGS/1.0.0"})
                tree = ET.ElementTree(root)
            # 忽略命名空间查找 tag
            found = False
            for el in root.iter():
                if el.tag.split("}")[-1] == tag:
                    el.text = text
                    found = True
            if not found:
                # 简单插入到根下
                child = ET.SubElement(root, tag)
                child.text = text
            if not dry_run:
                self._backup(path)
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                tree.write(path, encoding="utf-8", xml_declaration=True)
            return {"type": "set_xml", "file": path, "status": "ok",
                    "detail": f"<{tag}>={text}" + ("（预览）" if dry_run else "")}
        except Exception as e:  # noqa: BLE001
            return {"type": "set_xml", "file": path, "status": "error", "detail": str(e)}

    # ---------- set_props / set_tres (key=value) ----------
    def _set_kv(self, file_t: str, key: str, value_t: str, src: str, dst: str,
                dry_run: bool, sep: str = "=") -> dict:
        path = self._resolve(file_t)
        value = self._render(value_t, src, dst)
        try:
            lines = []
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    lines = f.readlines()
            pat = re.compile(r"^\s*" + re.escape(key) + r"\s*" + re.escape(sep) + r"\s*")
            replaced = False
            out = []
            for ln in lines:
                if pat.match(ln) and not ln.lstrip().startswith("#"):
                    out.append(f"{key}{sep}{value}\n")
                    replaced = True
                else:
                    out.append(ln)
            if not replaced:
                out.append(f"{key}{sep}{value}\n")
            if not dry_run:
                self._backup(path)
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.writelines(out)
            return {"type": "set_kv", "file": path, "status": "ok",
                    "detail": f"{key}{sep}{value}" + ("（预览）" if dry_run else "")}
        except Exception as e:  # noqa: BLE001
            return {"type": "set_kv", "file": path, "status": "error", "detail": str(e)}

    # ---------- set_ini ----------
    def _set_ini(self, file_t: str, section: str, key: str, value_t: str,
                 src: str, dst: str, dry_run: bool) -> dict:
        import configparser
        path = self._resolve(file_t)
        value = self._render(value_t, src, dst)
        try:
            cp = configparser.ConfigParser()
            if os.path.exists(path):
                cp.read(path, encoding="utf-8")
            if not cp.has_section(section):
                cp.add_section(section)
            cp.set(section, key, value)
            if not dry_run:
                self._backup(path)
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    cp.write(f)
            return {"type": "set_ini", "file": path, "status": "ok",
                    "detail": f"[{section}] {key}={value}" + ("（预览）" if dry_run else "")}
        except Exception as e:  # noqa: BLE001
            return {"type": "set_ini", "file": path, "status": "error", "detail": str(e)}

    # ---------- replace_text ----------
    def _replace_targets(self, spec: dict, src: str) -> list[str]:
        if spec.get("file"):
            return [self._resolve(spec["file"])]
        out = []
        base = self._rebase(expand(spec.get("base") or src))
        pat = spec.get("glob", "**/*.ini")
        import fnmatch
        for root, _dirs, files in os.walk(base):
            for fl in files:
                if fnmatch.fnmatch(fl, os.path.basename(pat)):
                    out.append(os.path.join(root, fl))
        return out

    def _replace_text(self, spec: dict, src: str, dst: str, dry_run: bool) -> list[dict]:
        out = []
        targets = self._replace_targets(spec, src)
        for path in targets:
            try:
                if not os.path.exists(path):
                    continue
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                new = content.replace(src, dst).replace(src.replace("\\", "/"), dst.replace("\\", "/"))
                if new == content:
                    continue
                if not dry_run:
                    self._backup(path)
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(new)
                out.append({"type": "replace_text", "file": path, "status": "ok",
                            "detail": f"替换路径 {src} -> {dst}" + ("（预览）" if dry_run else "")})
            except Exception as e:  # noqa: BLE001
                out.append({"type": "replace_text", "file": path, "status": "error", "detail": str(e)})
        return out or [{"type": "replace_text", "file": "（无匹配文件）", "status": "skipped", "detail": ""}]

    # ---------- env ----------
    def _set_env(self, name: str, value_t: str, src: str, dst: str, dry_run: bool) -> dict:
        value = self._render(value_t, src, dst)
        if not IS_WIN:
            return {"type": "env", "name": name, "status": "skipped", "detail": "非 Windows，跳过环境变量"}
        if dry_run:
            return {"type": "env", "name": name, "status": "ok",
                    "detail": f"将设置 {name}={value}（预览）"}
        try:
            r = subprocess.run(["setx", name, value], capture_output=True,
                               encoding="utf-8", errors="replace", check=False,
                               **nowin_kw())
            return {"type": "env", "name": name, "status": "ok" if r.returncode == 0 else "error",
                    "detail": f"已设置 {name}={value}" if r.returncode == 0 else (r.stderr or "")[:120]}
        except Exception as e:  # noqa: BLE001
            return {"type": "env", "name": name, "status": "error", "detail": str(e)}
