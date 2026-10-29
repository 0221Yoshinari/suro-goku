"""スリープ中の請求サーバーを起こし、デイリーレポートの送信を頼む。"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request


def main() -> int:
    secret = (os.environ.get("REPORT_CRON_SECRET") or "").strip()
    url = (
        os.environ.get("BILLING_REPORT_URL") or "https://suro-goku-billing.onrender.com/internal/daily-report"
    ).strip()
    if not secret:
        print("REPORT_CRON_SECRET が未設定のため、レポートを起動しません。", flush=True)
        return 1
    deadline = time.time() + 180
    while True:
        status, body = _post(url, secret)
        if body:
            print(body[:300], flush=True)
        if status in {401, 403}:
            print("レポートの起動が認証で拒否されました。", flush=True)
            return 1
        if status and status < 400 and _accepted(body):
            return 0
        if status:
            print(f"レポートの起動に失敗しました (HTTP {status})", flush=True)
        if time.time() >= deadline:
            return 1
        time.sleep(15)


def _post(url: str, secret: str) -> tuple[int, str]:
    request = urllib.request.Request(
        url,
        data=b"",
        method="POST",
        headers={"Authorization": f"Bearer {secret}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return int(response.status), response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return int(exc.code), body
    except Exception as exc:
        print(f"レポートの起動に失敗しました ({type(exc).__name__})", flush=True)
        return 0, ""


def _accepted(body: str) -> bool:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return False
    return bool(payload.get("ok")) if isinstance(payload, dict) else False


if __name__ == "__main__":
    raise SystemExit(main())
