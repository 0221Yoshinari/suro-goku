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
    # 開いた瞬間の一覧は、台明細を含まない小さな集計だけを読む。
    """
    ALTER TABLE hall_judgments
      ADD COLUMN IF NOT EXISTS summary TEXT NOT NULL DEFAULT ''
    """,
    """
    ALTER TABLE hall_judgments
      ADD COLUMN IF NOT EXISTS board TEXT NOT NULL DEFAULT ''
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


def _dump(body: dict[str, Any]) -> str:
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"))


def _stored_parts(payload: dict[str, Any]) -> tuple[str, str]:
    """台明細なしの一覧と、1店舗分の機種カード。失敗しても台帳本体は残す。"""
    try:
        from src.dashboard.judgment_sync import build_hall_parts

        summary, board = build_hall_parts(payload)
    except Exception as exc:
        print(f"サマリーの作成を飛ばします: {type(exc).__name__}", flush=True)
        return "", ""
    return _dump(summary), _dump(board)


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
        summary, board = _stored_parts(body)
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO hall_judgments
                      (hall_name, prefecture, payload, summary, board, updated_at)
                    VALUES
                      (:hall_name, :prefecture, :payload, :summary, :board, :updated_at)
                    ON CONFLICT (hall_name) DO UPDATE SET
                      prefecture = EXCLUDED.prefecture,
                      payload = EXCLUDED.payload,
                      summary = EXCLUDED.summary,
                      board = EXCLUDED.board,
                      updated_at = EXCLUDED.updated_at
                    """
                ),
                {
                    "hall_name": name,
                    "prefecture": (prefecture or "").strip().lower(),
                    "payload": _dump(body),
                    "summary": summary,
                    "board": board,
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
        loaded = _object(row.get("payload"))
        if loaded is not None:
            payloads.append(loaded)
    _remember_prefecture(wanted, payloads)
    return payloads


def _object(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


def summary_coverage(prefecture: str) -> dict[str, int] | None:
    """サマリーが何店舗分あるか。台のJSONは読まない。"""
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
                    SELECT COUNT(*) AS halls,
                           COUNT(*) FILTER (WHERE summary <> '') AS ready,
                           COALESCE(MAX(updated_at), 0) AS updated_at
                    FROM hall_judgments
                    WHERE prefecture = :prefecture
                    """
                ),
                {"prefecture": wanted},
            ).mappings().one()
    except Exception as exc:
        print(f"サマリー件数の確認に失敗しました: {type(exc).__name__}", flush=True)
        return None
    return {
        "halls": int(row["halls"] or 0),
        "ready": int(row["ready"] or 0),
        "updated_at": int(row["updated_at"] or 0),
    }


def load_opening_summaries(prefecture: str) -> list[dict[str, Any]]:
    """開いた瞬間に出す、全6・全56・全456の集計。"""
    wanted = (prefecture or "").strip().lower()
    if not wanted or not _postgres_url():
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
                    SELECT summary FROM hall_judgments
                    WHERE prefecture = :prefecture AND summary <> ''
                    ORDER BY hall_name
                    """
                ),
                {"prefecture": wanted},
            ).mappings().all()
    except Exception as exc:
        print(f"サマリーの読み込みに失敗しました: {type(exc).__name__}", flush=True)
        return []
    return [item for item in (_object(row.get("summary")) for row in rows) if item is not None]


def load_hall_board(hall_name: str) -> dict[str, Any] | None:
    """1店舗の機種カード。台明細は含まない。"""
    return _load_column(hall_name, "board")


def load_hall_payload(hall_name: str) -> dict[str, Any] | None:
    """1店舗の台明細。開いた機種のときだけ読む。"""
    return _load_column(hall_name, "payload")


