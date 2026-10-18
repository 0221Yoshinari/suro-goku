"""取得したホール判定をデータベースへ上書き保存する。

DATABASE_URL が PostgreSQL ならそのデータベースへ書く。
接続できないときは sqlite に落とす。URL が空のときは何もしない。
判定の本体は従来どおり data/judgments/latest.json に残す。
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from src.scraper.log_safe import emit, failure_reason

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SCHEMA = """
CREATE TABLE IF NOT EXISTS hall_snapshots (
  hall_name TEXT PRIMARY KEY,
  prefecture TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT '',
  judged_at TEXT NOT NULL DEFAULT '',
  fetched_at TEXT NOT NULL DEFAULT '',
  machine_count INTEGER NOT NULL DEFAULT 0,
  unit_count INTEGER NOT NULL DEFAULT 0,
  all_high_count INTEGER NOT NULL DEFAULT 0,
  high_56_count INTEGER NOT NULL DEFAULT 0,
  high_base_count INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL DEFAULT ''
);
"""
_UPSERT = """
INSERT INTO hall_snapshots (
  hall_name, prefecture, source, judged_at, fetched_at,
  machine_count, unit_count, all_high_count, high_56_count, high_base_count, updated_at
) VALUES (
  :hall_name, :prefecture, :source, :judged_at, :fetched_at,
  :machine_count, :unit_count, :all_high_count, :high_56_count, :high_base_count, :updated_at
)
ON CONFLICT(hall_name) DO UPDATE SET
  prefecture = excluded.prefecture,
  source = excluded.source,
  judged_at = excluded.judged_at,
  fetched_at = excluded.fetched_at,
  machine_count = excluded.machine_count,
  unit_count = excluded.unit_count,
  all_high_count = excluded.all_high_count,
  high_56_count = excluded.high_56_count,
  high_base_count = excluded.high_base_count,
  updated_at = excluded.updated_at
