# C 盘瘦身工具（CDisk Slimmer）

一款面向 Windows 的 **C 盘瘦身 / 数据迁移** 桌面工具。扫描分析 C 盘占用 → 一键清理可删缓存 → 把大文件夹迁移到其他盘且**不影响软件运行**（junction 透明迁移 / 一键改写配置），并提供防再生、报告导出等能力。

> 单文件 GUI（PySide6），双击即以管理员运行，无需安装 Python。

---

## ✨ 功能特性

### 1. 扫描分析
- 全盘扫描，实时进度 / 暂停 / 继续 / 取消，目录、文件数、累计大小实时显示
- **矩形树图 + 明细表**双视图：按大小着色分类（可清理 / 可迁移 / 系统保护 / 其他），支持双击下钻、返回上级、面包屑导航
- 扫描后自动给出「可清理 / 可迁移 / 系统保护」空间分布摘要
- **级联文件夹识别**：junction / 符号链接自动标记为「已重定向」，体积记 0、不递归，避免把其他盘的数据算进 C 盘

### 2. 一键清理
- 按 YAML 规则库聚合可清理项（缓存 / 临时文件 / 垃圾），带大小、风险分级（safe / cautious / danger）
- **safe 项默认勾选**，执行前可创建系统还原点；默认送回收站可还原

### 3. 智能迁移（核心）
- **两种迁移方式**：
  - **junction 透明迁移（推荐）**：物理复制到目标盘 → 删除 C 盘原目录 → 在原位置创建目录联接。原路径依旧有效、指向新盘，**软件零配置即可运行**
  - **移动 + 改配置**：搬走后由工具一键改写该程序的配置 / 环境变量，把路径指到新盘
- **逐目录自定义目标**：每一行可单独设置目标目录（不同工具迁到不同盘），也可「迁移此行」只搬单个目录
- **已知应用画像库（32 个）**：VS Code、Maven、Gradle、npm/pnpm/yarn、pip、Conda、Cargo、Go、Android Studio、Flutter、Docker、Godot、Unity、JetBrains、Chrome、Edge、Firefox、Steam、Epic 等
- **一键改配置引擎**：支持 `set_json / set_xml / set_props / set_ini / set_tres / replace_text / env` 七种规格，改前自动备份、可先「查看配置改动」预览
- **迁移进度 + 无黑窗**：后台线程执行，进度条 / 实时日志 / 取消按钮；全部子进程隐藏控制台窗口
- **级联关联（已重定向清单）**：迁移记录 原→新 映射，可随时**一键回滚**；已迁移目录在下次扫描自动识别并禁止重复迁移
- 安全护栏：迁移前关闭相关进程、删除源前先算好配置重映射、建链失败自动把数据移回原位、拒绝迁移红线目录

### 4. 防再生 + 报告
- 防再生开关：关闭系统自动再生垃圾，可一键还原
- 报告导出：CSV / HTML（含树图快照）

---

## 🚀 快速上手

1. 下载 `cdisk.exe`，**右键 → 以管理员身份运行**（迁移需要管理员权限）
2. 选择盘符 → 点击「扫描」
3. 切到「清理」页：勾选要清理的项 → 「执行清理」
4. 切到「迁移」页：
   - 每行可修改「目标目录」或点「…」选择
   - 方式选 **junction 透明（推荐）** 或 **移动+改配置**
   - 点「查看配置改动」可预览将要改写的配置
   - 勾选多项批量执行，或点「迁移此行」只搬单个目录
5. 迁移完成后，「已重定向」清单会登记 原→新 映射，随时可「回滚」

> ⚠ **迁移过程中请勿关闭本程序 / 不要关机或断开目标盘**，否则可能导致数据不一致。

---

## 🗂️ 目录结构

