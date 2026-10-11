"""python3 -m src.notify --test"""

from __future__ import annotations

import argparse

from src.notify.discord import send_test, webhook_url


def main() -> None:
    parser = argparse.ArgumentParser(description="Discord 通知")
    parser.add_argument("--test", action="store_true", help="テスト通知を1通送る")
    parser.add_argument("--message", help="テスト通知の本文")
    args = parser.parse_args()
    if not args.test:
        raise SystemExit("使うには --test を指定してください")
    if not webhook_url():
        raise SystemExit(".env の DISCORD_WEBHOOK_URL が空です")
    send_test(args.message)
    print("テスト通知を送信しました")


if __name__ == "__main__":
    main()
