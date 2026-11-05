"""跑得起来的示例：一键还原「晚市高峰 + 纸质加单 + 过敏套餐 + 缺料 + 催菜」。

用法（项目根目录）：
    set PYTHONPATH=src        # Windows
    export PYTHONPATH=src     # macOS/Linux
    python examples/demo.py
"""

import os
import sys

# 让示例脚本在没有 pip install 的情况下也能直接跑
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from kitchen_collab.main import run_demo  # noqa: E402

if __name__ == "__main__":
    run_demo()
