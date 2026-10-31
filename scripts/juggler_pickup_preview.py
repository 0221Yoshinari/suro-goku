#!/usr/bin/env python3
"""ジャグラーモード TOP4 をダッシュボード截图4枚＋1本文で Discord プレビューする。"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.poster.juggler_pickup_flow import JugglerPickupArgs, juggler_pickup_dir, run_juggler_pickup
from src.poster.x_api import XApiError, assert_write_access, load_x_env

JST = ZoneInfo("Asia/Tokyo")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = PROJECT_ROOT / "output" / "juggler-pickup"


def main(argv: list[str] | None = None) -> int:
    load_x_env()
    parser = argparse.ArgumentParser(description="ジャグラー島 TOP4 を4画像1投稿プレビューする")
    parser.add_argument("--limit", type=int, default=4, help="載せる件数（Xは最大4枚）")
    parser.add_argument("--halls", type=Path, default=PROJECT_ROOT / "data" / "judgments" / "halls")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="各キャプチャの保存先（日付/スロット配下）")
    parser.add_argument("--dashboard", default="", help="スロ極 URL。未設定なら SURO_DASHBOARD_URL")
    parser.add_argument("--publish", action="store_true", help="Xへ本番投稿する")
    parser.add_argument("--discord", action="store_true", help="Discordへプレビューを送る")
    parser.add_argument("--skip-capture", action="store_true", help="保存済み画像をそのまま使う")
    parser.add_argument("--slot", default="midday", help="ledger 用スロット名")
    parser.add_argument("--date", default="", help="ledger 用 YYYY-MM-DD。未設定なら当日")
    parser.add_argument(
        "--force",
        action="store_true",
        help="ledger に同日スロットがあっても再投稿する",
    )
    args = parser.parse_args(argv)

    if args.limit < 1 or args.limit > 4:
        print("画像は1〜4枚です。", file=sys.stderr)
        return 2

    now = datetime.now(JST)
    date = args.date or now.date().isoformat()
    if args.publish:
        try:
            assert_write_access()
        except XApiError as exc:
            print(str(exc), file=sys.stderr)
            return 1

    flow = JugglerPickupArgs(
        halls=args.halls,
        date=date,
        slot=args.slot,
        out_dir=juggler_pickup_dir(args.out_dir, date, args.slot),
        meta=PROJECT_ROOT / "output" / "post.json",
        text=PROJECT_ROOT / "output" / "post.txt",
        dashboard=args.dashboard or "",
        dry_run=not args.publish,
        discord=args.discord or not args.publish,
        skip_capture=args.skip_capture,
        force=args.force,
        limit=args.limit,
    )
    code, meta = run_juggler_pickup(flow, now=now)
    text = str(meta.get("text") or "")
    if text:
        print(text)
        print("---")
    if meta.get("url"):
        print(f"投稿しました: {meta['url']}")
    elif meta.get("skipped") == "already_posted":
        print(f"投稿済み: https://x.com/i/web/status/{meta.get('tweet_id')}", file=sys.stderr)
    elif not meta.get("ok", True):
        print(meta.get("error") or "失敗", file=sys.stderr)
    elif not meta.get("posted") and not meta.get("discord_preview"):
        print("ランキングに載せられるジャグラー島がありません。", file=sys.stderr)
        return 2
    for path in meta.get("images") or []:
        print(f"  {path}")
    if meta.get("discord_preview"):
        print(f"Discordへ1件のプレビューを送りました（HTTP {meta.get('preview_http')})")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
