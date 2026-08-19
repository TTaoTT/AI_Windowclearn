"""主窗口：扫描 / 清理 / 迁移 / 防再生 / 报告 五页，调用 core 引擎。

所有危险操作默认 dry_run 预览，确认后执行；执行前可创建系统还原点。
"""
from __future__ import annotations

import os
import subprocess
import threading
from PySide6.QtCore import QThread, Signal, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel,
    QMainWindow, QMenu, QMessageBox, QPushButton, QProgressBar, QPlainTextEdit,
    QSizePolicy, QSplitter, QTableWidget, QTableWidgetItem, QLineEdit,
    QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

from ..core.cleaner import Cleaner
from ..core.migrator import Migrator
from ..core.preventer import Preventer
from ..core.reporter import Reporter
from ..core.rules import RuleEngine
from ..core.safety import SafetyManager
from ..core.scanner import Node, Scanner, ScanCancelled
from ..core.scheduler import Scheduler
from ..core.util import IS_WIN, drives, human_size, normalize, nowin_kw, is_reparse_point
from .treemap import TreemapWidget


class Worker(QThread):
    done = Signal(object)
    progress = Signal(str)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self):
        try:
            self.done.emit(self.fn())
        except Exception as e:  # noqa: BLE001
            self.done.emit({"error": str(e)})


class ScanWorker(QThread):
    """专用扫描线程：把扫描引擎的进度/日志/完成/取消信号转发到主线程。"""

    progress = Signal(dict)
    logmsg = Signal(str)
    done = Signal(object)
    cancelled = Signal()

    def __init__(self, rules, root, pause_event, cancel_event):
        super().__init__()
        self.rules = rules
        self.root = root
        self.pause_event = pause_event
        self.cancel_event = cancel_event

    def run(self):
        try:
            def on_progress(info):
                self.progress.emit(info)
                phase_map = {"scan": "扫描目录", "measure": "统计大小",
                             "tag": "分析标签", "done": "完成"}
                label = phase_map.get(info["phase"], info["phase"])
                self.logmsg.emit(
                    f"[{label}] {info['path']}  | 目录 {info['dirs']} "
                    f"文件 {info['files']} 累计 {human_size(info['size'])}"
                )

            node = Scanner(self.rules).scan(
                self.root, tag=True,
                on_progress=on_progress,
                pause_event=self.pause_event,
                cancel_event=self.cancel_event,
            )
            self.done.emit(node)
        except ScanCancelled:
            self.cancelled.emit()
        except Exception as e:  # noqa: BLE001
            self.done.emit({"error": str(e)})


