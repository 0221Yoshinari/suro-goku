"""閲覧用アカウントを台帳へ登録する。

使い方:
  SEED_MEMBER_EMAIL=... SEED_MEMBER_PASSWORD=... python3 scripts/seed_viewer_account.py

パスワードは引数にもファイルにも残さない。PBKDF2 でハッシュしてから保存する。
同じメールがあるときは、パスワードと閲覧権限を更新する。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main() -> int:
    from src.dashboard.members import MembersStoreError, ensure_viewer_account
    from src.scraper.site7 import _load_dotenv

    _load_dotenv(PROJECT_ROOT / ".env")
    email = (os.environ.get("SEED_MEMBER_EMAIL") or "").strip()
    password = os.environ.get("SEED_MEMBER_PASSWORD") or ""
    if not email or not password:
        print("SEED_MEMBER_EMAIL と SEED_MEMBER_PASSWORD を指定してください", file=sys.stderr)
        return 1
    try:
        result = ensure_viewer_account(email, password)
    except MembersStoreError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("閲覧アカウントを作成しました" if result == "created" else "閲覧アカウントを更新しました")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
