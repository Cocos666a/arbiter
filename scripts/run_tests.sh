#!/usr/bin/env bash
# 一键跑全部离线测试（不访问外网，仅 127.0.0.1 上的 mock 网关）。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python3}"

echo "仓库根：$ROOT"
echo "解释器：$("$PYTHON" --version 2>&1)"
echo ""

exec "$PYTHON" -m unittest discover -s tests -t . -v
