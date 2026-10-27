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
    """
    CREATE TABLE IF NOT EXISTS prefecture_snapshots (
      prefecture TEXT PRIMARY KEY,
      source_stamp TEXT NOT NULL DEFAULT '',
      events_mtime DOUBLE PRECISION NOT NULL DEFAULT 0,
      judged_at TEXT NOT NULL DEFAULT '',
      fetched_at TEXT NOT NULL DEFAULT '',
      events_updated_at TEXT NOT NULL DEFAULT '',
      machine_count INTEGER NOT NULL DEFAULT 0,
      unit_count INTEGER NOT NULL DEFAULT 0,
      lineup_ready BOOLEAN NOT NULL DEFAULT FALSE,
      payload_bytes INTEGER NOT NULL DEFAULT 0,
      build_ms INTEGER NOT NULL DEFAULT 0,
      generated_at BIGINT NOT NULL DEFAULT 0,
      payload TEXT NOT NULL
    )
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

    # 無料枠は 512MB。接続を溜めると、県の組み立てと同時にメモリを押す。
    _ENGINE = create_engine(
        _normalize_postgres_url(url),
        pool_pre_ping=True,
        pool_size=2,
        max_overflow=1,
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


def drop_durable_prefecture(prefecture: str) -> None:
    """県の完成品を書き終えたあと、台明細込みのメモリを手放す。"""
    with _CACHE_LOCK:
        _DURABLE_CACHE.pop((prefecture or "").strip().lower(), None)


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
    saved_prefecture = (prefecture or "").strip().lower()
    if saved_prefecture:
        try:
            from src.dashboard.prefecture_snapshot import schedule

            schedule(saved_prefecture)
        except Exception as exc:
            print(f"県の完成品の更新予約に失敗しました: {type(exc).__name__}", flush=True)
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


def iter_durable_payloads(prefecture: str):
    """県の原本を1店舗ずつ返す。全店舗を同時には持たない。"""
    wanted = (prefecture or "").strip().lower()
    if not wanted or not _postgres_url():
        return
    try:
        if not _ensure():
            return
        from sqlalchemy import text

        engine = _engine()
        statement = text(
            """
            SELECT payload FROM hall_judgments
            WHERE prefecture = :prefecture
            ORDER BY hall_name
            """
        )
        with engine.connect() as connection:
            result = connection.execution_options(yield_per=1).execute(
                statement,
                {"prefecture": wanted},
            )
            for row in result.mappings():
                try:
                    loaded = json.loads(row["payload"])
                except (TypeError, json.JSONDecodeError):
                    continue
                if isinstance(loaded, dict) and str(loaded.get("hall_name") or "").strip():
                    yield loaded
    except Exception as exc:
        print(f"判定の分割読み込みに失敗しました: {type(exc).__name__}", flush=True)
        return


_RECEIPT_SQL = """
SELECT hall_name,
       CASE
         WHEN jsonb_typeof(payload::jsonb -> 'machines') = 'array'
         THEN jsonb_array_length(payload::jsonb -> 'machines')
         ELSE 0
       END AS machine_count,
       COALESCE(payload::jsonb ->> 'fetched_at', '') AS fetched_at
