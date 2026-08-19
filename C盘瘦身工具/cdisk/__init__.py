"""C 盘瘦身工具 - 清理 / 迁移 / 防再生。

架构见《技术文档.md》：
  cdisk.core  与 OS 无关的业务引擎（扫描/规则/清理/迁移/防再生/安全/调度/报告）
  cdisk.ui    PySide6 桌面界面
  cdisk.rules YAML 规则库（与代码解耦，可扩展）
"""

__version__ = "0.1.0"