```
C盘瘦身工具/
├── run.py                    # 入口（python run.py）
├── requirements.txt
├── _selftest.py              # 自测脚本
├── README.md
├── 需求文档.md / 技术文档.md
├── cdisk/
│   ├── core/                 # 引擎层（与 UI 解耦，可独立测试）
│   │   ├── scanner.py        # 扫描引擎（进度/暂停/取消、级联识别）
│   │   ├── cleaner.py        # 清理引擎（回收站/系统通道、还原点）
│   │   ├── migrator.py       # 迁移引擎（junction / 改配置 / 回滚 / 进度）
│   │   ├── config_patcher.py # 通用配置改写引擎（7 种规格，自动备份）
│   │   ├── rules.py          # 规则引擎（加载 YAML 规则库）
│   │   ├── safety.py         # 安全与红线目录
│   │   ├── preventer.py      # 防再生开关
│   │   ├── scheduler.py      # 定时任务
│   │   ├── reporter.py       # 报告导出
│   │   ├── treemap_layout.py # 矩形树图布局算法
│   │   └── util.py           # 工具（reparse 检测、隐藏窗口参数等）
│   ├── ui/
│   │   ├── main_window.py    # 主窗口（分析/清理/迁移/防再生/报告）
│   │   └── treemap.py        # 树图控件
│   └── rules/                # YAML 规则库（打包时一并打入 exe）
│       ├── clean_rules.yaml      # 清理规则
│       ├── migrate_rules.yaml    # 迁移规则
│       └── app_profiles.yaml     # 32 个已知应用画像（含 config_patch）
```

---

## 🧱 技术架构

- **Python 3.13 + PySide6**：GUI（PySide6 支持 Windows 原生控件）
- **分层设计**：`core/` 引擎（扫描/清理/迁移/规则/安全）与 `ui/` 完全解耦，核心逻辑可无 GUI 运行、便于测试
- **规则驱动**：清理 / 迁移 / 应用画像全部由 YAML 描述，新增规则无需改代码
- **迁移机制**：
  - junction：`_winapi.CreateJunction`（原生 API，失败回退 `mklink /J`），带重试与真实错误上报
  - 改配置：`config_patcher` 支持 `<NEW>` / `<OLD>` 占位符与路径重映射（rebase），改前自动备份
- **级联识别**：`GetFileAttributes` 检测 `FILE_ATTRIBUTE_REPARSE_POINT`，junction 不会被 `os.path.islink` 误判
- **无黑窗**：所有子进程 `CREATE_NO_WINDOW` + `STARTUPINFO` 隐藏；`explorer` 除外（避免藏掉资源管理器窗口）

---

## 🔨 开发 / 构建

```bash
# 1. 创建虚拟环境（Python 3.13）
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt

# 2. 运行
.venv\Scripts\python run.py

# 3. 打包单文件 exe（管理员、无控制台窗口）
.venv\Scripts\pyinstaller run.py --name cdisk --onefile --windowed --uac-admin \
  --add-data "cdisk/rules;cdisk/rules" \
  --hidden-import PySide6.QtCore --hidden-import PySide6.QtGui \
  --hidden-import PySide6.QtWidgets --hidden-import yaml
```

> 打包注意：旧版 exe 若被占用，先重命名旧产物再构建，避免 PyInstaller 清理失败。

---

## ❓ 常见问题

| 现象 | 说明 |
| --- | --- |
| 迁移时弹 DOS 黑窗 | 已修复：所有子进程隐藏控制台窗口 |
| 「打开文件夹」没反应 | 已修复：`explorer` 不再携带隐藏窗口参数 |
| 「建立目录联接失败」 | 已修复：改用原生 API 建链接、带回退与重试；失败自动把数据移回原位 |
| 迁移后软件找不到文件 | 使用 junction 方式不会出现；若用「移动+改配置」，请先「查看配置改动」确认配置已被改写 |
| 需要管理员权限 | 迁移 / 清理涉及系统目录，exe 以管理员运行 |

---

## ⚠️ 安全与免责

- 涉及删除 / 迁移操作前建议**创建系统还原点**（工具内置入口）
- 迁移采用「复制 → 删除源 → 建联接」流程，但任何工具都无法 100% 保证意外断电等场景；迁移期间请勿关机
- 红线目录（系统目录等）已被规则层保护，不会出现在可操作列表中
- 使用本工具造成的任何数据损失，作者不承担责任，请自行谨慎操作