def load_prefecture_boards(prefecture: str) -> list[dict[str, Any]]:
    """機種横断を開いたとき、県内の機種カードをまとめて返す。"""
    wanted = (prefecture or "").strip().lower()
    if not wanted or not _postgres_url():
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
                    SELECT board FROM hall_judgments
                    WHERE prefecture = :prefecture AND board <> ''
                    ORDER BY hall_name
                    """
                ),
                {"prefecture": wanted},
            ).mappings().all()
    except Exception as exc:
        print(f"機種カードの読み込みに失敗しました: {type(exc).__name__}", flush=True)
        return []
    return [item for item in (_object(row.get("board")) for row in rows) if item is not None]


def _load_column(hall_name: str, column: str) -> dict[str, Any] | None:
    name = (hall_name or "").strip()
    if not name or column not in {"board", "payload"} or not _postgres_url():
        return None
    try:
        if not _ensure():
            return None
        from sqlalchemy import text

        engine = _engine()
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    f"""
                    SELECT {column} AS body FROM hall_judgments
                    WHERE hall_name = :hall_name
                    """
                ),
                {"hall_name": name},
            ).mappings().first()
    except Exception as exc:
        print(f"店舗判定の読み込みに失敗しました: {type(exc).__name__}", flush=True)
        return None
    if row is None:
        return None
    return _object(row.get("body"))


def write_hall_parts(hall_name: str, summary: dict[str, Any], board: dict[str, Any]) -> bool:
    """既にある台帳へ、一覧用の集計と機種カードだけを書き戻す。"""
    name = (hall_name or "").strip()
    if not name or not _postgres_url():
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
                    UPDATE hall_judgments
                    SET summary = :summary, board = :board
                    WHERE hall_name = :hall_name
                    """
                ),
                {"hall_name": name, "summary": _dump(summary), "board": _dump(board)},
            )
    except Exception as exc:
        print(f"サマリーの書き戻しに失敗しました: {type(exc).__name__}", flush=True)
        return False
    return True


_BACKFILL_LOCK = threading.Lock()
_BACKFILL_RUNNING = False
_BACKFILL_ORDER = (
    "fukuoka",
    "tokyo",
    "osaka",
    "aichi",
    "kanagawa",
    "saitama",
    "chiba",
    "hyogo",
)


def schedule_summary_backfill() -> None:
    """保存済みの台帳から、一覧用の集計を後から作る。画面の応答は待たせない。"""
    global _BACKFILL_RUNNING
    if os.getenv("PYTEST_CURRENT_TEST") or not _postgres_url():
        return
    with _BACKFILL_LOCK:
        if _BACKFILL_RUNNING:
            return
        _BACKFILL_RUNNING = True
    threading.Thread(target=_run_summary_backfill, name="summary-backfill", daemon=True).start()


def _run_summary_backfill() -> None:
    global _BACKFILL_RUNNING
    done = 0
    try:
        if not _ensure():
            return
        from sqlalchemy import text

        engine = _engine()
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT hall_name, prefecture FROM hall_judgments
                    WHERE summary = '' OR board = ''
                    """
                )
            ).mappings().all()
        pending = sorted(
            rows,
            key=lambda row: (
                _BACKFILL_ORDER.index(str(row.get("prefecture") or ""))
                if str(row.get("prefecture") or "") in _BACKFILL_ORDER
                else len(_BACKFILL_ORDER),
                str(row.get("hall_name") or ""),
            ),
        )
        for row in pending:
            if _backfill_one(str(row.get("hall_name") or "")):
                done += 1
        if done:
            print(f"サマリーを{done}店舗分、保存しました。", flush=True)
    except Exception as exc:
        print(f"サマリーの作成を中断しました: {type(exc).__name__}", flush=True)
    finally:
        with _BACKFILL_LOCK:
            _BACKFILL_RUNNING = False


def _backfill_one(hall_name: str) -> bool:
    payload = load_hall_payload(hall_name)
    if not payload:
        return False
    try:
        from src.dashboard.judgment_sync import build_hall_parts

        summary, board = build_hall_parts(payload)
    except Exception as exc:
        print(f"サマリーを作れませんでした: {type(exc).__name__}", flush=True)
        summary = {"hall_name": hall_name, "machine_count": 0, "featured": [], "broken": True}
        board = {"hall_name": hall_name, "machines": []}
    return write_hall_parts(hall_name, summary, board)
