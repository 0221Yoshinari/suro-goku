"""データソース CLI。既定はサイトセブンの JSON 正規化。--fetch でライブ取得。"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from src.scraper.halls import PROJECT_ROOT, resolve_hall_names
from src.scraper.registry import get_source, list_sources


def _apply_hall_args(hall: list[str], halls: str | None) -> None:
    names: list[str] = []
    names.extend(hall)
    if halls:
        names.extend(part.strip() for part in halls.split(",") if part.strip())
    resolved = resolve_hall_names(names)
    if resolved:
        os.environ["SITE7_HALL_NAMES"] = ",".join(resolved)


def main() -> None:
    parser = argparse.ArgumentParser(description="公式データサイトから台データを取り込む")
    parser.add_argument("--source", default="site7", help="データソース ID（" + ", ".join(list_sources()) + "）")
    parser.add_argument("--fetch", action="store_true", help="ライブ取得する（保存済みセッションがあれば再利用）")
    parser.add_argument(
        "--reanalyze",
        action="store_true",
        help="サイトセブンへアクセスせず、保存済み incoming.json だけで判定を再実行する",
    )
    parser.add_argument(
        "--refresh-session",
        action="store_true",
        help="画面表示で CAPTCHA を手動解除し、Cookie を上書き保存する",
    )
    parser.add_argument("--headed", action="store_true", help="ブラウザ画面を表示する（headless=False）")
    parser.add_argument(
        "--captcha-wait",
        type=float,
        default=60.0,
        metavar="SEC",
        help="CAPTCHA 手動解除の待機秒数（既定 60）",
    )
    parser.add_argument(
        "--hall",
        action="append",
        default=[],
        metavar="NAME",
        help="取得する店舗名（1店舗テスト向け。複数回指定可）",
    )
    parser.add_argument("--halls", help="巡回ホール名（カンマ区切り）。未指定時は config/halls.json")
    parser.add_argument("--input", type=Path, help="既存 JSON を正規化する場合の入力パス")
    args = parser.parse_args()

    _apply_hall_args(args.hall, args.halls)
    if args.headed:
        os.environ["SCRAPER_HEADLESS"] = "0"

    if args.refresh_session or args.fetch:
        from src.run_gate import exit_if_halted

        exit_if_halted("ライブ取得")

    if args.refresh_session:
        hall = resolve_hall_names(args.hall or (args.halls or "").split(","))
        if args.source == "teramoba":
            import runpy

            script = PROJECT_ROOT / "scripts" / "import_chrome_teramoba_session.py"
            sys.argv = [str(script)]
            runpy.run_path(str(script), run_name="__main__")
            return
        else:
            from src.scraper.site7_fetch import refresh_site7_session

            path = asyncio.run(refresh_site7_session(wait_sec=args.captcha_wait, hall=hall[0] if hall else None))
        print(f"セッション保存: {path}")
        return

    fetch_extra: dict = {}
    if args.fetch:
        if args.captcha_wait > 0 and args.headed:
            from src.scraper.site7_fetch import set_captcha_wait_sec

            os.environ["SITE7_CAPTCHA_WAIT_SEC"] = str(args.captcha_wait)
            set_captcha_wait_sec(args.captcha_wait)
        if args.source == "site7":
            from src.scraper.fetch_all import fetch_configured

            result = asyncio.run(fetch_configured())
            print(f"取得: 設定ソース混合 / {len(result.machines)} 件")
        else:
            source = get_source(args.source)
            result = asyncio.run(source.fetch())
            print(f"取得: {source.display_name} / {len(result.machines)} 件")
        fetch_extra = dict(result.extra or {})
        if not args.reanalyze:
            return

    source = get_source(args.source)

    if args.reanalyze:
        from src.pipeline import _print_judgment, run_source

        incoming = args.input or (PROJECT_ROOT / "data" / args.source / "incoming.json")
        if not incoming.is_file():
            raise SystemExit(f"保存済みデータがありません: {incoming}")
        print(f"キャッシュ再解析: {incoming}（通信しません）", flush=True)
        analyzed = run_source(args.source, input_path=incoming)
        print(f"変換件数: {len(analyzed['records'])}")
        _print_judgment(analyzed["judgment"], PROJECT_ROOT / "data" / "judgments")
        if args.fetch:
            from src.notify.discord import notify_success
            from src.scraper.halls import configured_halls

            judgment = analyzed["judgment"]
            if fetch_extra.get("failed_halls"):
                judgment["failed_halls"] = fetch_extra["failed_halls"]
            if fetch_extra.get("failure_details"):
                judgment["failure_details"] = fetch_extra["failure_details"]
            sent = notify_success(
                judgment,
                elapsed_sec=fetch_extra.get("elapsed_sec"),
                target_halls=fetch_extra.get("target_halls") or configured_halls(),
            )
            print("Discord 完了通知を送信しました" if sent else "Discord 完了通知に失敗しました", flush=True)
        return

    records = source.ingest(args.input)
    print(f"変換件数: {len(records)}")


if __name__ == "__main__":
    main()
