"""无界面逻辑自测：扫描/规则/清理(dry)/迁移(dry)/防再生(dry)/报告。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cdisk.core.cleaner import Cleaner
from cdisk.core.migrator import Migrator
from cdisk.core.preventer import Preventer
from cdisk.core.reporter import Reporter
from cdisk.core.rules import RuleEngine
from cdisk.core.scanner import Scanner
from cdisk.core.util import human_size

base = tempfile.mkdtemp(prefix="cdisk_test_")
os.makedirs(os.path.join(base, "Windows", "Temp", "x"))
with open(os.path.join(base, "Windows", "Temp", "x", "a.tmp"), "w") as f:
    f.write("x" * 5000)
os.makedirs(os.path.join(base, "Users", "me", "Downloads", "big"))
with open(os.path.join(base, "Users", "me", "Downloads", "big", "movie.bin"), "w") as f:
    f.write("y" * 200000)

print("== 扫描 ==")
scanner = Scanner()
root = scanner.scan(base)
print("方法:", scanner.method, "总大小:", root.size, human_size(root.size))
assert root.size > 0
print("根子目录数:", len(root.children))

print("\n== 规则匹配 ==")
re = RuleEngine()
m = re.match_clean("C:\\Windows\\Temp\\foo")
print("clean match windows_temp:", [r["id"] for r in m])
assert any(r["id"] == "windows_temp" for r in m)
print("protected WinSxS:", re.is_protected("C:\\Windows\\WinSxS\\x"))
assert re.is_protected("C:\\Windows\\WinSxS\\x")
print("not protected Temp:", re.is_protected("C:\\Windows\\Temp\\x"))
assert not re.is_protected("C:\\Windows\\Temp\\x")

print("\n== 清理(dry_run) ==")
import tempfile as _t
_data = os.path.join(_t.gettempdir(), "cdisk_data")
os.makedirs(_data, exist_ok=True)
from cdisk.core.safety import SafetyManager
_safety = SafetyManager(os.path.join(_data, "op.db"))
cleaner = Cleaner(re, _safety)
res = cleaner.clean_targets(
    [{"path": os.path.join(base, "Windows", "Temp"),
      "rule": {"id": "windows_temp", "action": "recycle", "risk": "safe", "match": {"age_days": 0}}}],
    dry_run=True,
)
print(res)
assert res[0]["status"] == "ok" and res[0]["dry"] is True

print("\n== 迁移(dry_run) ==")
mig = Migrator(re)
r = mig.migrate(os.path.join(base, "Users", "me", "Downloads"), "D:\\Migrated\\Downloads",
                "junction", ["explorer.exe"], dry_run=True)
print(r)
assert r["status"] == "ok"

print("\n== 防再生(dry_run) ==")
prev = Preventer()
for t in prev.list_toggles():
    out = prev.apply(t["id"], dry_run=True, param="D:\\X")
    print(f"  {t['id']:20} -> {out['status']}: {out['detail']}")

print("\n== 报告 ==")
rep = Reporter()
csvp = os.path.join(tempfile.gettempdir(), "cdisk_report.csv")
htmlp = os.path.join(tempfile.gettempdir(), "cdisk_report.html")
rep.export_csv(root, csvp)
rep.export_html(root, htmlp)
print("CSV 字节:", os.path.getsize(csvp), " HTML 字节:", os.path.getsize(htmlp))
assert os.path.getsize(htmlp) > 500

print("\nALL OK")
