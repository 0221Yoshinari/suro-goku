"""運営LINEグループへのデイリーマーケティングレポート。

送信先は TARGET_LINE_GROUP_ID だけ。グループと複数人トークでは、
ウェルカムもコマンド応答も返さない。未設定のあいだは、そこでの
メッセージか参加イベントから ID を一度だけ記録する。
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from datetime import time as dt_time
from datetime import timedelta
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger("surogoku.marketing")

JST = ZoneInfo("Asia/Tokyo")
_GROUP_KEY = "target_group_id"
_GROUP_ID_RE = re.compile(r"^[CR][0-9a-fA-F]{32}$")
_IMPRESSION_RE = re.compile(r"^(?:インプレッション|インプ)\s*[:：]?\s*([0-9][0-9,]*)\s*$")
_MAX_IMPRESSIONS = 1_000_000_000_000
_SEPARATOR = "-" * 30
_CLAIM_STALE_SEC = 600

_DDL = (
    """
    CREATE TABLE IF NOT EXISTS marketing_settings (
      key TEXT PRIMARY KEY,
      value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS line_audience_events (
      event_id TEXT PRIMARY KEY,
      user_id TEXT NOT NULL DEFAULT '',
      kind TEXT NOT NULL,
      occurred_at BIGINT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS line_audience_events_kind_time_idx
      ON line_audience_events (kind, occurred_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS x_impression_days (
      day TEXT PRIMARY KEY,
      impressions BIGINT NOT NULL,
      recorded_at BIGINT NOT NULL
    )
    """,
)

_READY: set[str] = set()
_SENT: set[str] = set()
_SKIP_LOGGED: set[str] = set()
_LOCK = threading.Lock()
_START_LOCK = threading.Lock()
_STOP = threading.Event()
_STARTED = False


class ReportDeliveryError(Exception):
    """運営グループ以外への送信を拒んだ。"""


@dataclass(frozen=True)
class IngestResult:
    consume: bool = False
    reply: str = ""


def report_loop_enabled() -> bool:
    flag = (os.getenv("MARKETING_REPORT_LOOP") or "").strip().lower()
    if flag in {"0", "false", "no", "off"}:
        return False
    if flag in {"1", "true", "yes", "on"}:
        return True
    return "pytest" not in sys.modules


def start_daily_report() -> None:
    """23:50 JST に1通だけ送るループ。pytest 中は動かさない。"""
    global _STARTED
    if not report_loop_enabled():
        return
    with _START_LOCK:
        if _STARTED:
            return
        _STARTED = True
        threading.Thread(target=_loop, name="daily-marketing-report", daemon=True).start()


def _loop() -> None:
    while not _STOP.is_set():
        try:
            send_scheduled()
        except Exception:
            logger.exception("デイリーレポートの送信に失敗しました")
        if _STOP.wait(30):
            return


def scheduled_end(now: datetime) -> datetime | None:
    """23:50 から翌朝 6:00 までは、その日の 23:50 時点のレポートを送る。"""
    current = _as_jst(now)
    stamp = current.timetz().replace(tzinfo=None)
    if stamp >= dt_time(23, 50):
        day = current.date()
        end = datetime.combine(day, dt_time(23, 50), tzinfo=JST)
        return current if current >= end else end
    if stamp < dt_time(6, 0):
        day = current.date() - timedelta(days=1)
        return datetime.combine(day, dt_time(23, 50), tzinfo=JST)
    return None


def send_scheduled(now: datetime | None = None) -> bool:
    current = _as_jst(now or datetime.now(JST))
    end = scheduled_end(current)
    if end is None:
        return False
    if _day_key(end) in _SENT:
        return False
    return send_daily_report(end, mark=True)


def send_daily_report(at: datetime, *, mark: bool = True) -> bool:
    """過去24時間と前日同時間帯の差分を、運営グループへだけ送る。"""
    target = target_group_id()
    if not target:
        _log_skip(at)
        return False
    day = _day_key(at)
    if mark and not _claim(day):
        return False
    try:
        text = build_report(collect_metrics(at), at)
        status = push_text(target, text)
    except Exception:
        if mark:
            _release(day)
        raise
    if status >= 400:
        logger.error("デイリーレポートの送信に失敗しました status=%s", status)
        if mark:
            _release(day)
        return False
    if mark:
        _finish(day)
        _SENT.add(day)
    print(f"デイリーマーケティングレポートを送信しました ({day})", flush=True)
    return True


def push_text(group_id: str, text: str) -> int:
    """TARGET_LINE_GROUP_ID と一致するときだけ LINE へ push する。"""
    target = target_group_id()
    if not text or not target or not _same_group(group_id, target):
        logger.error("運営グループ以外への送信を拒否しました")
        raise ReportDeliveryError("運営グループ以外へは送信しません")
    token = (os.getenv("LINE_CHANNEL_ACCESS_TOKEN") or "").strip()
    if not token:
        raise ReportDeliveryError("LINEのアクセストークンが未設定です")
    response = httpx.post(
        "https://api.line.me/v2/bot/message/push",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"to": target, "messages": [{"type": "text", "text": text}]},
        timeout=10.0,
    )
    return int(response.status_code)


def target_group_id() -> str:
    """環境変数が正しければそれを優先し、台帳へ写す。空なら台帳の値。"""
    raw = _env_raw()
    if raw:
        if not _GROUP_ID_RE.fullmatch(raw):
            return ""
        stored = _db_group_id()
        if stored.casefold() != raw.casefold():
            _put_setting(_GROUP_KEY, raw)
        return raw
    return _db_group_id()


def is_target_group(group_id: str) -> bool:
    target = target_group_id()
    return bool(target) and _same_group(group_id, target)


def remember_group_id(group_id: str) -> bool:
    """未設定のときだけ groupId を記録する。2件目以降は捨てる。"""
    candidate = (group_id or "").strip()
    if not _GROUP_ID_RE.fullmatch(candidate) or _env_raw():
        return False
    with _LOCK:
        if _env_raw() or _db_group_id():
            return False
        _put_setting(_GROUP_KEY, candidate)
        os.environ["TARGET_LINE_GROUP_ID"] = candidate
    print(f"TARGET_LINE_GROUP_ID を記録しました: {candidate}", flush=True)
    logger.info("TARGET_LINE_GROUP_ID を記録しました: %s", candidate)
    return True


def is_multi_party_event(event: dict) -> bool:
    """グループと複数人トーク。ここには自動返信しない。"""
    source = event.get("source") if isinstance(event.get("source"), dict) else {}
    return _is_multi_party(source)


def ingest_line_event(event: dict, *, now: datetime | None = None) -> IngestResult:
    try:
        return _ingest_line_event(event, now=now)
    except Exception:
        logger.exception("LINEイベントの記録に失敗しました")
        source = event.get("source") if isinstance(event.get("source"), dict) else {}
        if _is_multi_party(source):
            return IngestResult(consume=True)
        return IngestResult(consume=False)


def parse_impressions(text: str) -> int | None:
    match = _IMPRESSION_RE.match((text or "").strip())
    if not match:
        return None
    try:
        value = int(match.group(1).replace(",", ""))
    except ValueError:
        return None
    if value < 0 or value > _MAX_IMPRESSIONS:
        return None
    return value


def collect_metrics(at: datetime) -> dict:
    """過去24時間と、その直前の24時間。インプレッションは日付ごとの記録値。"""
    current = _as_jst(at)
    end = int(current.timestamp())
    start = end - 86400
    previous_start = end - 172800
    day = current.date()
    return {
        "impressions": _impression_on(day.isoformat()),
        "impressions_prev": _impression_on((day - timedelta(days=1)).isoformat()),
        "follows": _audience_count("follow", start, end),
        "follows_prev": _audience_count("follow", previous_start, start),
        "blocks": _audience_count("unfollow", start, end),
        "blocks_prev": _audience_count("unfollow", previous_start, start),
        "emails": _email_count(start, end),
        "emails_prev": _email_count(previous_start, start),
        "paid": _paid_count(start, end),
        "paid_prev": _paid_count(previous_start, start),
    }


def build_report(metrics: dict, at: datetime) -> str:
    day = _as_jst(at).strftime("%Y/%m/%d")
    follows = int(metrics["follows"])
    paid = int(metrics["paid"])
    lines = [
        "［スロ極 デイリーマーケティングレポート］",
        f"📅 日付: {day}",
        _SEPARATOR,
        "■ X (Twitter)",
        _impression_line(metrics["impressions"], metrics["impressions_prev"]),
        "",
        "■ LINE",
        _people_line("新規友だち追加", follows, int(metrics["follows_prev"])),
        _people_line("ブロック数", int(metrics["blocks"]), int(metrics["blocks_prev"])),
        "",
        "■ Webアプリ (スロ極)",
        _people_line("メアド登録数", int(metrics["emails"]), int(metrics["emails_prev"])),
        _paid_line(paid, int(metrics["paid_prev"]), follows),
        _SEPARATOR,
        "本日もお疲れ様でした！",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    now = datetime.now(JST)
    if "--if-due" in args:
        if scheduled_end(now) is None:
            return 0
        return 0 if send_scheduled(now) else 1
    return 0 if send_daily_report(now, mark=False) else 1


def _ingest_line_event(event: dict, *, now: datetime | None) -> IngestResult:
    at = _as_jst(now or datetime.now(JST))
    source = event.get("source") if isinstance(event.get("source"), dict) else {}
    if _is_multi_party(source):
        party_id = str(source.get("groupId") or source.get("roomId") or "")
        return _ingest_multi_party(event, party_id, at)
    kind = str(event.get("type") or "")
    if kind in {"follow", "unfollow"}:
        _record_audience(event, kind, int(at.timestamp()))
        return IngestResult(consume=kind == "unfollow")
    return IngestResult(consume=False)


def _ingest_multi_party(event: dict, party_id: str, at: datetime) -> IngestResult:
    """グループと複数人トークでは返信せず、ID の記録とインプレッションの保存だけ行う。"""
    kind = str(event.get("type") or "")
    if kind in {"message", "join"}:
        remember_group_id(party_id)
    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    if kind == "message" and str(message.get("type") or "") == "text" and is_target_group(party_id):
        value = parse_impressions(str(message.get("text") or ""))
        if value is not None:
            _store_impressions(party_id, value, _stamp_to_datetime(event, at))
    return IngestResult(consume=True, reply="")


def _record_audience(event: dict, kind: str, fallback: int) -> None:
    if kind not in {"follow", "unfollow"}:
        return
    source = event.get("source") if isinstance(event.get("source"), dict) else {}
    if str(source.get("type") or "user") not in {"user", ""}:
        return
    user_id = str(source.get("userId") or "")[:100]
    occurred = _event_time(event, fallback)
    event_id = str(event.get("webhookEventId") or "").strip()[:200]
    if not event_id:
        event_id = f"{kind}:{user_id}:{occurred}"
    _write(
        """
        INSERT INTO line_audience_events (event_id, user_id, kind, occurred_at)
        VALUES (:event_id, :user_id, :kind, :occurred_at)
        ON CONFLICT (event_id) DO NOTHING
        """,
        {"event_id": event_id, "user_id": user_id, "kind": kind, "occurred_at": occurred},
    )


def _store_impressions(group_id: str, value: int, at: datetime) -> bool:
    if not is_target_group(group_id):
        return False
    _write(
        """
        INSERT INTO x_impression_days (day, impressions, recorded_at)
        VALUES (:day, :impressions, :recorded_at)
        ON CONFLICT (day) DO UPDATE SET
          impressions = excluded.impressions,
          recorded_at = excluded.recorded_at
        """,
        {"day": _day_key(at), "impressions": int(value), "recorded_at": int(_as_jst(at).timestamp())},
    )
    return True


def _audience_count(kind: str, start: int, end: int) -> int:
    return _count(
        """
        SELECT COUNT(*) FROM line_audience_events
        WHERE kind = :kind AND occurred_at >= :start AND occurred_at < :end
        """,
        {"kind": kind, "start": start, "end": end},
    )


def _email_count(start: int, end: int) -> int:
    rows = _rows(
        """
        SELECT email FROM square_members
        WHERE email <> '' AND registered_at >= :start AND registered_at < :end
        """,
        {"start": start, "end": end},
    )
    seen = {str(row[0] or "").strip().lower() for row in rows if str(row[0] or "").strip()}
    extra = _members_rows(
        """
        SELECT email FROM members
        WHERE email <> '' AND created_at >= :start AND created_at < :end
        """,
        {"start": start, "end": end},
    )
    for row in extra:
        email = str(row[0] or "").strip().lower()
        if email:
            seen.add(email)
    return len(seen)


def _paid_count(start: int, end: int) -> int:
    """カード決済の成功。現行は Square の paid_at。残っている Stripe 成功も足す。"""
    square = _count(
        """
        SELECT COUNT(*) FROM square_members
        WHERE paid_at IS NOT NULL AND paid_at >= :start AND paid_at < :end
          AND status_source = 'square'
        """,
        {"start": start, "end": end},
    )
    stripe = _members_count(
        """
        SELECT COUNT(*) FROM members
        WHERE stripe_subscription_id <> ''
          AND status = 'active'
          AND status_source IN ('checkout', 'subscription')
          AND status_event_at >= :start AND status_event_at < :end
          AND (trial_end IS NULL OR trial_end = 0)
        """,
        {"start": start, "end": end},
    )
    return square + stripe


def _impression_on(day: str) -> int | None:
    rows = _rows(
        "SELECT impressions FROM x_impression_days WHERE day = :day",
        {"day": day},
    )
    if not rows:
        return None
    return int(rows[0][0])


def _impression_line(current: int | None, previous: int | None) -> str:
    if current is None:
        return "・インプレッション数: 未取得 (前日比: —)"
    shown = f"{int(current):,}"
    if previous is None:
        return f"・インプレッション数: {shown} (前日比: —)"
    return f"・インプレッション数: {shown} (前日比: {_delta(int(current), int(previous))})"


def _people_line(label: str, current: int, previous: int) -> str:
    return f"・{label}: {current:,}人 (前日比: {_delta(current, previous, '人')})"


def _paid_line(paid: int, previous: int, follows: int) -> str:
    """CVR は、その24時間の課金者数を新規友だち追加数で割った値。"""
    if follows <= 0:
        cvr = "—"
    else:
        cvr = f"{(paid * 100) / follows:.1f}%"
    return f"・カード決済(課金): {paid:,}人 (前日比: {_delta(paid, previous, '人')} / CVR: {cvr})"


def _delta(current: int, previous: int, unit: str = "") -> str:
    diff = current - previous
    sign = "+" if diff >= 0 else "-"
    return f"{sign}{abs(diff):,}{unit}"


def _claim(day: str) -> bool:
    key = f"report:{day}"
    now = int(time.time())
    if _insert_new(key, f"sending:{now}"):
        return True
    current = _setting(key)
    if current == "sent":
        _SENT.add(day)
        return False
    if current.startswith("sending:"):
        try:
            started = int(current.split(":", 1)[1])
        except ValueError:
            started = 0
        if now - started < _CLAIM_STALE_SEC:
            return False
        return _replace_setting(key, current, f"sending:{now}")
    return False


def _finish(day: str) -> None:
    _put_setting(f"report:{day}", "sent")


def _release(day: str) -> None:
    _write(
        "DELETE FROM marketing_settings WHERE key = :key AND value LIKE 'sending:%'",
        {"key": f"report:{day}"},
    )


def _log_skip(at: datetime) -> None:
    day = _day_key(at)
    if day in _SKIP_LOGGED:
        return
    _SKIP_LOGGED.add(day)
    raw = _env_raw()
    if raw and not _GROUP_ID_RE.fullmatch(raw):
        print("TARGET_LINE_GROUP_ID の形式が不正なため、デイリーレポートを送りません。", flush=True)
        return
    print("TARGET_LINE_GROUP_ID が未設定のため、デイリーレポートを送りません。", flush=True)


def _env_raw() -> str:
    return (os.getenv("TARGET_LINE_GROUP_ID") or "").strip()


def _db_group_id() -> str:
    value = _setting(_GROUP_KEY).strip()
    if not _GROUP_ID_RE.fullmatch(value):
        return ""
    return value


def _same_group(left: str, right: str) -> bool:
    return bool(left) and bool(right) and left.casefold() == right.casefold()


def _is_multi_party(source: dict) -> bool:
    kind = str(source.get("type") or "")
    if kind in {"group", "room"}:
        return True
    return bool(str(source.get("groupId") or "").strip() or str(source.get("roomId") or "").strip())


def _as_jst(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=JST)
    return value.astimezone(JST)


def _day_key(at: datetime) -> str:
    return _as_jst(at).date().isoformat()


def _event_time(event: dict, fallback: int) -> int:
    try:
        stamp = int(event.get("timestamp"))
    except (TypeError, ValueError):
        return fallback
    if stamp > 10_000_000_000:
        return stamp // 1000
    if stamp > 0:
        return stamp
    return fallback


def _stamp_to_datetime(event: dict, fallback: datetime) -> datetime:
    return datetime.fromtimestamp(_event_time(event, int(_as_jst(fallback).timestamp())), JST)


def _setting(key: str) -> str:
    rows = _rows("SELECT value FROM marketing_settings WHERE key = :key", {"key": key})
    if not rows:
        return ""
    return str(rows[0][0] or "")


def _put_setting(key: str, value: str) -> None:
    _write(
        """
        INSERT INTO marketing_settings (key, value) VALUES (:key, :value)
        ON CONFLICT (key) DO UPDATE SET value = excluded.value
        """,
        {"key": key, "value": value},
    )


def _insert_new(key: str, value: str) -> bool:
    return (
        _rowcount(
            """
            INSERT INTO marketing_settings (key, value) VALUES (:key, :value)
            ON CONFLICT (key) DO NOTHING
            """,
            {"key": key, "value": value},
        )
        == 1
    )


def _replace_setting(key: str, expected: str, value: str) -> bool:
    return (
        _rowcount(
            """
            UPDATE marketing_settings SET value = :value
            WHERE key = :key AND value = :expected
            """,
            {"key": key, "value": value, "expected": expected},
        )
        == 1
    )


def _count(sql: str, params: dict) -> int:
    rows = _rows(sql, params)
    return int(rows[0][0] if rows else 0)


def _members_rows(sql: str, params: dict) -> list:
    try:
        return _rows(sql, params)
    except Exception as exc:
        message = str(exc).lower()
        if "no such table" in message or "does not exist" in message or "undefinedtable" in message:
            return []
        raise


def _members_count(sql: str, params: dict) -> int:
    rows = _members_rows(sql, params)
    if not rows:
        return 0
    return int(rows[0][0] or 0)


def _rows(sql: str, params: dict) -> list[tuple]:
    kind, target = _prepare()
    statement = " ".join(sql.split())
    if kind == "postgres":
        from sqlalchemy import text

        engine = _engine(str(target))
        with engine.connect() as connection:
            found = connection.execute(text(statement), params).all()
        return [tuple(row) for row in found]
    connection = sqlite3.connect(str(target), timeout=5)
    try:
        found = connection.execute(statement, params).fetchall()
    finally:
        connection.close()
    return [tuple(row) for row in found]


def _write(sql: str, params: dict) -> None:
    _rowcount(sql, params)


def _rowcount(sql: str, params: dict) -> int:
    kind, target = _prepare()
    statement = " ".join(sql.split())
    if kind == "postgres":
        from sqlalchemy import text

        engine = _engine(str(target))
        with engine.begin() as connection:
            result = connection.execute(text(statement), params)
            count = result.rowcount
        return int(count if count is not None and count >= 0 else 0)
    connection = sqlite3.connect(str(target), timeout=5)
    try:
        cursor = connection.execute(statement, params)
        connection.commit()
        count = cursor.rowcount
    finally:
        connection.close()
    return int(count if count is not None and count >= 0 else 0)


def _prepare() -> tuple[str, str]:
    from src.dashboard.square_members import _backend

    kind, target = _backend()
    key = f"{kind}:{target}"
    if key not in _READY:
        for statement in _DDL:
            _exec_ddl(kind, str(target), statement)
        _READY.add(key)
    return kind, str(target)


def _exec_ddl(kind: str, target: str, statement: str) -> None:
    sql = " ".join(statement.split())
    if kind == "postgres":
        from sqlalchemy import text

        engine = _engine(target)
        with engine.begin() as connection:
            connection.execute(text(sql))
        return
    connection = sqlite3.connect(target, timeout=5)
    try:
        connection.execute(sql)
        connection.commit()
    finally:
        connection.close()


def _engine(url: str):
    from src.dashboard.square_members import _engine as square_engine

    return square_engine(url)