class MigrateWorker(QThread):
    """迁移线程：逐项执行，转发进度/日志/完成；支持取消（迁移中不再弹 DOS 窗）。"""

    progress = Signal(int, str)   # 百分比, 说明文字
    log = Signal(str)
    done = Signal(object)

    def __init__(self, migrator, picks, cancel_event):
        super().__init__()
        self.migrator = migrator
        self.picks = picks          # [(c, method, target)]
        self.cancel_event = cancel_event

    def run(self):
        results = []
        total = len(self.picks)
        for i, (c, method, target) in enumerate(self.picks):
            if self.cancel_event.is_set():
                results.append({"status": "cancelled", "detail": "用户取消"})
                break

            def cb(phase, done, tot, msg):
                base = int(100.0 * i / total)
                step = int(70.0 * done / max(tot, 1))
                self.progress.emit(min(99, base + step),
                                   f"[{i + 1}/{total}] {phase} {msg}")
                self.log.emit(f"[{i + 1}/{total}] {phase}: {msg}")

            self.log.emit(f"[{i + 1}/{total}] 开始迁移「{c['name']}」 → {target}（{method}）")
            try:
                if c["kind"] == "app":
                    res = self.migrator.migrate_app(
                        c["obj"], target, method, dry_run=False,
                        on_progress=cb, cancel_event=self.cancel_event)
                else:
                    res = self.migrator.migrate(
                        c["src"], target, method,
                        c["obj"].get("associated_processes", []),
                        c["obj"].get("pre_command"), dry_run=False,
                        on_progress=cb, cancel_event=self.cancel_event)
            except Exception as e:  # noqa: BLE001
                res = {"status": "error", "reason": str(e)}
            res = dict(res)
            res["src"] = c["src"]
            res["dst"] = target
            results.append(res)
            self.log.emit(f"[{i + 1}/{total}] 「{c['name']}」 → {res.get('status', '?')}: "
                          f"{res.get('detail') or res.get('reason') or ''}")
            self.progress.emit(int(100.0 * (i + 1) / total), f"{i + 1}/{total} 项完成")
        self.done.emit(results)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("C 盘瘦身工具 v0.1")
        self.resize(1080, 720)
        self.setMinimumSize(960, 640)
        self.rules = RuleEngine()
        self.safety = SafetyManager()
        self.cleaner = Cleaner(self.rules, self.safety)
        self.migrator = Migrator(self.rules, self.safety)
        self.preventer = Preventer(self.safety)
        self.reporter = Reporter()
        self.scheduler = Scheduler(self.safety)
        self.root: Node | None = None
        self._node_index: dict[str, Node] = {}  # 规范化路径 -> 节点（用于按扫描结果取大小）

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        self._build_analysis()
        self._build_clean()
        self._build_migrate()
        self._build_prevent()
        self._build_report()
        self._build_status()
        self.tabs.currentChanged.connect(self._on_tab)

    # ---------------- 状态栏 ----------------
    def _build_status(self):
        self.status = QLabel("就绪")
        self.addToolBar("main").addWidget(self.status)

    # ---------------- 分析页 ----------------
    def _build_analysis(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(8, 8, 8, 8)
        v.setSpacing(6)

        # 控制栏
        bar = QHBoxLayout()
        bar.addWidget(QLabel("盘符:"))
        self.drive = QComboBox()
        self.drive.addItems(drives())
        bar.addWidget(self.drive)
        self.scan_btn = QPushButton("扫描")
        self.scan_btn.clicked.connect(self._scan)
        bar.addWidget(self.scan_btn)
        self.pause_btn = QPushButton("暂停")
        self.pause_btn.setEnabled(False)
        self.pause_btn.clicked.connect(self._scan_pause)
        bar.addWidget(self.pause_btn)
        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._scan_cancel)
        bar.addWidget(self.cancel_btn)
        self.back_btn = QPushButton("← 返回上级")
        self.back_btn.setEnabled(False)
        self.back_btn.clicked.connect(self._drill_up)
        bar.addWidget(self.back_btn)
        self.open_btn = QPushButton("在资源管理器中打开")
        self.open_btn.setToolTip("打开当前选中目录（或当前所在目录）")
        self.open_btn.clicked.connect(lambda: self._open_in_explorer(None))
        bar.addWidget(self.open_btn)
        bar.addStretch()
        v.addLayout(bar)

        # 进度条
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)  # 不确定模式（文件总数未知）
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        v.addWidget(self.progress_bar)

        # 摘要卡片：扫描后显示可清理/可迁移/系统保护的空间分布
        self.summary = QLabel(
            "扫描完成后，这里会显示本盘「可清理 / 可迁移 / 系统保护」的空间分布与处理建议。"
        )
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet(
            "background:#f3f6fb;border:1px solid #d9e2ec;border-radius:6px;padding:6px 8px;"
        )
        v.addWidget(self.summary)

        # 图例
        legend = QLabel(
            "图例：<font color='#e15759'>■ 可清理</font>　"
            "<font color='#59a14f'>■ 可迁移</font>　"
            "<font color='#9b59b6'>■ 系统保护</font>　"
            "<font color='#4e79a7'>■ 其他</font>　"
            "（树图块悬停可见名称与大小；单击选中，双击下钻）"
        )
        v.addWidget(legend)

        # 主分割：上=树图|列表，下=实时日志（固定高度，绝不撑大窗口）
        main_split = QSplitter(Qt.Vertical)

        upper = QSplitter(Qt.Horizontal)
        self.treemap = TreemapWidget()
        self.treemap.node_selected.connect(self._on_pick)
        upper.addWidget(self.treemap)

        list_widget = QWidget()
        lv = QVBoxLayout(list_widget)
        lv.setContentsMargins(0, 0, 0, 0)
        self.breadcrumb = QLabel("未扫描")
        self.breadcrumb.setStyleSheet("color:#555;")
        lv.addWidget(self.breadcrumb)
        self.tree = QTableWidget(0, 5)
        self.tree.setHorizontalHeaderLabels(["目录", "大小", "占比", "类别", "说明"])
        self.tree.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tree.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.tree.setSelectionBehavior(QTableWidget.SelectRows)
        self.tree.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        self.tree.cellDoubleClicked.connect(self._tree_drill)
        lv.addWidget(self.tree, stretch=1)
        upper.addWidget(list_widget)
        upper.setStretchFactor(0, 3)
        upper.setStretchFactor(1, 2)
        main_split.addWidget(upper)

        self.scan_log = QPlainTextEdit()
        self.scan_log.setReadOnly(True)
        self.scan_log.setMaximumHeight(150)
        self.scan_log.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.scan_log.setSizePolicy(QSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum))
        self.scan_log.setPlaceholderText("扫描过程实时记录（目录/文件/累计大小）")
        main_split.addWidget(self.scan_log)
        main_split.setStretchFactor(0, 4)
        main_split.setStretchFactor(1, 1)
        v.addWidget(main_split, stretch=1)

        self.tabs.addTab(w, "分析")
        self._nav_history = []

    def _scan(self):
        root = self.drive.currentText()
        self.scan_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self.pause_btn.setText("暂停")
        self.cancel_btn.setEnabled(True)
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setValue(0)
        self.scan_log.clear()
        self._last_dirs = 0
        self._last_files = 0
        self._last_size = 0
        self.status.setText(f"扫描中: {root} ...")
        self.scan_log.appendPlainText(f"[开始] 扫描 {root}")
        self._pause_event = threading.Event()    # 初始未置位 = 运行中
        self._cancel_event = threading.Event()
        worker = ScanWorker(self.rules, root, self._pause_event, self._cancel_event)
        worker.progress.connect(self._on_scan_progress)
        worker.logmsg.connect(self._on_scan_log)
        worker.done.connect(self._scan_done)
        worker.cancelled.connect(self._scan_cancelled)
        worker.start()
        self._scan_worker = worker

    def _on_scan_progress(self, info):
        self._last_dirs = info["dirs"]
        self._last_files = info["files"]
        self._last_size = info["size"]
        self.status.setText(
            f"扫描中 | 目录 {info['dirs']} 文件 {info['files']} "
            f"累计 {human_size(info['size'])} | {info['path']}"
        )

    def _on_scan_log(self, msg):
        self.scan_log.appendPlainText(msg)
        # 限制日志长度，避免长时间扫描内存膨胀
        if self.scan_log.blockCount() > 2000:
            lines = self.scan_log.toPlainText().split("\n")
            self.scan_log.setPlainText("\n".join(lines[-1500:]))

    def _scan_pause(self):
        if self._pause_event.is_set():
            self._pause_event.clear()
            self.pause_btn.setText("暂停")
            self.scan_log.appendPlainText("[已继续]")
        else:
            self._pause_event.set()
            self.pause_btn.setText("继续")
            self.scan_log.appendPlainText("[已暂停，扫描线程挂起]")

    def _scan_cancel(self):
        self._cancel_event.set()
        self._pause_event.clear()  # 解除暂停以便快速退出
        self.scan_log.appendPlainText("[取消请求，扫描将尽快停止]")

    def _scan_cancelled(self):
        self.scan_log.appendPlainText("[扫描已取消]")
        self.status.setText("已取消扫描")
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self._reset_scan_buttons()

    def _reset_scan_buttons(self):
        self.scan_btn.setEnabled(True)
        self.pause_btn.setEnabled(False)
        self.pause_btn.setText("暂停")
        self.cancel_btn.setEnabled(False)
        if getattr(self, "_pause_event", None) is not None:
            self._pause_event.clear()

    def _scan_done(self, node):
        if isinstance(node, dict) and node.get("error"):
            QMessageBox.critical(self, "扫描失败", node["error"])
            self.scan_log.appendPlainText(f"[失败] {node['error']}")
            self._reset_scan_buttons()
            return
        self.root = node
        self._nav_history = []
        self.back_btn.setEnabled(False)
        self._build_index(node)
        self.treemap.set_root(node)
        self._fill_tree(node)
        self._update_breadcrumb(node)
        self.summary.setText(self._summarize(node))
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(1)
        self.status.setText(f"完成 总占用 {human_size(node.size)}")
        self.scan_log.appendPlainText(
            f"[完成] 总占用 {human_size(node.size)}，已扫描目录 {self._last_dirs} 个，"
            f"文件 {self._last_files} 个"
        )
        self._reset_scan_buttons()

    def _fill_tree(self, node):
        self.tree.setRowCount(0)
        total = node.size or 1
        for c in sorted(node.children, key=lambda x: x.size, reverse=True)[:300]:
            r = self.tree.rowCount()
            self.tree.insertRow(r)
            name_item = QTableWidgetItem(c.path)
            name_item.setData(100, c)  # 存 node 引用，供双击下钻
            self.tree.setItem(r, 0, name_item)
            self.tree.setItem(r, 1, QTableWidgetItem(human_size(c.size)))
            self.tree.setItem(r, 2, QTableWidgetItem(f"{100.0 * c.size / total:.1f}%"))
            cat = self._node_category(c)
            cat_item = QTableWidgetItem(cat)
            cat_item.setForeground(self._cat_color(cat))
            self.tree.setItem(r, 3, cat_item)
            desc = ""
            if c.cascade:
                desc = "级联：数据已重定向 → " + (c.reparse_target or "?")
            elif c.clean_tags:
                desc = c.clean_tags[0].get("description", "")
            elif c.migrate_tags:
                desc = c.migrate_tags[0].get("description", "")
            self.tree.setItem(r, 4, QTableWidgetItem(desc))
            self.tree.setRowHeight(r, 24)

    def _node_category(self, n):
        if n.cascade:
            return "已重定向"
        if self.rules.is_protected(n.path):
            return "系统保护"
        if n.clean_tags:
            return "可清理"
        if n.migrate_tags:
            return "可迁移"
        return "其他"

    @staticmethod
    def _cat_color(cat):
        return {
            "可清理": QColor("#e15759"),
            "可迁移": QColor("#59a14f"),
            "系统保护": QColor("#9b59b6"),
            "已重定向": QColor("#6b7a99"),
            "其他": QColor("#4e79a7"),
        }.get(cat, QColor("#333333"))

    def _tree_menu(self, pos):
        item = self.tree.itemAt(pos)
        node = item.data(100) if item else None
        menu = QMenu(self)
        act = menu.addAction("在资源管理器中打开")
        act.setEnabled(node is not None)
        chosen = menu.exec(self.tree.viewport().mapToGlobal(pos))
        if chosen == act and node:
            self._open_in_explorer(node.path)

    def _open_in_explorer(self, path=None):
        if not path:
            sel = self.tree.currentRow()
            item = self.tree.item(sel, 0) if sel >= 0 else None
            if item:
                node = item.data(100)
                if node:
                    path = node.path
        if not path and self.root:
            path = self.root.path
        if not path:
            return
        try:
            if IS_WIN:
                # 注意：不能带 nowin_kw（隐藏窗口参数）——会把资源管理器窗口本身藏掉
                # 资源管理器是 GUI 程序，本身不会弹控制台窗口
                if os.path.isfile(path):
                    subprocess.Popen(["explorer", f"/select,{path}"])
                else:
                    subprocess.Popen(["explorer", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:  # noqa: BLE001
            # 兜底：explorer 异常时退回系统默认方式打开
            try:
                opener = getattr(os, "startfile", None)
                if opener:
                    opener(path)
                else:
                    QMessageBox.warning(self, "提示", f"打开资源管理器失败: {e}")
            except Exception:  # noqa: BLE001
                QMessageBox.warning(self, "提示", f"打开资源管理器失败: {e}")

    def _summarize(self, node):
        """汇总可清理/可迁移/系统保护的空间分布，避免父子重复计数。"""
        clean_by = {"safe": 0, "cautious": 0, "danger": 0}
        state = {"migrate": 0, "protected": 0}
        covered = set()
        prot_covered = set()

        def walk(n):
            np = normalize(n.path)
            if self.rules.is_protected(n.path):
                if not any(np.startswith(p) for p in prot_covered):
                    state["protected"] += n.size
                    prot_covered.add(np)
                return  # 子项同样受保护，跳过避免重复计数
            if n.clean_tags and np not in covered:
                r = n.clean_tags[0].get("risk") or "safe"
                clean_by[r] = clean_by.get(r, 0) + n.size
                covered.add(np)
            elif n.migrate_tags and np not in covered:
                state["migrate"] += n.size
                covered.add(np)
            for c in n.children:
                walk(c)

        walk(node)
        total = node.size or 1
        clean_total = sum(clean_by.values())

        def pct(x):
            return f"{100.0 * x / total:.1f}%"

        return (
            "总占用 <b>%s</b>　|　可清理 <b>%s</b>（%s）"
            "［safe %s / cautious %s / danger %s］　|　"
            "可迁移 <b>%s</b>（%s）　|　系统保护 <b>%s</b>（%s）<br>"
            "<span style='color:#2e7d32'>提示：可迁移项迁移后通过目录联接(junction)对软件完全透明，"
            "无需重新配置即可正常运行；可清理项默认送回收站，可一键还原。</span>"
            % (
                human_size(node.size), human_size(clean_total), pct(clean_total),
                human_size(clean_by["safe"]), human_size(clean_by["cautious"]),
                human_size(clean_by["danger"]), human_size(state["migrate"]),
                pct(state["migrate"]), human_size(state["protected"]),
                pct(state["protected"]),
            )
        )

    def _on_pick(self, node):
        if self.root is not None and node is not self.root:
            self._nav_history.append(self.root)
        self.root = node
        self.treemap.set_root(node)
        self._fill_tree(node)
        self._update_breadcrumb(node)
        self.back_btn.setEnabled(bool(self._nav_history))

    def _drill_up(self):
        if self._nav_history:
            prev = self._nav_history.pop()
            self.root = prev
            self.treemap.set_root(prev)
            self._fill_tree(prev)
            self._update_breadcrumb(prev)
        self.back_btn.setEnabled(bool(self._nav_history))

    def _tree_drill(self, row, _col):
        item = self.tree.item(row, 0)
        if not item:
            return
        node = item.data(100)
        if node and node.children:
            self._on_pick(node)

    def _update_breadcrumb(self, node):
        self.breadcrumb.setText(
            f"当前目录：{node.path}　总占用 {human_size(node.size)}"
        )

    def _build_index(self, node: Node) -> None:
        """建立 规范化路径 -> 节点 索引，供清理/迁移页按扫描结果取大小。"""
        self._node_index.clear()
        stack = [node]
        while stack:
            n = stack.pop()
            self._node_index[normalize(n.path)] = n
            stack.extend(n.children)

    def _size_for(self, path: str) -> int:
        """优先取扫描树节点大小，否则退回文件大小。"""
        node = self._node_index.get(normalize(path))
        if node is not None:
            return node.size
        try:
            if os.path.isfile(path):
                return os.path.getsize(path)
        except OSError:
            pass
        return 0

    # ---------------- 清理页 ----------------
    def _build_clean(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(QLabel("可清理项（按风险分级，默认仅勾选 safe）。执行前建议创建还原点。"))
        self.clean_table = QTableWidget(0, 5)
        self.clean_table.setHorizontalHeaderLabels(["", "路径", "大小", "风险", "方式"])
        self.clean_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.clean_table.setEditTriggers(QTableWidget.NoEditTriggers)
        v.addWidget(self.clean_table, stretch=1)
        bar = QHBoxLayout()
        self.restore_btn = QPushButton("创建还原点")
        self.restore_btn.clicked.connect(self._create_restore)
        self.clean_dry = QPushButton("预览(dry_run)")
        self.clean_dry.clicked.connect(lambda: self._do_clean(dry=True))
        self.clean_run = QPushButton("执行清理")
        self.clean_run.clicked.connect(lambda: self._do_clean(dry=False))
        bar.addWidget(self.restore_btn)
        bar.addWidget(self.clean_dry)
        bar.addWidget(self.clean_run)
        bar.addStretch()
        v.addLayout(bar)
        self.tabs.addTab(w, "清理")

    def _refresh_clean(self):
        if not self.root:
            return
        items = self._gather_clean()
        self.clean_table.setRowCount(0)
        for it in items:
            r = self.clean_table.rowCount()
            self.clean_table.insertRow(r)
            chk = QTableWidgetItem()
            chk.setCheckState(Qt.Checked if it["risk"] == "safe" else Qt.Unchecked)
            self.clean_table.setItem(r, 0, chk)
            path_item = QTableWidgetItem(it["path"])
            path_item.setData(100, it["rule"])  # 存完整规则，执行时直接交给 cleaner
            self.clean_table.setItem(r, 1, path_item)
            self.clean_table.setItem(r, 2, QTableWidgetItem(human_size(it["size"])))
            risk_item = QTableWidgetItem(it["risk"])
            risk_item.setForeground(self._cat_color("可清理"))
            self.clean_table.setItem(r, 3, risk_item)
            self.clean_table.setItem(r, 4, QTableWidgetItem(it.get("action", "")))
            self.clean_table.setRowHeight(r, 24)
        if not items:
            self.clean_table.insertRow(0)
            self.clean_table.setItem(0, 1, QTableWidgetItem("未发现可清理项（或尚未扫描系统盘）"))

    def _gather_clean(self):
        """按清理规则的基础文件夹聚合，取扫描结果大小，过滤不存在/红线项。"""
        out = []
        seen = set()
        for rule in self.rules.clean_rules:
            for pat in (rule.get("match") or {}).get("paths", []):
                base = pat[:-2] if pat.endswith("\\*") else pat
                base = os.path.expandvars(base)
                if not os.path.exists(base):
                    continue
                key = normalize(base)
                if key in seen or self.rules.is_protected(base):
                    continue
                seen.add(key)
                out.append({"path": base, "size": self._size_for(base),
                            "risk": rule.get("risk", ""), "action": rule.get("action", ""),
                            "rule": rule})
        out.sort(key=lambda x: x["size"], reverse=True)
        return out

    def _do_clean(self, dry: bool):
        if not self.root:
            QMessageBox.information(self, "提示", "请先扫描")
            return
        targets = []
        for r in range(self.clean_table.rowCount()):
            chk = self.clean_table.item(r, 0)
            if chk and chk.checkState():
                rule = self.clean_table.item(r, 1).data(100)
                if rule:
                    targets.append({"path": self.clean_table.item(r, 1).text(), "rule": rule})
        if not targets:
            QMessageBox.information(self, "提示", "未勾选任何项")
            return
        if not dry:
            if QMessageBox.question(self, "确认",
                                    f"将清理 {len(targets)} 项（默认送回收站）。继续？") != QMessageBox.StandardButton.Yes:
                return
            self.status.setText("清理中...")
        worker = Worker(lambda: self.cleaner.clean_targets(targets, dry_run=dry))
        worker.done.connect(lambda res: self._clean_done(res, dry))
        worker.start()

    def _clean_done(self, res, dry):
        ok = sum(1 for x in res if x.get("status") == "ok")
        self.status.setText(f"清理完成（dry={dry}）：{ok}/{len(res)} 成功")
        QMessageBox.information(self, "清理结果",
                                f"处理 {len(res)} 项，成功 {ok}。\n"
                                + ("（dry_run 预览，未实际删除）" if dry else "（已送回收站/系统通道）"))

    # ---------------- 迁移页 ----------------
    def _build_migrate(self):
        w = QWidget()
        v = QVBoxLayout(w)
        explain = QLabel(
            "迁移原理（为何不影响软件运行）：\n"
            "方式一【junction 透明迁移，推荐】：把文件夹物理复制到目标盘，删除 C 盘原目录，并在原位置创建「目录联接(junction)」。"
            "原路径依旧有效、指向新位置，软件无需重新安装或改任何配置即可正常运行（详见下方说明）。\n"
            "方式二【移动+改配置】：把文件夹搬到新盘后，由本工具一键改写该程序的配置文件/环境变量（如 VS Code 的 settings.json、"
            "Maven 的 settings.xml、Godot 的 editor_settings.tres 等），把路径指到新位置。\n"
            "两种方式的迁移清单都会记录「原路径 → 新路径」，可随时回退。\n"
            "◆ 每一行可单独修改「目标目录」，把不同工具迁到不同盘；也可以只迁移单个目录（点该行的「迁移此行」）。"
        )
        explain.setWordWrap(True)
        explain.setStyleSheet(
            "background:#f1f8f1;border:1px solid #cfe8cf;border-radius:6px;padding:8px;color:#2e4d2e;"
        )
        v.addWidget(explain)
        row = QHBoxLayout()
        row.addWidget(QLabel("默认目标根目录:"))
        self.dst = QComboBox()
        self.dst.setEditable(True)
        self.dst.addItem("D:\\Migrated")
        self.dst.setMinimumWidth(220)
        row.addWidget(self.dst)
        apply_all = QPushButton("用根目录重置所有行")
        apply_all.setToolTip("用上面的根目录 + 各目录名，重写每一行的目标目录")
        apply_all.clicked.connect(self._apply_root_to_all)
        row.addWidget(apply_all)
        row.addStretch()
        v.addLayout(row)

        tip = QLabel("提示：每行「目标目录」可直接编辑或点「…」选择；只想搬一个目录就点该行的「迁移此行」。")
        tip.setStyleSheet("color:#666;font-size:11px;")
        v.addWidget(tip)

        self.mig_table = QTableWidget(0, 6)
        self.mig_table.setHorizontalHeaderLabels(["", "对象", "大小", "目标目录", "方式", "操作"])
        mh = self.mig_table.horizontalHeader()
        mh.setSectionResizeMode(1, QHeaderView.Stretch)
        mh.setSectionResizeMode(3, QHeaderView.Stretch)
        mh.setMinimumSectionSize(48)
        self.mig_table.setColumnWidth(0, 30)
        self.mig_table.setColumnWidth(2, 90)
        self.mig_table.setColumnWidth(4, 160)
        self.mig_table.setColumnWidth(5, 260)
        self.mig_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.mig_widgets = {}
        v.addWidget(self.mig_table, stretch=1)

        # 已重定向(级联)清单：迁移后在此登记 原→新 关联，可随时回滚
        v.addWidget(QLabel("已重定向（级联关联，扫描时自动识别为「已重定向」并跳过迁移）："))
        self.migrated_table = QTableWidget(0, 5)
        self.migrated_table.setHorizontalHeaderLabels(["源路径", "目标路径", "方式", "时间", "操作"])
        mr = self.migrated_table.horizontalHeader()
        mr.setSectionResizeMode(0, QHeaderView.Stretch)
        mr.setSectionResizeMode(1, QHeaderView.Stretch)
        mr.setMinimumSectionSize(60)
        self.migrated_table.setColumnWidth(2, 120)
        self.migrated_table.setColumnWidth(3, 160)
        self.migrated_table.setColumnWidth(4, 80)
        self.migrated_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.migrated_table.setMaximumHeight(190)
        v.addWidget(self.migrated_table)
        self._refresh_migrated()

        bar = QHBoxLayout()
        self.mig_dry = QPushButton("预览(dry_run)")
        self.mig_dry.clicked.connect(lambda: self._do_migrate(dry=True))
        self.mig_run = QPushButton("执行迁移(勾选项)")
        self.mig_run.clicked.connect(lambda: self._do_migrate(dry=False))
        bar.addWidget(self.mig_dry)
        bar.addWidget(self.mig_run)
        bar.addWidget(QLabel("进度:"))
        self.mig_progress = QProgressBar()
        self.mig_progress.setRange(0, 100)
        self.mig_progress.setValue(0)
        bar.addWidget(self.mig_progress, stretch=1)
        self.mig_cancel_btn = QPushButton("取消")
        self.mig_cancel_btn.setEnabled(False)
        self.mig_cancel_btn.clicked.connect(self._mig_cancel)
        bar.addWidget(self.mig_cancel_btn)
        bar.addStretch()
        v.addLayout(bar)

        self.mig_log = QPlainTextEdit()
        self.mig_log.setReadOnly(True)
        self.mig_log.setMaximumHeight(110)
        self.mig_log.setPlaceholderText("迁移过程实时记录（关进程 → 复制 → 建联接 → 完成）")
        v.addWidget(self.mig_log)
        self.tabs.addTab(w, "迁移")

    def _build_mig_candidates(self):
        """合并 migrate_rules 与 app_profiles，仅保留本机存在的项，附扫描大小。

        级联文件夹（已重定向的 junction/符号链接，或已在迁移清单里的源）直接排除，
        因为数据已迁走，再迁移只会搬动"指针"本身。
        """
        migrated = {normalize(e.get("src", "")) for e in self.migrator._load_manifest()}
        cands = []
        for rule in self.rules.migrate_rules:
            srcs = [os.path.expandvars(p) for p in (rule.get("match") or {}).get("paths", [])]
            src = next((s for s in srcs if os.path.exists(s)), None)
            if not src or is_reparse_point(src) or normalize(src) in migrated:
                continue
            cands.append({"name": rule.get("description", rule.get("id", "")),
                          "src": src, "size": self._size_for(src),
                          "methods": [rule.get("method", "junction")],
                          "kind": "rule", "obj": rule})
        for p in self.rules.app_profiles:
            srcs = [os.path.expandvars(s) for s in p.get("sources", [])]
            src = next((s for s in srcs if os.path.exists(s)), None)
            if not src or is_reparse_point(src) or normalize(src) in migrated:
                continue
            methods = list(p.get("methods") or [p.get("method_default", "junction")])
            if "junction" not in methods:
                methods.insert(0, "junction")
            cands.append({"name": f"{p.get('name', '')}（{p.get('category', '')}）",
                          "src": src, "size": self._size_for(src),
                          "methods": methods, "kind": "app", "obj": p})
        cands.sort(key=lambda x: x["size"], reverse=True)
        return cands

    @staticmethod
    def _method_label(m):
        return {"junction": "junction 透明(推荐)", "config": "移动+改配置",
                "shell_folder_redirect": "系统重定向"}.get(m, m)

    def _refresh_migrate(self):
        self.mig_table.setRowCount(0)
        self.mig_widgets = {}
        root = self.dst.currentText().strip() or "D:\\Migrated"
        for c in self._build_mig_candidates():
            r = self.mig_table.rowCount()
            self.mig_table.insertRow(r)
            chk = QTableWidgetItem()
            chk.setCheckState(Qt.Unchecked)
            self.mig_table.setItem(r, 0, chk)
            obj_item = QTableWidgetItem(f"{c['name']}\n{c['src']}")
            obj_item.setData(100, c)  # 存候选 dict
            self.mig_table.setItem(r, 1, obj_item)
            self.mig_table.setItem(r, 2, QTableWidgetItem(human_size(c["size"])))
            # 目标目录：可编辑输入框 + 浏览按钮（每一项可自定义到不同盘）
            default_target = os.path.join(root, os.path.basename(c["src"].rstrip("\\")))
            target_le = QLineEdit(default_target)
            target_le.setToolTip("该目录迁移后的目标位置（可自定义到任意非系统盘）")
            browse = QPushButton("...")
            browse.setFixedWidth(26)
            browse.setToolTip("选择目标目录")
            browse.clicked.connect(lambda _=False, le=target_le: self._pick_target(le))
            tbox = QWidget()
            thb = QHBoxLayout(tbox)
            thb.addWidget(target_le, stretch=1)
            thb.addWidget(browse)
            thb.setContentsMargins(2, 2, 2, 2)
            self.mig_table.setCellWidget(r, 3, tbox)
            # 方式下拉
            combo = QComboBox()
            for m in c["methods"]:
                combo.addItem(self._method_label(m), m)
            self.mig_table.setCellWidget(r, 4, combo)
            # 操作：查看配置改动 + 迁移此行（单目录迁移）
            diff_btn = QPushButton("查看配置改动")
            diff_btn.clicked.connect(lambda _=False, row=r: self._show_config_diff(row))
            mig_btn = QPushButton("迁移此行")
            mig_btn.clicked.connect(lambda _=False, row=r: self._do_migrate(dry=False, single_row=row))
            obox = QWidget()
            ohb = QHBoxLayout(obox)
            ohb.addWidget(diff_btn)
            ohb.addWidget(mig_btn)
            ohb.setContentsMargins(2, 2, 2, 2)
            self.mig_table.setCellWidget(r, 5, obox)
            self.mig_widgets[r] = {"target": target_le, "combo": combo}
            self.mig_table.setRowHeight(r, 46)
        if self.mig_table.rowCount() == 0:
            self.mig_table.insertRow(0)
            self.mig_table.setItem(0, 1, QTableWidgetItem("未发现可迁移项（或尚未扫描系统盘）"))

    def _pick_target(self, le: QLineEdit):
        res = QFileDialog.getExistingDirectory(self, "选择目标目录（非系统盘）")
        d = res[0] if isinstance(res, tuple) else res
        if d:
            le.setText(d)

    def _apply_root_to_all(self):
        root = self.dst.currentText().strip()
        if not root:
            return
        for r in range(self.mig_table.rowCount()):
            wi = self.mig_widgets.get(r)
            c = self.mig_table.item(r, 1).data(100) if self.mig_table.item(r, 1) else None
            if wi and c and wi.get("target"):
                wi["target"].setText(os.path.join(root, os.path.basename(c["src"].rstrip("\\"))))

    def _show_config_diff(self, row):
        c = self.mig_table.item(row, 1).data(100)
        if not c:
            return
        combo = self.mig_table.cellWidget(row, 4)
        method = combo.currentData() if combo else "junction"
        wi = self.mig_widgets.get(row, {})
        dst = (wi.get("target").text().strip() if wi.get("target")
               else os.path.join(self.dst.currentText(), os.path.basename(c["src"].rstrip("\\"))))
        lines = []
        if c["kind"] == "app":
            profile = c["obj"]
            if method == "config" and profile.get("config_patch"):
                rebases = self.migrator.config_rebases(profile, dst)
                lines = self.migrator.patcher.describe(profile["config_patch"], c["src"], dst, rebases)
                title = f"{profile.get('name','')} 改配置预览（迁移到 {dst}）"
            else:
                lines = ["junction 透明迁移：原路径保留目录联接，程序无需任何配置改动即可正常运行。",
                         profile.get("notes", "")]
                title = f"{profile.get('name','')} 迁移说明"
        else:
            rule = c["obj"]
            lines = [rule.get("description", ""), "方式：" + self._method_label(method),
                     rule.get("notes", "")]
            title = "迁移说明"
        QMessageBox.information(self, title, "\n".join(l for l in lines if l))

    def _do_migrate(self, dry: bool, single_row: int | None = None):
        dst_root = self.dst.currentText()
        picks = []
        rows = [single_row] if single_row is not None else range(self.mig_table.rowCount())
        for r in rows:
            chk = self.mig_table.item(r, 0)
            c = self.mig_table.item(r, 1).data(100) if self.mig_table.item(r, 1) else None
            if not c:
                continue
            if single_row is None and not (chk and chk.checkState()):
                continue
            wi = self.mig_widgets.get(r, {})
            target = (wi.get("target").text().strip() if wi.get("target")
                      else os.path.join(dst_root, os.path.basename(c["src"].rstrip("\\"))))
            combo = wi.get("combo")
            method = combo.currentData() if combo else "junction"
            if not target:
                QMessageBox.warning(self, "提示", f"第 {r + 1} 行目标目录为空，已跳过")
                continue
            picks.append((c, method, target))
        if not picks:
            QMessageBox.information(self, "提示", "未勾选任何项" if single_row is None else "无可迁移项")
            return
        if not dry:
            if getattr(self, "_mig_worker", None) is not None and self._mig_worker.isRunning():
                QMessageBox.information(self, "提示", "已有迁移任务进行中，请等待完成或取消")
                return
            # 级联(迁移)前提示：数据搬运期间不要关闭程序/关机
            warn = "\n\n⚠ 迁移过程中请勿关闭本程序、不要关机或断开目标盘，否则可能导致数据不一致。"
            if single_row is not None:
                nm, tg = picks[0][0]["name"], picks[0][2]
                q = QMessageBox.question(self, "确认",
                        f"将迁移「{nm}」到 {tg}。{warn}\n\n继续？")
            else:
                q = QMessageBox.question(self, "确认",
                        f"将迁移 {len(picks)} 项。{warn}\n\n继续？")
            if q != QMessageBox.StandardButton.Yes:
                return
            self._start_migrate_worker(picks)
            return
        # dry 预览：同步快速执行，汇总结果
        results = []
        for c, method, target in picks:
            if c["kind"] == "app":
                res = self.migrator.migrate_app(c["obj"], target, method, dry_run=True)
            else:
                res = self.migrator.migrate(c["src"], target, method,
                                            c["obj"].get("associated_processes", []),
                                            c["obj"].get("pre_command"), dry_run=True)
            results.append(res)
        ok = sum(1 for x in results if x.get("status") in ("ok", "partial"))
        self.status.setText(f"迁移预览（dry）：{ok}/{len(results)} 可执行")
        patch_lines = []
        for res in results:
            for p in res.get("patch", []):
                patch_lines.append(f"• {p.get('file','')}: {p.get('detail','')}")
        msg = f"预览 {len(results)} 项，{ok} 项可执行（dry_run，未实际迁移）。\n"
        if patch_lines:
            msg += "\n将改写的配置：\n" + "\n".join(patch_lines[:30])
        QMessageBox.information(self, "迁移预览", msg)

    def _start_migrate_worker(self, picks):
        self.mig_cancel_event = threading.Event()
        worker = MigrateWorker(self.migrator, picks, self.mig_cancel_event)
        worker.progress.connect(self._on_mig_progress)
        worker.log.connect(self._on_mig_log)
        worker.done.connect(self._migrate_done)
        worker.start()
        self._mig_worker = worker
        self.mig_dry.setEnabled(False)
        self.mig_run.setEnabled(False)
        self.mig_cancel_btn.setEnabled(True)
        self.mig_progress.setValue(0)
        self.mig_log.clear()
        self.mig_log.appendPlainText(f"[开始] 迁移 {len(picks)} 项")
        self.mig_log.appendPlainText("[提示] 迁移过程中请勿关闭本程序/关机，直到任务完成！")

    def _on_mig_progress(self, percent, text):
        self.mig_progress.setValue(percent)
        self.status.setText(f"迁移中：{text}")

    def _on_mig_log(self, msg):
        self.mig_log.appendPlainText(msg)
        if self.mig_log.blockCount() > 800:
            lines = self.mig_log.toPlainText().split("\n")
            self.mig_log.setPlainText("\n".join(lines[-600:]))

    def _mig_cancel(self):
        if getattr(self, "mig_cancel_event", None) is not None:
            self.mig_cancel_event.set()
        self.mig_log.appendPlainText("[取消请求，当前项完成后停止]")

    def _migrate_done(self, results):
        self.mig_cancel_btn.setEnabled(False)
        self.mig_dry.setEnabled(True)
        self.mig_run.setEnabled(True)
        ok = sum(1 for x in results if x.get("status") in ("ok", "partial"))
        self.status.setText(f"迁移完成：{ok}/{len(results)} 成功")
        patch_lines = []
        for res in results:
            for p in res.get("patch", []):
                patch_lines.append(f"• {p.get('file','')}: {p.get('detail','')}")
        msg = f"处理 {len(results)} 项，成功 {ok}。\n"
        fails = [x for x in results if x.get("status") not in ("ok", "partial")]
        if fails:
            msg += "\n未成功项：\n" + "\n".join(
                f"• {x.get('detail') or x.get('reason') or x.get('status')}" for x in fails[:10])
        if patch_lines:
            msg += "\n\n配置已改写：\n" + "\n".join(patch_lines[:30])
        self.mig_log.appendPlainText(msg)
        QMessageBox.information(self, "迁移结果", msg)
        # 执行级联关联：刷新清单，并把已迁移的源在扫描树中标记为已重定向
        self._refresh_migrate()
        self._refresh_migrated()
        self._mark_migrated_in_tree(results)

    def _refresh_migrated(self):
        if not hasattr(self, "migrated_table"):
            return
        self.migrated_table.setRowCount(0)
        for e in self.migrator._load_manifest():
            r = self.migrated_table.rowCount()
            self.migrated_table.insertRow(r)
            self.migrated_table.setItem(r, 0, QTableWidgetItem(e.get("src", "")))
            self.migrated_table.setItem(r, 1, QTableWidgetItem(e.get("dst", "")))
            self.migrated_table.setItem(r, 2, QTableWidgetItem(e.get("type", "")))
            self.migrated_table.setItem(r, 3, QTableWidgetItem(e.get("created", "")))
            rb = QPushButton("回滚")
            rb.clicked.connect(lambda _=False, src=e.get("src", ""): self._rollback_entry(src))
            self.migrated_table.setCellWidget(r, 4, rb)
            self.migrated_table.setRowHeight(r, 36)
        if self.migrated_table.rowCount() == 0:
            self.migrated_table.insertRow(0)
            self.migrated_table.setItem(0, 0, QTableWidgetItem("（暂无已迁移项）"))

    def _rollback_entry(self, src):
        q = QMessageBox.question(self, "确认回滚",
                f"将删除 {src} 处的目录联接（数据仍保留在目标盘，不会删除数据）。\n继续？")
        if q != QMessageBox.StandardButton.Yes:
            return
        res = self.migrator.rollback(src)
        QMessageBox.information(self, "回滚", res.get("detail", res.get("status", "")))
        self._refresh_migrated()
        self._refresh_migrate()

    def _mark_migrated_in_tree(self, results):
        """迁移成功后，把原路径在扫描树里标记为「已重定向」，无需重新扫描。"""
        changed = False
        for res in results:
            if res.get("status") not in ("ok", "partial"):
                continue
            src = res.get("src")
            if not src:
                continue
            node = self._node_index.get(normalize(src))
            if node is not None:
                node.cascade = True
                node.reparse_target = res.get("dst") or ""
                node.size = 0
                changed = True
        if changed and self.root is not None:
            self._fill_tree(self.root)
            self.summary.setText(self._summarize(self.root))

    # ---------------- 防再生页 ----------------
    def _build_prevent(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(QLabel("防再生开关：开启后 C 盘不再堆积不必要文件。可一键还原。"))
        self.prev_table = QTableWidget(0, 5)
        self.prev_table.setHorizontalHeaderLabels(["开关", "说明", "风险", "当前", "操作"])
        self.prev_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        v.addWidget(self.prev_table, stretch=1)
        self._refresh_prevent()
        self.tabs.addTab(w, "防再生")

    def _refresh_prevent(self):
        self.prev_table.setRowCount(0)
        for t in self.preventer.list_toggles():
            r = self.prev_table.rowCount()
            self.prev_table.insertRow(r)
            self.prev_table.setItem(r, 0, QTableWidgetItem(t["label"]))
            self.prev_table.setItem(r, 1, QTableWidgetItem(t["desc"]))
            self.prev_table.setItem(r, 2, QTableWidgetItem(t["risk"]))
            self.prev_table.setItem(r, 3, QTableWidgetItem(str(t["current"])))
            preview = QPushButton("预览")
            applyb = QPushButton("应用")
            preview.clicked.connect(lambda _=False, tid=t["id"], np=t["needs_param"]: self._prevent_apply(tid, np, True))
            applyb.clicked.connect(lambda _=False, tid=t["id"], np=t["needs_param"]: self._prevent_apply(tid, np, False))
            box = QWidget()
            hb = QHBoxLayout(box)
            hb.addWidget(preview)
            hb.addWidget(applyb)
            hb.setContentsMargins(0, 0, 0, 0)
            self.prev_table.setCellWidget(r, 4, box)

    def _prevent_apply(self, tid, needs_param, dry):
        param = ""
        if needs_param:
            param, _ = QFileDialog.getExistingDirectory(self, "选择目标目录（非系统盘）") or ("", False)
            if not param:
                return
        res = self.preventer.apply(tid, dry_run=dry, param=param)
        self._refresh_prevent()
        QMessageBox.information(self, "防再生", f"{tid}: {res.get('detail','')}")

    # ---------------- 报告/日志页 ----------------
    def _build_report(self):
        w = QWidget()
        v = QVBoxLayout(w)
        bar = QHBoxLayout()
        self.csv_btn = QPushButton("导出 CSV")
        self.csv_btn.clicked.connect(self._export_csv)
        self.html_btn = QPushButton("导出 HTML(含树图)")
        self.html_btn.clicked.connect(self._export_html)
        bar.addWidget(self.csv_btn)
        bar.addWidget(self.html_btn)
        bar.addStretch()
        v.addLayout(bar)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        v.addWidget(self.log_view, stretch=1)
        self.tabs.addTab(w, "报告/日志")

    def _refresh_log(self):
        lines = []
        for r in self.safety.recent(200):
            lines.append(f"{r['ts']}  {r['op_type']:14} {r['risk']:8} {r['result']:6}  {r['target']}  {r['detail']}")
        self.log_view.setPlainText("\n".join(lines))

    def _export_csv(self):
        if not self.root:
            QMessageBox.information(self, "提示", "请先扫描")
            return
        p, _ = QFileDialog.getSaveFileName(self, "保存 CSV", "report.csv", "*.csv")
        if p:
            self.reporter.export_csv(self.root, p)
            self.status.setText(f"已导出 CSV: {p}")

    def _export_html(self):
        if not self.root:
            QMessageBox.information(self, "提示", "请先扫描")
            return
        p, _ = QFileDialog.getSaveFileName(self, "保存 HTML", "report.html", "*.html")
        if p:
            self.reporter.export_html(self.root, p)
            self.status.setText(f"已导出 HTML: {p}")

    # ---------------- 还原点 ----------------
    def _create_restore(self):
        ok, msg = self.safety.create_restore_point()
        QMessageBox.information(self, "还原点", msg)

    # ---------------- 切页刷新 ----------------
    def _on_tab(self, idx):
        name = self.tabs.tabText(idx)
        if name == "清理":
            self._refresh_clean()
        elif name == "迁移":
            self._refresh_migrate()
            self._refresh_migrated()
        elif name == "防再生":
            self._refresh_prevent()
        elif name == "报告/日志":
            self._refresh_log()


def main():
    import sys
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
