"""squarified treemap 布局算法（供 GUI 树图与 HTML 报告复用）。

输入 items: [(key, value>0), ...]；输出每个块的矩形 (x,y,w,h)。
算法：Bruls, Huizing, van Wijk, "Squarified Treemaps"。
"""

from typing import List, Tuple


def _worst(row_areas: List[float], side: float) -> float:
    if not row_areas:
        return float("inf")
    s = sum(row_areas)
    mx = max(row_areas)
    mn = min(row_areas)
    return max((side * side * mx) / (s * s), (s * s) / (side * side * mn))


def _layout_row(row, free, rects) -> None:
    side = min(free[2], free[3])
    s = sum(a for _, a in row)
    if free[2] >= free[3]:
        strip_w = s / side
        cx = free[0]
        cy = free[1]
        for key, a in row:
            ih = a / strip_w
            rects.append({"key": key, "x": cx, "y": cy, "w": strip_w, "h": ih})
            cy += ih
        free[0] += strip_w
        free[2] -= strip_w
    else:
        strip_h = s / side
        cx = free[0]
        cy = free[1]
        for key, a in row:
            iw = a / strip_h
            rects.append({"key": key, "x": cx, "y": cy, "w": iw, "h": strip_h})
            cx += iw
        free[1] += strip_h
        free[3] -= strip_h


def squarify(items: List[Tuple[str, float]], x: float, y: float, w: float, h: float) -> List[dict]:
    """返回 [{"key","x","y","w","h"}, ...]。"""
    items = [it for it in items if it[1] > 0]
    if not items:
        return []
    items = sorted(items, key=lambda t: t[1], reverse=True)
    total = sum(a for _, a in items) or 1.0
    scale = (w * h) / total
    items = [(k, a * scale) for k, a in items]
    rects: List[dict] = []
    free = [x, y, w, h]
    row: List[Tuple[str, float]] = []
    idx = 0
    while idx < len(items):
        side = min(free[2], free[3])
        cand = items[idx]
        cur = [a for _, a in row]
        new = cur + [cand[1]]
        if row and _worst(new, side) > _worst(cur, side):
            _layout_row(row, free, rects)
            row = [cand]
        else:
            row.append(cand)
        idx += 1
    if row:
        _layout_row(row, free, rects)
    return rects
