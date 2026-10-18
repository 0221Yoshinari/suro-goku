"""会員台帳のデータベース保存。

DATABASE_URL が PostgreSQL なら SQLAlchemy で接続する。
接続できない、またはドライバが無いときは sqlite3 に落とす。
DATABASE_URL が空のときは何もせず、呼び出し元が JSON ファイルを使う。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_COLUMNS = (
    "id",
    "email",
    "name",
    "password_hash",
    "license_key",
    "stripe_customer_id",
    "stripe_subscription_id",
    "checkout_session_id",
    "status",
    "is_active",
    "status_source",
    "status_event_at",
    "trial_start",
    "trial_end",
    "current_period_end",
    "plan_name",
    "subscription_started_at",
    "active_session_id",
    "session_seen",
    "session_ip",
    "session_fp",
    "created_at",
    "updated_at",
)
_NULLABLE = {"trial_start", "trial_end", "current_period_end", "subscription_started_at"}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS members (
  id TEXT PRIMARY KEY,
  email TEXT NOT NULL DEFAULT '',
  name TEXT NOT NULL DEFAULT '',
  password_hash TEXT NOT NULL DEFAULT '',
  license_key TEXT NOT NULL DEFAULT '',
  stripe_customer_id TEXT NOT NULL DEFAULT '',
  stripe_subscription_id TEXT NOT NULL DEFAULT '',
  checkout_session_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'incomplete',
  is_active INTEGER NOT NULL DEFAULT 0,
  status_source TEXT NOT NULL DEFAULT '',
  status_event_at BIGINT NOT NULL DEFAULT 0,
  trial_start BIGINT,
  trial_end BIGINT,
  current_period_end BIGINT,
  plan_name TEXT NOT NULL DEFAULT '',
  subscription_started_at BIGINT,
  active_session_id TEXT NOT NULL DEFAULT '',
  session_seen BIGINT NOT NULL DEFAULT 0,
  session_ip TEXT NOT NULL DEFAULT '',
  session_fp TEXT NOT NULL DEFAULT '',
  created_at BIGINT NOT NULL DEFAULT 0,
  updated_at BIGINT NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS stripe_events (
  event_id TEXT PRIMARY KEY
);
"""
_INSERT = (
    "INSERT INTO members ("
    + ", ".join(_COLUMNS)
    + ") VALUES ("
    + ", ".join(f":{name}" for name in _COLUMNS)
    + ")"
)
_ENGINES: dict[str, object] = {}
_FAILED_POSTGRES: set[str] = set()
_READY_POSTGRES: set[str] = set()
_READY_SQLITE: set[str] = set()


class MemberDbError(Exception):
    """台帳データベースの読み書きに失敗した。"""


def database_url() -> str:
    return (os.environ.get("DATABASE_URL") or "").strip()


def init_member_store() -> str:
    """起動時にテーブルを作る。失敗してもプロセスは止めない。"""
    kind = describe_backend()
    print(f"会員台帳: {kind}", flush=True)
    return kind


def describe_backend() -> str:
    kind, _target = _resolve()
    if kind == "postgres":
        return "postgres"
    if kind == "sqlite":
        url = database_url()
        if url.startswith("postgres"):
            return "sqlite（PostgreSQLに接続できないため）"
        return "sqlite"
    return "json"


def uses_database() -> bool:
    kind, _target = _resolve()
    return kind in {"postgres", "sqlite"}


def load_members() -> dict:
    kind, target = _resolve()
    if kind == "postgres":
        return _postgres_load(str(target))
    if kind == "sqlite":
        return _sqlite_load(Path(target))
    raise MemberDbError("データベース台帳ではありません")


def save_members(data: dict) -> None:
    kind, target = _resolve()
    if kind == "postgres":
        _postgres_save(str(target), data)
        return
    if kind == "sqlite":
        _sqlite_save(Path(target), data)
        return
    raise MemberDbError("データベース台帳ではありません")


def _resolve() -> tuple[str, str | Path]:
    url = database_url()
    if not url:
        return "json", ""
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
            print(f"PostgreSQLを使えないため sqlite に切り替えます: {type(exc).__name__}", flush=True)
            path = _fallback_sqlite_path()
            _ensure_sqlite(path)
            return "sqlite", path
        return "postgres", url
    print("DATABASE_URL の形式を確認できないため、ファイル台帳を使います", flush=True)
    return "json", ""


