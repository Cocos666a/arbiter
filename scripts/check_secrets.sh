#!/usr/bin/env bash
# 扫描仓库，发现疑似密钥字面量即失败退出。
# 允许出现脱敏占位（REDACTED）与测试专用假值（TESTONLY）。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

FAIL=0

echo "== 扫描 sk- 样式密钥 =="
HITS="$(grep -rInE 'sk-[A-Za-z0-9_-]{8,}' . \
    --exclude-dir=.git \
    --exclude-dir=__pycache__ \
    --exclude-dir=reports \
    2>/dev/null | grep -v 'REDACTED' | grep -v 'TESTONLY' || true)"
if [ -n "$HITS" ]; then
    echo "$HITS"
    FAIL=1
else
    echo "  未发现"
fi

echo "== 扫描 Bearer 字面量 =="
HITS="$(grep -rInE 'Bearer [A-Za-z0-9]' . \
    --exclude-dir=.git \
    --exclude-dir=__pycache__ \
    --exclude-dir=reports \
    2>/dev/null | grep -v 'REDACTED' | grep -v 'TESTONLY' || true)"
if [ -n "$HITS" ]; then
    echo "$HITS"
    FAIL=1
else
    echo "  未发现"
fi

echo "== 扫描其他常见凭据样式 =="
HITS="$(grep -rInE '(api[_-]?key|secret|token|password)[[:space:]]*[:=][[:space:]]*["'\''][A-Za-z0-9_-]{16,}["'\'']' . \
    --exclude-dir=.git \
    --exclude-dir=__pycache__ \
    --exclude-dir=reports \
    --exclude='check_secrets.sh' \
    2>/dev/null | grep -v 'REDACTED' | grep -v 'TESTONLY' || true)"
if [ -n "$HITS" ]; then
    echo "$HITS"
    FAIL=1
else
    echo "  未发现"
fi

echo ""
if [ "$FAIL" -eq 0 ]; then
    echo "check_secrets: 通过（仓库内未发现密钥字面量）"
    exit 0
fi
echo "check_secrets: 失败（发现疑似密钥，请改用 --key-file 或环境变量）" >&2
exit 1