FROM hall_judgments
WHERE prefecture = :prefecture
ORDER BY hall_name
"""

_RECEIPT_NAMES_SQL = """
SELECT hall_name
FROM hall_judgments
WHERE prefecture = :prefecture
ORDER BY hall_name
"""


def receipt_counts(prefecture: str) -> dict[str, Any] | None:
    """件数だけ返す。台明細のJSONはアプリのメモリに載らない。"""
    wanted = (prefecture or "").strip().lower()
    if not wanted or not _postgres_url():
        return None
    try:
        if not _ensure():
            return None
        from sqlalchemy import text

        engine = _engine()
        with engine.connect() as connection:
            try:
                rows = connection.execute(text(_RECEIPT_SQL), {"prefecture": wanted}).mappings().all()
                exact = True
            except Exception:
                connection.rollback()
                rows = connection.execute(text(_RECEIPT_NAMES_SQL), {"prefecture": wanted}).mappings().all()
                exact = False
    except Exception as exc:
        print(f"判定件数の読み込みに失敗しました: {type(exc).__name__}", flush=True)
        return None
    halls: list[str] = []
    machine_count = 0
    fetched_at = ""
    for row in rows:
        name = str(row.get("hall_name") or "").strip()
        if name:
            halls.append(name)
        if exact:
            machine_count += int(row.get("machine_count") or 0)
            fetched_at = max(fetched_at, str(row.get("fetched_at") or ""))
    if not exact:
        machine_count = len(halls)
    return {
        "ok": True,
        "prefecture": wanted,
        "machine_count": machine_count,
        "hall_count": len(halls),
        "halls": halls,
        "fetched_at": fetched_at or None,
        "durable": True,
        "durable_halls": len(halls),
    }


def load_hall_payload(hall_name: str) -> dict[str, Any] | None:
    """1店舗の原本だけ読む。県全体は組み立てない。"""
    name = (hall_name or "").strip()
    if not name or not _postgres_url():
        return None
    try:
        if not _ensure():
            return None
        from sqlalchemy import text

        engine = _engine()
        with engine.connect() as connection:
            row = connection.execute(
                text("SELECT payload FROM hall_judgments WHERE hall_name = :hall_name"),
                {"hall_name": name},
            ).mappings().first()
    except Exception as exc:
        print(f"店舗判定の読み込みに失敗しました: {type(exc).__name__}", flush=True)
        return None
    if row is None:
        return None
    try:
        loaded = json.loads(row["payload"])
    except (TypeError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def hall_source_stamps() -> dict[str, str]:
    """県ごとの原本の印。店舗数と最終保存時刻。データベースが無いときは空。"""
    if not _postgres_url():
        return {}
    try:
        if not _ensure():
            return {}
        from sqlalchemy import text

        engine = _engine()
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT prefecture, COUNT(*) AS n, COALESCE(MAX(updated_at), 0) AS updated
                    FROM hall_judgments
                    WHERE prefecture <> ''
                    GROUP BY prefecture
                    """
                )
            ).mappings().all()
    except Exception as exc:
        print(f"県の更新印を読めませんでした: {type(exc).__name__}", flush=True)
        return {}
    stamps: dict[str, str] = {}
    for row in rows:
        prefecture = str(row["prefecture"] or "").strip().lower()
        if prefecture:
            stamps[prefecture] = f"{int(row['n'])}:{int(row['updated'])}"
    return stamps


