#!/usr/bin/env bash
# 1サイクル実行（config/halls.json の source。玉屋∞本店は teramoba）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PYTHONUNBUFFERED=1
exec python3 -m src.scheduler --once "$@"