def _fallback_sqlite_path() -> Path:
    raw = (os.environ.get("MEMBER_SQLITE_PATH") or "").strip()
    if raw:
        return Path(raw)
    return PROJECT_ROOT / "data" / "members.sqlite"


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
    key = str(path.resolve()) if path.parent.exists() else str(path)
    if key in _READY_SQLITE and path.is_file():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5)
    try:
        connection.executescript(_SCHEMA)
        _sqlite_migrate(connection)
        connection.commit()
    finally:
        connection.close()
    _READY_SQLITE.add(str(path.resolve()))


def _sqlite_load(path: Path) -> dict:
    _ensure_sqlite(path)
    connection = sqlite3.connect(path, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        members = connection.execute("SELECT * FROM members").fetchall()
        events = connection.execute("SELECT event_id FROM stripe_events ORDER BY event_id").fetchall()
    except sqlite3.Error as exc:
        raise MemberDbError("会員台帳を読み取れません") from exc
    finally:
        connection.close()
    return {
        "members": [_member_from_row(row) for row in members],
        "events": [str(row["event_id"]) for row in events if row["event_id"]],
    }


def _sqlite_save(path: Path, data: dict) -> None:
    _ensure_sqlite(path)
    connection = sqlite3.connect(path, timeout=5)
    try:
        connection.execute("DELETE FROM members")
        connection.execute("DELETE FROM stripe_events")
        for member in _member_rows(data):
            connection.execute(_INSERT, member)
        for event_id in _event_ids(data):
            connection.execute("INSERT INTO stripe_events (event_id) VALUES (:event_id)", {"event_id": event_id})
        connection.commit()
    except sqlite3.Error as exc:
        connection.rollback()
        raise MemberDbError("会員台帳を保存できません") from exc
    finally:
        connection.close()


def _engine(url: str):
    cached = _ENGINES.get(url)
    if cached is not None:
        return cached
    from sqlalchemy import create_engine

    engine = create_engine(_normalize_postgres_url(url), pool_pre_ping=True, connect_args={"connect_timeout": 3})
    _ENGINES[url] = engine
    return engine


def _ensure_postgres(url: str) -> None:
    if url in _READY_POSTGRES:
        _engine(url)
        return
    from sqlalchemy import text

    engine = _engine(url)
    with engine.begin() as connection:
        for statement in _schema_statements():
            connection.execute(text(statement))
        _postgres_migrate(connection)
    _READY_POSTGRES.add(url)


def _postgres_load(url: str) -> dict:
    from sqlalchemy import text

    engine = _engine(url)
    try:
        with engine.connect() as connection:
            members = connection.execute(text("SELECT * FROM members")).mappings().all()
            events = connection.execute(text("SELECT event_id FROM stripe_events ORDER BY event_id")).mappings().all()
    except Exception as exc:
        raise MemberDbError("会員台帳を読み取れません") from exc
    return {
        "members": [_member_from_row(row) for row in members],
        "events": [str(row["event_id"]) for row in events if row["event_id"]],
    }


def _postgres_save(url: str, data: dict) -> None:
    from sqlalchemy import text

    engine = _engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM members"))
            connection.execute(text("DELETE FROM stripe_events"))
            insert = text(_INSERT)
            for member in _member_rows(data):
                connection.execute(insert, member)
            event_insert = text("INSERT INTO stripe_events (event_id) VALUES (:event_id)")
            for event_id in _event_ids(data):
                connection.execute(event_insert, {"event_id": event_id})
    except Exception as exc:
        raise MemberDbError("会員台帳を保存できません") from exc


def _normalize_postgres_url(url: str) -> str:
    if url.startswith("postgresql+psycopg://") or url.startswith("postgresql+psycopg2://"):
        return url
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def _schema_statements() -> list[str]:
    return [statement.strip() for statement in _SCHEMA.split(";") if statement.strip()]


def _sqlite_migrate(connection: sqlite3.Connection) -> None:
    names = {str(row[1]) for row in connection.execute("PRAGMA table_info(members)")}
    if "password_hash" not in names:
        connection.execute("ALTER TABLE members ADD COLUMN password_hash TEXT NOT NULL DEFAULT ''")
    if "is_active" not in names:
        connection.execute("ALTER TABLE members ADD COLUMN is_active INTEGER NOT NULL DEFAULT 0")
        connection.execute("UPDATE members SET is_active = 1 WHERE status IN ('active', 'trialing')")
    _sqlite_add_contract_columns(connection, names)


def _sqlite_add_contract_columns(connection: sqlite3.Connection, names: set[str]) -> None:
    if "name" not in names:
        connection.execute("ALTER TABLE members ADD COLUMN name TEXT NOT NULL DEFAULT ''")
    if "trial_start" not in names:
        connection.execute("ALTER TABLE members ADD COLUMN trial_start BIGINT")
    if "plan_name" not in names:
        connection.execute("ALTER TABLE members ADD COLUMN plan_name TEXT NOT NULL DEFAULT ''")
    if "subscription_started_at" not in names:
        connection.execute("ALTER TABLE members ADD COLUMN subscription_started_at BIGINT")


def _postgres_migrate(connection) -> None:
    from sqlalchemy import text

    found = connection.execute(
        text("SELECT column_name FROM information_schema.columns WHERE table_name = 'members'")
    ).fetchall()
    names = {str(row[0]) for row in found}
    if "password_hash" not in names:
        connection.execute(text("ALTER TABLE members ADD COLUMN password_hash TEXT NOT NULL DEFAULT ''"))
    if "is_active" not in names:
        connection.execute(text("ALTER TABLE members ADD COLUMN is_active INTEGER NOT NULL DEFAULT 0"))
        connection.execute(text("UPDATE members SET is_active = 1 WHERE status IN ('active', 'trialing')"))
    if "name" not in names:
        connection.execute(text("ALTER TABLE members ADD COLUMN name TEXT NOT NULL DEFAULT ''"))
    if "trial_start" not in names:
        connection.execute(text("ALTER TABLE members ADD COLUMN trial_start BIGINT"))
    if "plan_name" not in names:
        connection.execute(text("ALTER TABLE members ADD COLUMN plan_name TEXT NOT NULL DEFAULT ''"))
    if "subscription_started_at" not in names:
        connection.execute(text("ALTER TABLE members ADD COLUMN subscription_started_at BIGINT"))


def _member_rows(data: dict) -> list[dict]:
    rows: list[dict] = []
    for item in data.get("members") or []:
        if not isinstance(item, dict):
            continue
        row = {}
        for name in _COLUMNS:
            value = item.get(name)
            if name in _NULLABLE:
                row[name] = _optional_int(value)
            elif name == "is_active":
                row[name] = _bit(value)
            elif name in {"status_event_at", "session_seen", "created_at", "updated_at"}:
                row[name] = _required_int(value)
            else:
                row[name] = "" if value is None else str(value)
        if row["id"]:
            rows.append(row)
    return rows


def _event_ids(data: dict) -> list[str]:
    found: list[str] = []
    for item in data.get("events") or []:
        event_id = str(item or "").strip()
        if event_id and event_id not in found:
            found.append(event_id)
    return found[-500:]


def _member_from_row(row) -> dict:
    member = {name: row[name] for name in _COLUMNS}
    for name in _NULLABLE:
        member[name] = _optional_int(member[name])
    for name in ("status_event_at", "session_seen", "created_at", "updated_at"):
        member[name] = _required_int(member[name])
    member["is_active"] = _bit(member.get("is_active")) == 1
    for name in _COLUMNS:
        if name not in _NULLABLE and name not in {"status_event_at", "session_seen", "created_at", "updated_at", "is_active"}:
            member[name] = "" if member[name] is None else str(member[name])
    return member


def _bit(value: object) -> int:
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, int):
        return 1 if value else 0
    return 1 if str(value or "").strip().lower() in {"1", "true", "yes", "on"} else 0


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _required_int(value: object) -> int:
    parsed = _optional_int(value)
    return 0 if parsed is None else parsed
