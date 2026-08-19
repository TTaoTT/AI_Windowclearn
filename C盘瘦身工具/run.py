"""入口：python run.py（建议以管理员身份运行以获得完整功能）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cdisk.ui.main_window import main

if __name__ == "__main__":
    main()
