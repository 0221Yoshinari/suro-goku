#!/usr/bin/env python3
"""ジャグラーモード TOP4 をダッシュボード截图4枚＋1本文で Discord プレビューする。"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.poster.capture import CaptureError, capture_juggler_islands
from src.poster.discord_preview import DiscordPreviewError, send_x_preview
from src.poster.juggler_rank import compose_juggler_pickup_post, rank_juggler_islands
from src.poster.ledger import ledger_path, load_ledger, posted_entry, remember
from src.poster.x_api import XApiError, assert_write_access, load_x_env, publish_tweet

JST = ZoneInfo("Asia/Tokyo")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = PROJECT_ROOT / "output" / "juggler-pickup"


def main(argv: list[str] | None = None) -> int:
    load_x_env()
    parser = argparse.ArgumentParser(description="ジャグラー島 TOP4 を4画像1投稿プレビューする")
    parser.add_argument("--limit", type=int, default=4, help="載せる件数（Xは最大4枚）")
    parser.add_argument("--halls", type=Path, default=PROJECT_ROOT / "data" / "judgments" / "halls")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="各キャプチャの保存先")
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

    picks = rank_juggler_islands(args.halls, limit=args.limit)
    if not picks:
        print("ランキングに載せられるジャグラー島がありません。", file=sys.stderr)
        return 2

    targets = [
        (str(row.get("hall_name") or ""), str(row.get("family_label") or ""))
        for row in picks
    ]
    now = datetime.now(JST)
    date = args.date or now.date().isoformat()
    text = compose_juggler_pickup_post(picks, now=now, slot=args.slot)
    print(text)
    print("---")

    saved: list[Path] = []
    if args.skip_capture:
        for index in range(1, len(targets) + 1):
            path = args.out_dir / f"{index:02d}.png"
            if not path.is_file():
                print(f"画像がありません: {path}", file=sys.stderr)
                return 1
            saved.append(path)
    else:
        base_url = args.dashboard or None
        args.out_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="juggler-capture-") as folder:
            capture_dir = Path(folder)
            try:
                shots = capture_juggler_islands(targets, capture_dir, base_url=base_url)
            except CaptureError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            if len(shots) != len(targets):
                print("キャプチャ枚数が足りません。", file=sys.stderr)
                return 1
            for index, shot in enumerate(shots, start=1):
                dest = args.out_dir / f"{index:02d}.png"
                shutil.copyfile(shot, dest)
                saved.append(dest)

    pictures = [path.read_bytes() for path in saved]
    ledger_file = ledger_path(PROJECT_ROOT / "output" / "post.json")
    ledger = load_ledger(ledger_file)
    hall_key = "ジャグラーTOP4"

    if args.publish:
        prior = posted_entry(ledger, date, args.slot, hall_key)
        if prior and not args.force:
            print(f"投稿済み: https://x.com/i/web/status/{prior['tweet_id']}", file=sys.stderr)
            return 0
        try:
            assert_write_access()
            result = publish_tweet(text, images=pictures)
        except XApiError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        remember(ledger, date, args.slot, hall_key, result["id"], "")
        ledger_file.parent.mkdir(parents=True, exist_ok=True)
        ledger_file.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        url = f"https://x.com/i/web/status/{result['id']}"
        print(f"投稿しました: {url}")
        try:
            send_x_preview(f"{text}\n{url}", images=pictures, label="【X投稿済み】")
        except DiscordPreviewError as exc:
            print(str(exc), file=sys.stderr)

    if args.discord or not args.publish:
        try:
            status = send_x_preview(text, images=pictures)
        except DiscordPreviewError as exc:
            print(str(exc), file=sys.stderr)
            return 1 if not args.publish else 0
        print(f"Discordへ1件のプレビューを送りました（画像{len(pictures)}枚, HTTP {status}）")

    for path in saved:
        print(f"  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