"""
_FAILED_POSTGRES: set[str] = set()
_LOGGED_SKIP = False


def database_url() -> str:
    return (os.environ.get("DATABASE_URL") or "").strip()


def persist_fetched_halls(judgment: dict, hall_names: list[str] | None) -> str:
    """今回取得できた店舗だけを更新する。失敗しても取得処理は止めない。"""
    try:
        return _persist(judgment, hall_names or [])
    except Exception as exc:
        emit(f"ホール結果をデータベースへ保存できませんでした: {failure_reason(exc)}", flush=True)
        return "failed"


def load_hall_snapshots() -> list[dict]:
    kind, target = _resolve()
    if kind == "postgres":
        return _postgres_load(str(target))
    if kind == "sqlite":
        return _sqlite_load(Path(target))
    return []


def _persist(judgment: dict, hall_names: list[str]) -> str:
    global _LOGGED_SKIP
    kind, target = _resolve()
    if kind == "skip":
        if not _LOGGED_SKIP:
            emit("DATABASE_URL が無いため、ホール結果は判定JSONだけに残します", flush=True)
            _LOGGED_SKIP = True
        return "skipped"
    rows = _rows(judgment, hall_names)
    if not rows:
        return kind
    if kind == "postgres":
        _postgres_upsert(str(target), rows)
    else:
        _sqlite_upsert(Path(target), rows)
    emit(f"ホール結果を保存しました: {kind} / {len(rows)}店舗", flush=True)
    return kind


def _rows(judgment: dict, hall_names: list[str]) -> list[dict]:
    from src.scraper.halls import hall_locale_index

    counts = judgment.get("hall_counts") if isinstance(judgment.get("hall_counts"), dict) else {}
    locales = hall_locale_index()
    now = datetime.now(timezone.utc).isoformat()
    judged_at = str(judgment.get("judged_at") or "")
    fetched_at = str(judgment.get("fetched_at") or "")
    source = str(judgment.get("source") or "")
    rows: list[dict] = []
    seen: set[str] = set()
    for name in hall_names:
        hall = str(name or "").strip()
        if not hall or hall in seen:
            continue
        seen.add(hall)
        item = counts.get(hall) if isinstance(counts.get(hall), dict) else {}
        locale = locales.get(hall) or {}
        rows.append(
            {
                "hall_name": hall,
                "prefecture": str(locale.get("prefecture") or ""),
                "source": source,
                "judged_at": judged_at,
                "fetched_at": fetched_at,
                "machine_count": int(item.get("machine_count") or 0),
                "unit_count": int(item.get("count") or 0),
                "all_high_count": int(item.get("all_high_count") or 0),
                "high_56_count": int(item.get("high_56_count") or 0),
                "high_base_count": int(item.get("high_base_count") or 0),
                "updated_at": now,
            }
        )
    return rows


def _resolve() -> tuple[str, str | Path]:
    url = database_url()
    if not url:
        return "skip", ""
    if url.startswith("sqlite"):
        path = _sqlite_path(url)
        _ensure_sqlite(path)
        return "sqlite", path
    if url.startswith("postgres"):
        if url in _FAILED_POSTGRES:
            path = _fallback_sqlite_path()
            _ensure_sqlite(path)
            return "sqlite", path
        try:
            _ensure_postgres(url)
        except Exception as exc:
            _FAILED_POSTGRES.add(url)
            emit(f"PostgreSQLを使えないためホール結果は sqlite に保存します: {type(exc).__name__}", flush=True)
            path = _fallback_sqlite_path()
            _ensure_sqlite(path)
            return "sqlite", path
        return "postgres", url
    emit("DATABASE_URL の形式を確認できないため、ホール結果は判定JSONだけに残します", flush=True)
    return "skip", ""


def _fallback_sqlite_path() -> Path:
    raw = (os.environ.get("HALL_SQLITE_PATH") or "").strip()
    if raw:
        return Path(raw)
    return PROJECT_ROOT / "data" / "hall_snapshots.sqlite"


def _sqlite_path(url: str) -> Path:
    parsed = urlparse(url)
    path = parsed.path or ""
    if url.startswith("sqlite:////"):
        return Path(path)
    relative = path.lstrip("/")
    if not relative:
        return _fallback_sqlite_path()
    candidate = Path(relative)
    if candidate.is_absolute():
        return candidate
    return PROJECT_ROOT / candidate


def _ensure_sqlite(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5)
    try:
        connection.executescript(_SCHEMA)
        connection.commit()
    finally:
        connection.close()


def _sqlite_upsert(path: Path, rows: list[dict]) -> None:
    _ensure_sqlite(path)
    connection = sqlite3.connect(path, timeout=5)
    try:
        connection.executemany(_UPSERT, rows)
        connection.commit()
    finally:
        connection.close()


def _sqlite_load(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    connection = sqlite3.connect(path, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        found = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='hall_snapshots'"
        ).fetchone()
        if found is None:
            return []
        loaded = connection.execute("SELECT * FROM hall_snapshots ORDER BY hall_name").fetchall()
    finally:
        connection.close()
    return [dict(row) for row in loaded]


def _engine(url: str):
    from sqlalchemy import create_engine

    target = url
    if url.startswith("postgres://"):
        target = "postgresql+psycopg://" + url[len("postgres://") :]
    elif url.startswith("postgresql://"):
        target = "postgresql+psycopg://" + url[len("postgresql://") :]
    return create_engine(target, pool_pre_ping=True, connect_args={"connect_timeout": 3})


def _ensure_postgres(url: str) -> None:
    from sqlalchemy import text

    engine = _engine(url)
    with engine.begin() as connection:
        for statement in _SCHEMA.split(";"):
            sql = statement.strip()
            if sql:
                connection.execute(text(sql))


def _postgres_upsert(url: str, rows: list[dict]) -> None:
    from sqlalchemy import text

    engine = _engine(url)
    statement = text(_UPSERT)
    with engine.begin() as connection:
        for row in rows:
            connection.execute(statement, row)


def _postgres_load(url: str) -> list[dict]:
    from sqlalchemy import text

    engine = _engine(url)
    with engine.connect() as connection:
        loaded = connection.execute(text("SELECT * FROM hall_snapshots ORDER BY hall_name")).mappings().all()
    return [dict(row) for row in loaded]
