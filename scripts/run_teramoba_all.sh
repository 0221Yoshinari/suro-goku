#!/usr/bin/env bash
# 玉屋グループ全店舗をテラモバ2から一括取得し、判定して Discord 通知する
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PYTHONUNBUFFERED=1
export PLAYWRIGHT_CHANNEL="${PLAYWRIGHT_CHANNEL:-chrome}"
export TERAMOBA_BROWSER_HEADLESS="${TERAMOBA_BROWSER_HEADLESS:-0}"
export TERAMOBA_BROWSER_PROFILE="${TERAMOBA_BROWSER_PROFILE:-}"
exec python3 -m src.scraper --source teramoba --fetch --reanalyze "$@"
