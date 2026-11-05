#!/usr/bin/env bash
# 部署：单店试点环境的最小部署脚本（示例，需按门店实际情况调整）
set -euo pipefail
cd "$(dirname "$0")/.."

# 用法示例：
#   DEPLOY_HOST=user@host scripts/deploy.sh
: "${DEPLOY_HOST:?请先 export DEPLOY_HOST=user@host}"
: "${DEPLOY_DIR:=/opt/kitchen-collab}"

echo "▶ 同步代码到 $DEPLOY_HOST:$DEPLOY_DIR"
ssh "$DEPLOY_HOST" "mkdir -p $DEPLOY_DIR"
rsync -avz --exclude '__pycache__' --exclude '.git' \
      src config requirements.txt "$DEPLOY_HOST:$DEPLOY_DIR/"

echo "▶ 远端安装依赖并做冒烟测试"
ssh "$DEPLOY_HOST" "cd $DEPLOY_DIR && \
  pip3 install -q -r requirements.txt && \
  PYTHONPATH=src python3 -m kitchen_collab.main demo > /tmp/kc_smoke.log && \
  tail -5 /tmp/kc_smoke.log"

echo "✅ 部署完成。审计日志位于远端 $DEPLOY_DIR/data/audit_log.jsonl"
