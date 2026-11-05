"""通用工具：配置加载等。"""

from __future__ import annotations

import json
import os


def load_config(path: str | None = None) -> dict:
    """加载配置。优先 YAML（若装了 pyyaml），否则回退 JSON。

    配置查找顺序：显式路径 > 环境变量 KITCHEN_CONFIG > 项目内 config/default.yaml。
    """
    candidates = [path, os.environ.get("KITCHEN_CONFIG")]
    # config.py 位于 <root>/src/kitchen_collab/utils/，向上 4 层到项目根
    root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))
    candidates.append(os.path.join(root, "config", "default.yaml"))
    candidates.append(os.path.join(root, "config", "default.json"))
    for p in candidates:
        if p and os.path.exists(p):
            return _parse(p)
    raise FileNotFoundError("找不到配置文件：config/default.yaml 或 config/default.json")


def _parse(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if path.endswith((".yaml", ".yml")):
        try:
            import yaml  # 可选依赖
            return yaml.safe_load(text)
        except ImportError:
            raise RuntimeError("读取 YAML 配置需要安装 pyyaml：pip install pyyaml")
    return json.loads(text)
