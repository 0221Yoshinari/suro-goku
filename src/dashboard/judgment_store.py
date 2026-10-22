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
    # 一覧はここだけ読む。summary 全体は末尾集計を含むので、比較や全件読込をすると画面が止まる。
    """
    ALTER TABLE hall_judgments
      ADD COLUMN IF NOT EXISTS opening TEXT NOT NULL DEFAULT ''
    """,
    # 県一覧は店舗台帳を走査しない。県につき1行の小さいJSONだけを読む。
    """
    CREATE TABLE IF NOT EXISTS prefecture_openings (
      prefecture TEXT PRIMARY KEY,
      body TEXT NOT NULL,
      updated_at BIGINT NOT NULL DEFAULT 0
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

    _ENGINE = create_engine(
        _normalize_postgres_url(url),
        pool_pre_ping=True,
        pool_timeout=3,
        connect_args={"connect_timeout": 5, "options": "-c statement_timeout=12000"},
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


def _slim_opening(summary: dict[str, Any]) -> dict[str, Any]:
    """全6・全56・全456のチップだけ。末尾集計や台明細は含めない。"""
    featured = []
    for item in summary.get("featured") or []:
        if not isinstance(item, dict):
            continue
        slim = dict(item)
        slim.pop("units", None)
        featured.append(slim)
    return {
        "hall_name": summary.get("hall_name") or "",
        "prefecture": summary.get("prefecture") or "",
        "judged_at": summary.get("judged_at"),
        "fetched_at": summary.get("fetched_at"),
        "machine_count": int(summary.get("machine_count") or 0),
        "unit_count": int(summary.get("unit_count") or 0),
        "all_high_count": int(summary.get("all_high_count") or 0),
        "high_56_count": int(summary.get("high_56_count") or 0),
        "high_base_count": int(summary.get("high_base_count") or 0),
        "featured": featured,
    }


def _opening_text(summary_json: str) -> str:
    loaded = _object(summary_json)
    if not loaded:
        return ""
    return _dump(_slim_opening(loaded))


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
                      (hall_name, prefecture, payload, summary, board, opening, updated_at)
                    VALUES
                      (:hall_name, :prefecture, :payload, :summary, :board, :opening, :updated_at)
                    ON CONFLICT (hall_name) DO UPDATE SET
                      prefecture = EXCLUDED.prefecture,
                      payload = EXCLUDED.payload,
                      summary = EXCLUDED.summary,
                      board = EXCLUDED.board,
                      opening = EXCLUDED.opening,
                      updated_at = EXCLUDED.updated_at
                    """
                ),
                {
                    "hall_name": name,
                    "prefecture": (prefecture or "").strip().lower(),
                    "payload": _dump(body),
                    "summary": summary,
                    "board": board,
                    "opening": _opening_text(summary),
                    "updated_at": int(time.time()),
                },
            )
    except Exception as exc:
        print(f"判定の永続保存に失敗しました: {type(exc).__name__}", flush=True)
        return False
    _cache_replace((prefecture or "").strip().lower(), body)
    return True


def save_prefecture_opening(prefecture: str, rows: list[dict[str, Any]]) -> bool:
    """県一覧を1行で保存する。店舗ごとの大きい列は触らない。"""
    wanted = (prefecture or "").strip().lower()
    if not wanted or not _postgres_url():
        return False
    slim_rows = [_slim_opening(row) for row in rows if isinstance(row, dict) and str(row.get("hall_name") or "").strip()]
    try:
        if not _ensure():
            return False
        from sqlalchemy import text

        engine = _engine()
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('statement_timeout', '8000', false)")).scalar()
            connection.execute(
                text(
                    """
                    INSERT INTO prefecture_openings (prefecture, body, updated_at)
                    VALUES (:prefecture, :body, :updated_at)
                    ON CONFLICT (prefecture) DO UPDATE SET
                      body = EXCLUDED.body,
                      updated_at = EXCLUDED.updated_at
                    """
                ),
                {
                    "prefecture": wanted,
                    "body": _dump({"halls": slim_rows}),
                    "updated_at": int(time.time()),
                },
            )
    except Exception as exc:
        print(f"県一覧の保存に失敗しました: {type(exc).__name__}", flush=True)
        return False
    _OPENING_ROW_CACHE.pop(wanted, None)
    return True


