"""自绘 squarified 树图控件。点击选中目录，双击下钻。"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QWidget

from ..core.treemap_layout import squarify
from ..core.util import human_size

CLEAN_COLOR = QColor("#e15759")
MIGRATE_COLOR = QColor("#59a14f")
OTHER_COLOR = QColor("#4e79a7")
BG = QColor("#ffffff")


class TreemapWidget(QWidget):
    node_selected = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.root = None
        self.rects: list = []
        self.setMinimumSize(420, 320)

    def set_root(self, node):
        self.root = node
        self._layout()
        self.update()

    def _layout(self):
        self.rects = []
        if not self.root:
            return
        children = sorted(self.root.children, key=lambda n: n.size, reverse=True)[:80]
        items = [(c.path, c.size) for c in children]
        w = self.width() or 600
        h = self.height() or 400
        for r in squarify(items, 0, 0, w, h):
            node = next((c for c in children if c.path == r["key"]), None)
            if node:
                self.rects.append((node, r["x"], r["y"], r["w"], r["h"]))

    def _color(self, node):
        if node.clean_tags:
            return CLEAN_COLOR
        if node.migrate_tags:
            return MIGRATE_COLOR
        return OTHER_COLOR

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.fillRect(self.rect(), BG)
        for node, x, y, w, h in self.rects:
            p.fillRect(int(x), int(y), max(1, int(w)), max(1, int(h)), self._color(node))
            p.setPen(Qt.white)
            p.drawRect(int(x), int(y), max(1, int(w)), max(1, int(h)))
            if w > 60 and h > 30:
                label = (node.name or node.path)[-16:]
                p.setPen(Qt.white)
                p.drawText(int(x) + 3, int(y) + 13, label)
                p.drawText(int(x) + 3, int(y) + 27, human_size(node.size))
            elif w > 46 and h > 16:
                label = (node.name or node.path)[:10]
                p.setPen(Qt.white)
                p.drawText(int(x) + 3, int(y) + 13, label)
        p.end()

    def mouseMoveEvent(self, ev):
        node = self._hit(ev.pos())
        if node:
            self.setToolTip(f"{node.path}\n{human_size(node.size)}")
        else:
            self.setToolTip("")
        super().mouseMoveEvent(ev)

    def resizeEvent(self, ev):
        self._layout()
        super().resizeEvent(ev)

    def _hit(self, pos):
        for node, x, y, w, h in self.rects:
            if x <= pos.x() <= x + w and y <= pos.y() <= y + h:
                return node
        return None

    def mousePressEvent(self, ev):
        node = self._hit(ev.pos())
        if node:
            self.node_selected.emit(node)

    def mouseDoubleClickEvent(self, ev):
        node = self._hit(ev.pos())
        if node and node.children:
            self.set_root(node)
