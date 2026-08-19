"""报告引擎：扫描树导出 CSV / HTML（含 SVG treemap）。"""
from __future__ import annotations

import csv
import html
import os
from typing import Optional

from .treemap_layout import squarify
from .util import human_size


class Reporter:
    def export_csv(self, root, path: str, top_n: int = 500) -> str:
        rows = []

        def walk(n, depth=0):
            rows.append((n.path, n.size, n.risk, n.tag_text()))
            for c in sorted(n.children, key=lambda x: x.size, reverse=True)[:top_n]:
                if depth < 6:
                    walk(c, depth + 1)

        walk(root)
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["path", "size_bytes", "size", "risk", "tags"])
            for p, sz, risk, tags in rows:
                w.writerow([p, sz, human_size(sz), risk, tags])
        return path

    def export_html(self, root, path: str, top_n: int = 40) -> str:
        # 收集顶级子目录
        children = sorted(root.children, key=lambda x: x.size, reverse=True)[:top_n]
        items = [(c.name or c.path, c.size) for c in children]
        W, H = 900, 520
        rects = squarify(items, 0, 0, W, H)
        rect_svg = []
        for r in rects:
            key = r["key"]
            node = next((c for c in children if (c.name or c.path) == key), None)
            color = "#4e79a7"
            if node and node.clean_tags:
                color = "#e15759"
            elif node and node.migrate_tags:
                color = "#59a14f"
            label = html.escape(str(key))[:18]
            rect_svg.append(
                f'<rect x="{r["x"]:.1f}" y="{r["y"]:.1f}" width="{r["w"]:.1f}" '
                f'height="{r["h"]:.1f}" fill="{color}" stroke="#fff" stroke-width="0.5">'
                f'<title>{html.escape(str(key))} {human_size(int(node.size)) if node else ""}</title></rect>'
                f'<text x="{r["x"]+3:.1f}" y="{r["y"]+14:.1f}" font-size="10" fill="#fff">'
                f'{html.escape(label)}</text>'
            )
        svg = (
            f'<svg width="{W}" height="{H}" xmlns="http://www.w3.org/2000/svg">'
            + "".join(rect_svg) + "</svg>"
        )
        # 表格
        table_rows = []
        for c in children:
            table_rows.append(
                f"<tr><td>{html.escape(c.path)}</td><td>{human_size(c.size)}</td>"
                f"<td>{c.risk}</td><td>{html.escape(c.tag_text())}</td></tr>"
            )
        doc = f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>C 盘瘦身报告</title>
<style>body{{font-family:system-ui,'Microsoft YaHei',sans-serif;margin:24px;color:#222}}
h1{{font-size:20px}} table{{border-collapse:collapse;width:100%;font-size:13px}}
td,th{{border:1px solid #ddd;padding:6px 8px;text-align:left}}
th{{background:#f5f5f5}} .legend span{{display:inline-block;margin-right:14px}}</style>
</head><body>
<h1>C 盘瘦身扫描报告</h1>
<p>根目录：<b>{html.escape(root.path)}</b> ｜ 总占用：<b>{human_size(root.size)}</b></p>
<div class="legend"><span><span style="color:#e15759">■</span> 可清理</span>
<span><span style="color:#59a14f">■</span> 可迁移</span>
<span><span style="color:#4e79a7">■</span> 其他</span></div>
{svg}
<h2>Top {top_n} 目录</h2>
<table><tr><th>路径</th><th>大小</th><th>风险</th><th>标签</th></tr>
{''.join(table_rows)}</table>
</body></html>"""
        with open(path, "w", encoding="utf-8") as f:
            f.write(doc)
        return path
