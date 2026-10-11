"""管理画面向けのホール一覧と有効/無効の切り替え。"""

from __future__ import annotations

import json
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.scraper.halls import (
    GROUP_LABELS,
    HALLS_CONFIG_PATH,
    PREFECTURE_LABELS,
    PROJECT_ROOT,
    group_label,
    load_halls_config,
)
from src.scraper.hall_catalog import save_halls_payload

SOURCE_LABELS = {
    "teramoba": "テラモバ",
    "goraggio": "台データオンライン",
    "pscube": "P'sCUBE",
    "maruhan_app": "マルハンアプリ",
    "site7": "サイトセブン",
}
STATUS_LABELS = {
    "ok": "成功",
    "error": "エラー",
    "pending": "未取得",
    "archived": "アーカイブ",
}
USER_DATA_LABELS = {
    "updated": "データ更新済み",
    "waiting": "次回更新待ち",
    "queued": "順次データ反映中",
    "preparing": "更新準備中",
}
_JST = ZoneInfo("Asia/Tokyo")
_STATUS_CACHE: tuple[float, float, dict[str, dict[str, str]]] | None = None


class HallAdminError(RuntimeError):
    """管理画面からホールを更新できなかった。"""


def user_data_state(status: str, fetched_at: str | None, *, today: date | None = None) -> dict[str, str]:
    """利用者向けの取得状況。内部のエラー表記は出さない。"""
    day = today or datetime.now(_JST).date()
    if status == "ok" and _fetched_on(fetched_at, day):
        state = "updated"
    elif status == "ok":
        state = "waiting"
    elif status == "pending":
        state = "queued"
    else:
        state = "preparing"
    return {"state": state, "label": USER_DATA_LABELS[state]}


def hall_status_index() -> dict[str, dict[str, str]]:
    """有効店舗の名前 → 利用者向けの取得状況。短時間キャッシュする。"""
    global _STATUS_CACHE
    now = time.monotonic()
    try:
        mtime = HALLS_CONFIG_PATH.stat().st_mtime
    except OSError:
        mtime = 0.0
    if _STATUS_CACHE and _STATUS_CACHE[0] == mtime and now - _STATUS_CACHE[1] < 20:
        return _STATUS_CACHE[2]
    from src.scraper.halls import load_halls

    today = datetime.now(_JST).date()
    index: dict[str, dict[str, str]] = {}
    for hall in load_halls():
        status, fetched_at = _fetch_status(hall.id, hall.source)
        index[hall.name] = user_data_state(status, fetched_at, today=today)
    _STATUS_CACHE = (mtime, now, index)
    return index


def _fetched_on(iso: str | None, day: date) -> bool:
    if not iso:
        return False
    try:
        parsed = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(_JST).date() == day


def list_hall_rows() -> dict[str, Any]:
    load_halls_config.cache_clear()
    payload = load_halls_config()
    rows: list[dict[str, Any]] = []
    for raw in payload.get("halls") or []:
        if isinstance(raw, dict):
            rows.append(_row(raw))
    rows.sort(key=lambda item: (item["prefecture"] or "zzz", item["name"]))
    return {
        "count": len(rows),
        "enabled_count": sum(1 for item in rows if item["enabled"]),
        "ok_count": sum(1 for item in rows if item["status"] == "ok"),
        "error_count": sum(1 for item in rows if item["status"] == "error"),
        "pending_count": sum(1 for item in rows if item["status"] == "pending"),
        "halls": rows,
    }


def set_hall_enabled(hall_id: str, enabled: bool, *, path: Path | None = None) -> dict[str, Any]:
    hall_id = str(hall_id or "").strip()
    target = path or HALLS_CONFIG_PATH
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HallAdminError("ホール設定を読み取れませんでした") from exc
    if not isinstance(payload, dict):
        raise HallAdminError("ホール設定を読み取れませんでした")
    found: dict[str, Any] | None = None
    for raw in payload.get("halls") or []:
        if isinstance(raw, dict) and str(raw.get("id") or "") == hall_id:
            raw["enabled"] = bool(enabled)
            if enabled:
                raw["archived"] = False
                raw.pop("archive_reason", None)
            found = raw
            break
    if found is None:
        raise HallAdminError("登録されていないホールです")
    save_halls_payload(payload, target)
    return _row(found)


def _row(raw: dict[str, Any]) -> dict[str, Any]:
    hall_id = str(raw.get("id") or "")
    source = str(raw.get("source") or raw.get("source_type") or "")
    prefecture = str(raw.get("prefecture") or "")
    group = str(raw.get("group") or "")
    if raw.get("archived") is True:
        status, fetched_at = "archived", None
    else:
        status, fetched_at = _fetch_status(hall_id, source)
    return {
        "id": hall_id,
        "name": str(raw.get("name") or ""),
        "prefecture": prefecture,
        "prefecture_label": PREFECTURE_LABELS.get(prefecture, prefecture or "未設定"),
        "area": str(raw.get("area") or ""),
        "group": group,
        "group_label": group_label(group) if group else GROUP_LABELS["other"],
        "source": source,
        "source_label": SOURCE_LABELS.get(source, source or "不明"),
        "source_url": str(raw.get("source_url") or ""),
        "enabled": bool(raw.get("enabled", True)),
        "archived": raw.get("archived") is True,
        "data_public": raw.get("data_public", True) is not False,
        "status": status,
        "status_label": STATUS_LABELS[status],
        "fetched_at": fetched_at,
    }


def _fetch_status(hall_id: str, source: str) -> tuple[str, str | None]:
    if not hall_id:
        return "pending", None
    incoming = PROJECT_ROOT / "data" / (source or "site7") / "halls" / hall_id / "incoming.json"
    status, fetched_at = _peek_status(incoming)
    if status != "pending":
        return status, fetched_at
    judgment = PROJECT_ROOT / "data" / "judgments" / "halls" / hall_id / "latest.json"
    judged_status, judged_at = _peek_status(judgment)
    if judged_status == "pending":
        return "pending", None
    return judged_status, judged_at or fetched_at


def _peek_status(path: Path) -> tuple[str, str | None]:
    if not path.is_file():
        return "pending", None
    try:
        with path.open(encoding="utf-8") as handle:
            head = handle.read(1600)
    except OSError:
        return "error", None
    fetched = None
    match = re.search(r'"fetched_at"\s*:\s*"([^"]*)"', head)
    if match and match.group(1):
        fetched = match.group(1)
    compact = re.sub(r"\s+", "", head)
    if '"machines":[]' in compact:
        return "error", fetched
    if '"machines":' in compact or fetched:
        return "ok", fetched
    return "error", fetched
