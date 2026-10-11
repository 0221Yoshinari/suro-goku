#!/usr/bin/env python3
"""サイトセブンの CAPTCHA を画面表示で手動解除し、Cookie を上書き保存する。"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scraper.site7_fetch import refresh_site7_session


def main() -> None:
    parser = argparse.ArgumentParser(
        description="headed ブラウザでサイトセブンに入り、CAPTCHA 解除後の Cookie を保存する"
    )
    parser.add_argument(
        "--wait",
        type=float,
        default=60.0,
        metavar="SEC",
        help="CAPTCHA 手動解除の待機秒数（既定 60）",
    )
    parser.add_argument("--hall", help="開くホール名。未指定時は config/halls.json の先頭")
    args = parser.parse_args()
    from src.run_gate import exit_if_halted

    exit_if_halted("サイトセブンのセッション更新")
    path = asyncio.run(refresh_site7_session(wait_sec=args.wait, hall=args.hall))
    print(f"次の headless 取得でこの Cookie を使います: {path}")


if __name__ == "__main__":
    main()
