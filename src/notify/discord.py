"""Discord Webhook 通知。"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from src.analyzer.judgment import PROJECT_ROOT
from src.scraper.site7 import _load_dotenv

from src.scraper.halls import configured_halls

TOKYO = ZoneInfo("Asia/Tokyo")
DEFAULT_DASHBOARD_URL = "http://127.0.0.1:8000"


def _ensure_env() -> None:
    _load_dotenv(PROJECT_ROOT / ".env")


def webhook_url() -> str:
    _ensure_env()
    path = PROJECT_ROOT / "config" / "discord.json"
    if path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload, dict):
            configured = str(payload.get("webhook_url") or "").strip()
            if configured:
                return configured
    return (os.getenv("DISCORD_WEBHOOK_URL") or "").strip()


def dashboard_url() -> str:
    _ensure_env()
    explicit = (os.getenv("DASHBOARD_URL") or "").strip().rstrip("/")
    if explicit.startswith("https://"):
        return explicit
    from src.tunnel import current_public_url

    public = current_public_url()
    if public:
        return public
    return explicit or DEFAULT_DASHBOARD_URL


def dashboard_access_line() -> str:
    url = dashboard_url()
    if url.startswith("https://"):
        return f"[ダッシュボードを開く]({url})"
    return f"ダッシュボード: {url}"


def _format_clock(iso: str | None) -> str:
    if not iso:
        return "不明"
    try:
        stamp = datetime.fromisoformat(iso)
    except ValueError:
        return "不明"
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=TOKYO)
    else:
        stamp = stamp.astimezone(TOKYO)
    return stamp.strftime("%Y-%m-%d %H:%M")


def _failure_lines(judgment: dict[str, Any], failed: list[str]) -> list[str]:
    from src.scraper.pace import summarize_failure

    reasons: dict[str, str] = {}
    for item in judgment.get("failure_details") or []:
        if not isinstance(item, dict):
            continue
        hall = str(item.get("hall") or "").strip()
        if hall:
            reasons[hall] = summarize_failure(str(item.get("reason") or ""))
    lines = []
    for name in failed:
        lines.append(f"・{name}: [理由] {reasons.get(name) or '取得失敗'}")
    return lines


def build_success_content(
    judgment: dict[str, Any],
    *,
    elapsed_sec: float | None = None,
    target_halls: list[str] | None = None,
) -> str:
    del elapsed_sec
    targets = list(target_halls or judgment.get("target_halls") or configured_halls())
    failed = list(dict.fromkeys(str(name) for name in (judgment.get("failed_halls") or []) if str(name).strip()))
    if targets:
        ok_count = len([name for name in targets if name not in failed])
    else:
        ok_count = 0
    lines = [
        "📊 **【台データ取得完了】**",
        f"・日時: {_format_clock(judgment.get('judged_at'))}",
        f"・成功: **{ok_count}** 店舗",
        f"・失敗: **{len(failed)}** 店舗",
    ]
    if failed:
        lines.extend(["", "⚠️ **失敗内訳**", *_failure_lines(judgment, failed)])
    return "\n".join(lines).strip()


def message_chunks(text: str, limit: int = 1900) -> list[str]:
    """Discord の本文上限に収まるよう、行単位で分割する。"""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.split("\n"):
        piece = line if len(line) <= limit else line[: limit - 1] + "…"
        extra = len(piece) + (1 if current else 0)
        if current and size + extra > limit:
            chunks.append("\n".join(current))
            current = [piece]
            size = len(piece)
        else:
            current.append(piece)
            size += extra
    if current:
        chunks.append("\n".join(current))
    return chunks or [text[:limit]]


CAPTCHA_NOTICE = "【要対応】サイトセブン側でCAPTCHA認証が発生しました。ブラウザで認証を通してください"


def is_captcha_error(error: str) -> bool:
    text = error or ""
    return (
        "CAPTCHA" in text
        or "ロボットではありません" in text
        or "私はロボットではありません" in text
        or CAPTCHA_NOTICE in text
    )


def build_error_content(error: str) -> str:
    if is_captcha_error(error):
        return CAPTCHA_NOTICE
    from src.scraper.pace import summarize_failure

    reason = summarize_failure(error)
    return (
        "**データ取得エラー**\n"
        f"取得を完了できませんでした。（{reason}）\n"
        "時間をおいて自動で再試行します。\n"
        f"{dashboard_access_line()}"
    )


def send_webhook(content: str) -> int:
    url = webhook_url()
    if not url:
        raise RuntimeError("DISCORD_WEBHOOK_URL が未設定です")
    body = json.dumps({"content": content}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "tamaya-slot-scraper"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            response.read()
            return int(response.status)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Discord通知に失敗しました ({exc.code})") from exc


def notify_success(
    judgment: dict[str, Any],
    *,
    elapsed_sec: float | None = None,
    target_halls: list[str] | None = None,
) -> bool:
    try:
        content = build_success_content(judgment, elapsed_sec=elapsed_sec, target_halls=target_halls)
        chunks = message_chunks(content)
        status = 0
        for index, chunk in enumerate(chunks, start=1):
            body = f"({index}/{len(chunks)})\n{chunk}" if len(chunks) > 1 else chunk
            status = send_webhook(body)
    except Exception as exc:
        print(f"Discord通知失敗: {exc}", flush=True)
        return False
    print(f"Discord通知を送信しました (HTTP {status})", flush=True)
    return True


def notify_error(error: str) -> None:
    try:
        if is_captcha_error(error):
            send_webhook(CAPTCHA_NOTICE)
            return
        send_webhook(build_error_content(error))
    except Exception as exc:
        print(f"Discord通知失敗: {exc}", flush=True)


def send_test(message: str | None = None) -> None:
    send_webhook(message or "【テスト通知】Discord通知機能の連携が完了しました！")


def notify_event_capture(*, target_date: str, hall_count: int, handles: list[str]) -> bool:
    """翌日分のイベント取得が完了したとき、1通だけ送る。"""
    parts = str(target_date or "").split("-")
    if len(parts) == 3 and all(part.isdigit() for part in parts):
        label = f"{int(parts[1])}月{int(parts[2])}日"
    else:
        label = target_date or "翌日"
    accounts = "、".join(f"@{str(handle).lstrip('@')}" for handle in handles if handle) or "@PatiSlodays"
    message = (
        f"🟢【スロ極】明日（{label}）のイベント・取材情報をX（{accounts}）より正常に読み込みました！"
        f"（対象: {hall_count}店舗）"
    )
    try:
        status = send_webhook(message)
    except Exception as exc:
        print(f"Discord通知失敗: {exc}", flush=True)
        return False
    print(f"Discordイベント通知を送信しました (HTTP {status})", flush=True)
    return True
