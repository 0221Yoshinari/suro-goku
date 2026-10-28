"""スリープ中の請求サーバーを起こし、デイリーレポートの送信を頼む。"""

from __future__ import annotations

import os
import sys
import urllib.request


def main() -> int:
    secret = (os.environ.get("REPORT_CRON_SECRET") or "").strip()
    url = (
        os.environ.get("BILLING_REPORT_URL") or "https://suro-goku-billing.onrender.com/internal/daily-report"
    ).strip()
    if not secret:
        print("REPORT_CRON_SECRET が未設定のため、レポートを起動しません。", flush=True)
        return 1
    request = urllib.request.Request(
        url,
        data=b"",
        method="POST",
        headers={"Authorization": f"Bearer {secret}"},
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        print(response.read().decode("utf-8", errors="replace")[:300], flush=True)
        return 0 if response.status < 400 else 1


if __name__ == "__main__":
    raise SystemExit(main())
