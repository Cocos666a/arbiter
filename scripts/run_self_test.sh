#!/usr/bin/env bash
# 对真实网关的端到端自测（需自备网关地址与密钥，二者都不入库）。
#
# 环境变量：
#   ARBITER_BASE_URL     必填，被测网关地址
#   ARBITER_KEY_FILE     必填，内容为一行密钥的本地文件（自行 chmod 600）
#   ARBITER_MODELS       可选，逗号分隔的基线模型列表，默认 deepseek-chat
#   ARBITER_AUDIT_MODEL  可选，被审计模型，默认取 ARBITER_MODELS 的第一个
#   ARBITER_OUT_DIR      可选，报告目录，默认 reports/self_test/
#
# 流程：① 对每个模型建数字基线（供 fingerprint 归因）
#       ② 对审计模型跑全量九节审计
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
BASE="${ARBITER_BASE_URL:-}"
KEYF="${ARBITER_KEY_FILE:-}"
MODELS="${ARBITER_MODELS:-deepseek-chat}"
AUDIT_MODEL="${ARBITER_AUDIT_MODEL:-${MODELS%%,*}}"
OUT="${ARBITER_OUT_DIR:-$HERE/reports/self_test}"

if [ -z "$BASE" ]; then
  echo "缺少 ARBITER_BASE_URL（被测网关地址，例如 https://gateway.example.com）" >&2
  exit 1
fi
if [ -z "$KEYF" ] || [ ! -f "$KEYF" ]; then
  echo "缺少 ARBITER_KEY_FILE（保存密钥的本地文件路径）" >&2
  exit 1
fi

cd "$HERE"
mkdir -p "$OUT"

echo "== 1/2 建指纹基线（每个模型目标 3 个可用样本，最多 6 次尝试）"
IFS=',' read -r -a MODEL_ARR <<< "$MODELS"
for m in "${MODEL_ARR[@]}"; do
  m="$(echo "$m" | tr -d ' ')"
  [ -z "$m" ] && continue
  echo "-- $m"
  python3 -m arbiter bank build --base-url "$BASE" --model "$m" \
    --key-file "$KEYF" --samples 3
done
python3 -m arbiter bank show

echo "== 2/2 全量审计 $AUDIT_MODEL"
python3 -m arbiter run --base-url "$BASE" --model "$AUDIT_MODEL" \
  --key-file "$KEYF" --sections all --out "$OUT"

echo "== 完成；报告在 $OUT"
