#!/usr/bin/env bash
# 构建：打源码分发包 + 确认测试全绿
set -euo pipefail
cd "$(dirname "$0")/.."

echo "▶ 运行测试"
export PYTHONPATH=src
python -m unittest discover -s tests -v

echo "▶ 清理旧构建"
rm -rf build dist *.egg-info

echo "▶ 打包（纯 src 布局，可直接 zip 分发）"
mkdir -p dist
zip -r dist/kitchen-collab-src.zip src config data/sample_orders.json examples docs README.md requirements.txt

echo "✅ 构建完成：dist/"
