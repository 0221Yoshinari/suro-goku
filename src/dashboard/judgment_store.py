"""判定JSONを Postgres に残す。

Render の無料プランはディスクを持たない。再起動やスリープで
data/judgments が空になっても、画面はこちらを読む。
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

_ENGINE = None
_READY = False
# 県ごとの判定JSONは大きい。画面が連続で開いても、同じ秒数のあいだは再読込しない。
_DURABLE_TTL_SECONDS = 180.0
_CACHE_LOCK = threading.Lock()
_DURABLE_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_SCHEMA_SQL = (
    """
    CREATE TABLE IF NOT EXISTS hall_judgments (
      hall_name TEXT PRIMARY KEY,
      prefecture TEXT NOT NULL DEFAULT '',
      payload TEXT NOT NULL,
      updated_at BIGINT NOT NULL DEFAULT 0
    )
    """,
    # 画面は県で絞って店舗名順に読む。主キーは店舗名だけなので、県の索引が無いと全件走査になる。
    """
    CREATE INDEX IF NOT EXISTS hall_judgments_prefecture_hall_idx
      ON hall_judgments (prefecture, hall_name)
    """,
)


def _postgres_url() -> str:
    if os.getenv("PYTEST_CURRENT_TEST") and not os.getenv("JUDGMENT_STORE_FORCE"):
        return ""
    url = (os.getenv("DATABASE_URL") or "").strip()
    if url.startswith("postgres://") or url.startswith("postgresql://"):
        return url
    return ""


def durable_enabled() -> bool:
    return bool(_postgres_url())


def _normalize_postgres_url(url: str) -> str:
    if url.startswith("postgresql+psycopg://") or url.startswith("postgresql+psycopg2://"):
        return url
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def _engine():
    global _ENGINE
    url = _postgres_url()
    if not url:
        return None
    if _ENGINE is not None:
        return _ENGINE
    from sqlalchemy import create_engine

    _ENGINE = create_engine(
        _normalize_postgres_url(url),
        pool_pre_ping=True,
        connect_args={"connect_timeout": 5},
    )
    return _ENGINE


def _ensure() -> bool:
    global _READY
    engine = _engine()
    if engine is None:
        return False
    if _READY:
        return True
    from sqlalchemy import text

    with engine.begin() as connection:
        for statement in _SCHEMA_SQL:
            connection.execute(text(statement))
    _READY = True
    return True


def clear_durable_cache() -> None:
    with _CACHE_LOCK:
        _DURABLE_CACHE.clear()


def _cached_prefecture(prefecture: str) -> list[dict[str, Any]] | None:
    with _CACHE_LOCK:
        cached = _DURABLE_CACHE.get(prefecture)
        if cached is None or time.monotonic() - cached[0] >= _DURABLE_TTL_SECONDS:
            return None
        return list(cached[1])


def _remember_prefecture(prefecture: str, payloads: list[dict[str, Any]]) -> None:
    with _CACHE_LOCK:
        _DURABLE_CACHE[prefecture] = (time.monotonic(), list(payloads))


def _cache_replace(prefecture: str, payload: dict[str, Any]) -> None:
    """保存済みの県キャッシュがあれば、その店舗だけ差し替える。全件の再読込はしない。"""
    name = str(payload.get("hall_name") or "").strip()
    if not name:
        return
    with _CACHE_LOCK:
        for key, (stamp, rows) in list(_DURABLE_CACHE.items()):
            kept = [row for row in rows if str(row.get("hall_name") or "").strip() != name]
            if len(kept) != len(rows):
                _DURABLE_CACHE[key] = (stamp, kept)
        bucket = _DURABLE_CACHE.get(prefecture)
        if bucket is None:
            return
        stamp, rows = bucket
        rows = [row for row in rows if str(row.get("hall_name") or "").strip() != name]
        rows.append(payload)
        rows.sort(key=lambda row: str(row.get("hall_name") or ""))
        _DURABLE_CACHE[prefecture] = (stamp, rows)


def save_durable(payload: dict[str, Any], prefecture: str) -> bool:
    """1店舗分を台帳へ残す。データベースが無いときは何もしない。"""
    if not _postgres_url():
        return False
    name = str(payload.get("hall_name") or "").strip()
    if not name:
        return False
    try:
        if not _ensure():
            return False
        from sqlalchemy import text

        engine = _engine()
        body = dict(payload)
        body.pop("results", None)
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO hall_judgments (hall_name, prefecture, payload, updated_at)
                    VALUES (:hall_name, :prefecture, :payload, :updated_at)
                    ON CONFLICT (hall_name) DO UPDATE SET
                      prefecture = EXCLUDED.prefecture,
                      payload = EXCLUDED.payload,
                      updated_at = EXCLUDED.updated_at
                    """
                ),
                {
                    "hall_name": name,
                    "prefecture": (prefecture or "").strip().lower(),
                    "payload": json.dumps(body, ensure_ascii=False, separators=(",", ":")),
                    "updated_at": int(time.time()),
                },
            )
    except Exception as exc:
        print(f"判定の永続保存に失敗しました: {type(exc).__name__}", flush=True)
        return False
    _cache_replace((prefecture or "").strip().lower(), body)
    return True


def durable_hall_count(prefecture: str) -> int | None:
    """Postgres に残っている店舗数。データベースが無いときは None。"""
    wanted = (prefecture or "").strip().lower()
    if not wanted or not _postgres_url():
        return None
    try:
        if not _ensure():
            return None
        from sqlalchemy import text

        engine = _engine()
        with engine.connect() as connection:
            row = connection.execute(
                text("SELECT COUNT(*) AS n FROM hall_judgments WHERE prefecture = :prefecture"),
                {"prefecture": wanted},
            ).mappings().one()
    except Exception:
        return None
    return int(row["n"])


def load_durable(prefecture: str) -> list[dict[str, Any]]:
    """指定した県の判定を読む。データベースが無いときは空。"""
    wanted = (prefecture or "").strip().lower()
    if not wanted or not _postgres_url():
        return []
    cached = _cached_prefecture(wanted)
    if cached is not None:
        return cached
    try:
        if not _ensure():
            return []
        from sqlalchemy import text

        engine = _engine()
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT payload FROM hall_judgments
                    WHERE prefecture = :prefecture
                    ORDER BY hall_name
                    """
                ),
                {"prefecture": wanted},
            ).mappings().all()
    except Exception as exc:
        print(f"判定の永続読み込みに失敗しました: {type(exc).__name__}", flush=True)
        return []
    payloads: list[dict[str, Any]] = []
    for row in rows:
        try:
            loaded = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(loaded, dict):
            payloads.append(loaded)
    _remember_prefecture(wanted, payloads)
    return payloads