def save_prefecture_snapshot(
    prefecture: str,
    *,
    source_stamp: str,
    events_mtime: float,
    judged_at: str,
    fetched_at: str,
    events_updated_at: str,
    machine_count: int,
    unit_count: int,
    lineup_ready: bool,
    payload: str,
    build_ms: int,
) -> bool:
    """県の画面用JSONを1行で残す。開くリクエストはこの行だけ読む。"""
    wanted = (prefecture or "").strip().lower()
    if not wanted or not _postgres_url():
        return False
    try:
        if not _ensure():
            return False
        from sqlalchemy import text

        engine = _engine()
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO prefecture_snapshots (
                      prefecture, source_stamp, events_mtime, judged_at, fetched_at,
                      events_updated_at, machine_count, unit_count, lineup_ready,
                      payload_bytes, build_ms, generated_at, payload
                    ) VALUES (
                      :prefecture, :source_stamp, :events_mtime, :judged_at, :fetched_at,
                      :events_updated_at, :machine_count, :unit_count, :lineup_ready,
                      :payload_bytes, :build_ms, :generated_at, :payload
                    )
                    ON CONFLICT (prefecture) DO UPDATE SET
                      source_stamp = EXCLUDED.source_stamp,
                      events_mtime = EXCLUDED.events_mtime,
                      judged_at = EXCLUDED.judged_at,
                      fetched_at = EXCLUDED.fetched_at,
                      events_updated_at = EXCLUDED.events_updated_at,
                      machine_count = EXCLUDED.machine_count,
                      unit_count = EXCLUDED.unit_count,
                      lineup_ready = EXCLUDED.lineup_ready,
                      payload_bytes = EXCLUDED.payload_bytes,
                      build_ms = EXCLUDED.build_ms,
                      generated_at = EXCLUDED.generated_at,
                      payload = EXCLUDED.payload
                    """
                ),
                {
                    "prefecture": wanted,
                    "source_stamp": source_stamp,
                    "events_mtime": float(events_mtime),
                    "judged_at": judged_at,
                    "fetched_at": fetched_at,
                    "events_updated_at": events_updated_at,
                    "machine_count": int(machine_count),
                    "unit_count": int(unit_count),
                    "lineup_ready": bool(lineup_ready),
                    "payload_bytes": len(payload.encode("utf-8")),
                    "build_ms": int(build_ms),
                    "generated_at": int(time.time()),
                    "payload": payload,
                },
            )
    except Exception as exc:
        print(f"県の完成品の保存に失敗しました: {type(exc).__name__}", flush=True)
        return False
    return True


def load_prefecture_snapshot(prefecture: str) -> dict[str, Any] | None:
    """保存済みの画面用JSON。未作成のときは None。"""
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
                text(
                    """
                    SELECT source_stamp, events_mtime, payload
                    FROM prefecture_snapshots
                    WHERE prefecture = :prefecture
                    """
                ),
                {"prefecture": wanted},
            ).mappings().first()
    except Exception as exc:
        print(f"県の完成品の読み込みに失敗しました: {type(exc).__name__}", flush=True)
        return None
    if row is None:
        return None
    payload = row["payload"]
    if not isinstance(payload, str) or not payload:
        return None
    return {
        "source_stamp": str(row["source_stamp"] or ""),
        "events_mtime": float(row["events_mtime"] or 0),
        "payload": payload,
    }


def load_prefecture_snapshot_meta(prefecture: str) -> dict[str, Any] | None:
    """更新確認用。完成品の本文は読まない。"""
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
                text(
                    """
                    SELECT judged_at, fetched_at, events_updated_at, machine_count, unit_count, lineup_ready, events_mtime
                    FROM prefecture_snapshots
                    WHERE prefecture = :prefecture
                    """
                ),
                {"prefecture": wanted},
            ).mappings().first()
    except Exception:
        return None
    if row is None:
        return None
    return {
        "judged_at": str(row["judged_at"] or "") or None,
        "fetched_at": str(row["fetched_at"] or "") or None,
        "events_updated_at": str(row["events_updated_at"] or "") or None,
        "machine_count": int(row["machine_count"] or 0),
        "unit_count": int(row["unit_count"] or 0),
        "lineup_ready": bool(row["lineup_ready"]),
        "events_mtime": float(row["events_mtime"] or 0),
    }


def snapshot_source_stamps() -> dict[str, str]:
    """保存済み完成品が、どの原本の印で作られたか。"""
    if not _postgres_url():
        return {}
    try:
        if not _ensure():
            return {}
        from sqlalchemy import text

        engine = _engine()
        with engine.connect() as connection:
            rows = connection.execute(
                text("SELECT prefecture, source_stamp FROM prefecture_snapshots")
            ).mappings().all()
    except Exception:
        return {}
    return {
        str(row["prefecture"] or ""): str(row["source_stamp"] or "")
        for row in rows
        if str(row["prefecture"] or "")
    }


def list_prefecture_snapshot_meta() -> list[dict[str, Any]]:
    """ヘルスチェック用。本文は返さない。"""
    if not _postgres_url():
        return []
    try:
        if not _ensure():
            return []
        from sqlalchemy import text

        engine = _engine()
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT prefecture, machine_count, unit_count, payload_bytes, build_ms, generated_at
                    FROM prefecture_snapshots
                    ORDER BY prefecture
                    """
                )
            ).mappings().all()
    except Exception:
        return []
    return [
        {
            "prefecture": str(row["prefecture"] or ""),
            "machine_count": int(row["machine_count"] or 0),
            "unit_count": int(row["unit_count"] or 0),
            "payload_bytes": int(row["payload_bytes"] or 0),
            "build_ms": int(row["build_ms"] or 0),
            "generated_at": int(row["generated_at"] or 0),
        }
        for row in rows
    ]