_OPENING_ROW_CACHE: dict[str, tuple[float, tuple[list[dict[str, Any]], int]]] = {}
_OPENING_ROW_LOCK = threading.Lock()
_OPENING_ROW_TTL = 45.0


def load_prefecture_opening(prefecture: str) -> tuple[list[dict[str, Any]], int] | None:
    """県一覧の1行。店舗台帳は読まない。同じ一覧の再読込はメモリから返す。"""
    wanted = (prefecture or "").strip().lower()
    if not wanted or not _postgres_url():
        return None
    cached = _OPENING_ROW_CACHE.get(wanted)
    if cached is not None and time.monotonic() - cached[0] < _OPENING_ROW_TTL:
        return cached[1]
    with _OPENING_ROW_LOCK:
        cached = _OPENING_ROW_CACHE.get(wanted)
        if cached is not None and time.monotonic() - cached[0] < _OPENING_ROW_TTL:
            return cached[1]
        loaded = _read_prefecture_opening(wanted)
        if loaded is not None:
            _OPENING_ROW_CACHE[wanted] = (time.monotonic(), loaded)
        return loaded


def _read_prefecture_opening(wanted: str) -> tuple[list[dict[str, Any]], int] | None:
    try:
        if not _ensure():
            return None
        from sqlalchemy import text

        engine = _engine()
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('statement_timeout', '3000', false)")).scalar()
            row = connection.execute(
                text(
                    """
                    SELECT body, updated_at FROM prefecture_openings
                    WHERE prefecture = :prefecture
                    """
                ),
                {"prefecture": wanted},
            ).mappings().first()
    except Exception as exc:
        print(f"県一覧を読めませんでした: {type(exc).__name__}", flush=True)
        return None
    if row is None:
        return None
    loaded = _object(row.get("body"))
    halls = loaded.get("halls") if isinstance(loaded, dict) else None
    if not isinstance(halls, list):
        return None
    return halls, int(row.get("updated_at") or 0)


def save_openings(rows: list[dict[str, Any]]) -> int:
    """一覧用の小さい列だけを書き換える。台帳の大きい列は読まない。"""
    if not rows or not _postgres_url():
        return 0
    try:
        if not _ensure():
            return 0
        from sqlalchemy import text

        engine = _engine()
        saved = 0
        with engine.begin() as connection:
            for row in rows:
                if not isinstance(row, dict):
                    continue
                name = str(row.get("hall_name") or "").strip()
                if not name:
                    continue
                result = connection.execute(
                    text(
                        """
                        UPDATE hall_judgments
                        SET opening = :opening
                        WHERE hall_name = :hall_name
                        """
                    ),
                    {"hall_name": name, "opening": _dump(_slim_opening(row))},
                )
                saved += int(result.rowcount or 0)
    except Exception as exc:
        print(f"一覧用の集計を保存できませんでした: {type(exc).__name__}", flush=True)
        return 0
    return saved


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
        with engine.begin() as connection:
            connection.exec_driver_sql("SELECT set_config('statement_timeout', '4000', true)")
            row = connection.execute(
                text(
                    """
                    SELECT COUNT(*) AS halls,
                           COUNT(*) FILTER (WHERE opening <> '') AS ready,
                           COALESCE(MAX(updated_at), 0) AS updated_at,
                           COALESCE(SUM(updated_at), 0) AS updated_sum
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
        "updated_sum": int(row["updated_sum"] or 0),
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
        started = time.monotonic()
        with engine.begin() as connection:
            connection.exec_driver_sql("SELECT set_config('statement_timeout', '4000', true)")
            rows = connection.execute(
                text(
                    """
                    SELECT opening AS summary FROM hall_judgments
                    WHERE prefecture = :prefecture AND opening <> ''
                    ORDER BY hall_name
                    """
                ),
                {"prefecture": wanted},
            ).mappings().all()
        print(
            f"一覧サマリー {wanted} {len(rows)}店舗 {time.monotonic() - started:.2f}秒",
            flush=True,
        )
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
                    SET summary = :summary, board = :board, opening = :opening
                    WHERE hall_name = :hall_name
                    """
                ),
                {
                    "hall_name": name,
                    "summary": _dump(summary),
                    "board": _dump(board),
                    "opening": _dump(_slim_opening(summary)),
                },
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


_OPENING_SKIP: dict[str, float] = {}


def schedule_summary_backfill() -> None:
    """本番の一覧リクエストでは大きい列を読み直さない。一覧列は巡回側が小さいJSONで埋める。"""
    return


def _run_summary_backfill() -> None:
    global _BACKFILL_RUNNING
    done = 0
    try:
        if not _ensure():
            return
        for _ in range(800):
            name = _next_opening_gap()
            if not name:
                break
            if _fill_opening(name):
                done += 1
            else:
                _OPENING_SKIP[name] = time.monotonic() + 600
        if done:
            print(f"一覧用の集計を{done}店舗分、保存しました。", flush=True)
    except Exception as exc:
        print(f"一覧用の集計を中断しました: {type(exc).__name__}", flush=True)
    finally:
        with _BACKFILL_LOCK:
            _BACKFILL_RUNNING = False


def _next_opening_gap() -> str:
    """一覧列が空の店舗名だけ返す。台のJSONは読まない。"""
    from sqlalchemy import text

    now = time.monotonic()
    engine = _engine()
    with engine.begin() as connection:
        connection.exec_driver_sql("SELECT set_config('statement_timeout', '4000', true)")
        rows = connection.execute(
            text(
                """
                SELECT hall_name FROM hall_judgments
                WHERE opening = ''
                ORDER BY CASE WHEN prefecture = 'fukuoka' THEN 0 ELSE 1 END, hall_name
                LIMIT 80
                """
            )
        ).mappings().all()
    for row in rows:
        name = str(row.get("hall_name") or "").strip()
        if name and _OPENING_SKIP.get(name, 0) <= now:
            return name
    return ""


def _fill_opening(hall_name: str) -> bool:
    """1店舗の機種カードから、一覧用の小さい列だけを作る。"""
    name = (hall_name or "").strip()
    if not name or not _postgres_url():
        return False
    try:
        if not _ensure():
            return False
        from sqlalchemy import text

        engine = _engine()
        with engine.begin() as connection:
            connection.exec_driver_sql("SELECT set_config('statement_timeout', '4000', true)")
            row = connection.execute(
                text("SELECT board FROM hall_judgments WHERE hall_name = :hall_name"),
                {"hall_name": name},
            ).mappings().first()
        board = _object(row.get("board") if row else "")
        machines = board.get("machines") if isinstance(board, dict) else None
        if isinstance(machines, list) and machines:
            from src.dashboard.judgment_sync import summaries_from_hall_cards

            card = dict(board)
            card["hall_name"] = card.get("hall_name") or name
            rows = summaries_from_hall_cards([card])
            if not rows:
                return False
            with engine.begin() as connection:
                connection.exec_driver_sql("SELECT set_config('statement_timeout', '4000', true)")
                connection.execute(
                    text(
                        """
                        UPDATE hall_judgments
                        SET opening = :opening
                        WHERE hall_name = :hall_name AND opening = ''
                        """
                    ),
                    {"hall_name": name, "opening": _dump(rows[0])},
                )
            return True
        return _backfill_one(name)
    except Exception as exc:
        print(f"一覧用の集計を保存できませんでした: {type(exc).__name__}", flush=True)
        return False


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
