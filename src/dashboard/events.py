"""日付ごとのホール・系列イベント。手動登録と、あとからの自動取得が同じファイルを使う。"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from src.analyzer.judgment import PROJECT_ROOT
from src.normalize import normalize_machine_name
from src.scraper.halls import GROUP_LABELS
from src.scraper.halls import group_label
from src.scraper.halls import hall_groups
from src.scraper.halls import load_halls

EVENTS_PATH = PROJECT_ROOT / "data" / "events" / "todays_events.json"
EVENT_TYPES = ("合同", "取材", "特定日")
JOINT_STATUSES = ("ALL_HIGH", "HIGH_56")
JST = ZoneInfo("Asia/Tokyo")

_BADGE_KIND = {"合同": "joint", "取材": "cover", "特定日": "day"}
_STATUS_SHORT = {"ALL_HIGH": "全6", "HIGH_56": "全56"}


class EventError(ValueError):
    """入力や保存ファイルがイベントとして扱えない。"""


def today_jst() -> str:
    return datetime.now(JST).date().isoformat()


def events_updated_at() -> str | None:
    return read_store().get("updated_at")


def list_events() -> list[dict]:
    """保存済みイベントを新しい日付順で返す。"""
    rows = [public_event(item) for item in read_store().get("events") or []]
    rows.sort(key=lambda item: (item.get("date") or "", item.get("event_title") or ""), reverse=True)
    return rows


def events_for_date(date: str | None = None) -> list[dict]:
    target = date or today_jst()
    return [item for item in list_events() if item.get("date") == target]


def events_on_dates(dates: list[str]) -> list[dict]:
    wanted = {item for item in dates if item}
    return [item for item in list_events() if item.get("date") in wanted]


def target_options() -> dict:
    groups = [
        {"id": group["id"], "label": group["label"], "hall_count": len(group.get("halls") or [])}
        for group in hall_groups(include_disabled=False)
    ]
    halls = [
        {
            "id": hall.id,
            "name": hall.name,
            "group": hall.group,
            "group_label": group_label(hall.group),
        }
        for hall in load_halls()
    ]
    return {"groups": groups, "halls": halls}


def create_event(payload: dict) -> dict:
    event = _normalize_payload(payload)
    event["id"] = _new_id()
    store = read_store()
    events = list(store.get("events") or [])
    events.append(event)
    _write_store(events, dismissed=store.get("dismissed") or [])
    return public_event(event)


def update_event(event_id: str, payload: dict) -> dict:
    event = _normalize_payload(payload)
    event["id"] = event_id
    store = read_store()
    events = list(store.get("events") or [])
    for index, current in enumerate(events):
        if str(current.get("id") or "") == event_id:
            for key in ("source", "source_key", "source_url"):
                if current.get(key):
                    event[key] = current[key]
            events[index] = event
            _write_store(events, dismissed=store.get("dismissed") or [])
            return public_event(event)
    raise EventError("指定したイベントが見つかりません")


def delete_event(event_id: str) -> None:
    store = read_store()
    events = list(store.get("events") or [])
    removed = next((item for item in events if str(item.get("id") or "") == event_id), None)
    kept = [item for item in events if str(item.get("id") or "") != event_id]
    if removed is None:
        raise EventError("指定したイベントが見つかりません")
    dismissed = list(store.get("dismissed") or [])
    source_key = str(removed.get("source_key") or "")
    if source_key and source_key not in dismissed:
        dismissed.append(source_key)
    _write_store(kept, dismissed=dismissed)


def apply_auto_events(incoming: list[dict], refreshed_sources: list[str] | None = None) -> dict[str, int]:
    """自動取得分だけを source_key で追加・更新する。手動登録と削除済みは触らない。

    置換は今回告知を返せた情報源だけに限る。他県のソースが空でも、その既存イベントは残す。
    """
    store = read_store()
    events = list(store.get("events") or [])
    dismissed = set(store.get("dismissed") or [])
    index_by_key: dict[str, int] = {}
    for index, item in enumerate(events):
        key = str(item.get("source_key") or "")
        if key:
            index_by_key[key] = index
    added = 0
    updated = 0
    removed = 0
    accepted = [item for item in (_accept_auto_event(raw) for raw in incoming) if item is not None]
    incoming_keys = {
        str(raw.get("source_key") or "").strip()
        for raw in accepted
        if str(raw.get("source_key") or "").strip() and str(raw.get("source_key") or "").strip() not in dismissed
    }
    refreshed = {str(item).strip() for item in (refreshed_sources or []) if str(item).strip()}
    refreshed.update(_event_source(raw) for raw in accepted)
    refreshed.discard("")
    if refreshed:
        kept = []
        for item in events:
            key = str(item.get("source_key") or "")
            source = _event_source(item)
            if key and source in refreshed and key not in incoming_keys and key not in dismissed:
                removed += 1
                continue
            kept.append(item)
        events = kept
        index_by_key = {}
        for index, item in enumerate(events):
            key = str(item.get("source_key") or "")
            if key:
                index_by_key[key] = index
    for raw in accepted:
        key = str(raw.get("source_key") or "").strip()
        if not key or key in dismissed:
            continue
        event = _normalize_payload(raw)
        event.update(
            {
                "source": str(raw.get("source") or "x"),
                "source_key": key,
                "source_url": str(raw.get("source_url") or ""),
                "prefectures": list(raw.get("prefectures") or []),
            }
        )
        current_index = index_by_key.get(key)
        if current_index is None:
            event["id"] = _new_id()
            events.append(event)
            index_by_key[key] = len(events) - 1
            added += 1
            continue
        current = events[current_index]
        event["id"] = str(current.get("id") or _new_id())
        if all(
            current.get(field) == event.get(field)
            for field in ("date", "hall_id", "group", "event_type", "event_title", "source_url", "prefectures")
        ):
            continue
        events[current_index] = event
        updated += 1
    if added or updated or removed:
        _write_store(events, dismissed=list(dismissed))
    return {"added": added, "updated": updated, "removed": removed}


def group_joint_picks(machines: list[dict] | None, events: list[dict] | None = None) -> list[dict]:
    """合同イベントの系列で、全6または全56が2店舗以上そろった機種。"""
    source = events if events is not None else events_for_date()
    picks: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for event in source:
        if event.get("event_type") != "合同" or not event.get("group"):
            continue
        group_id = str(event["group"])
        hall_names = set(group_hall_names(group_id, event.get("prefectures") or []))
        if not hall_names:
            continue
        grouped: dict[str, dict[str, dict]] = {}
        titles: dict[str, str] = {}
        for item in machines or []:
            hall = str(item.get("hall_name") or "")
            if hall not in hall_names:
                continue
            status = dashboard_status(item)
            if status not in JOINT_STATUSES:
                continue
            name = str(item.get("machine_name") or "").strip() or "(機種不明)"
            key = normalize_machine_name(name) or name
            bucket = grouped.setdefault(key, {})
            previous = bucket.get(hall)
            if previous is None or _status_rank(status) < _status_rank(previous["status"]):
                bucket[hall] = {"hall_name": hall, "status": status, "status_label": _STATUS_SHORT[status]}
            current_title = titles.get(key) or name
            if len(name) < len(current_title):
                titles[key] = name
            else:
                titles.setdefault(key, name)
        for key, halls in grouped.items():
            if len(halls) < 2:
                continue
            identity = (group_id, key)
            if identity in seen:
                continue
            seen.add(identity)
            hall_rows = sorted(halls.values(), key=lambda row: (_status_rank(row["status"]), row["hall_name"]))
            all_high = sum(1 for row in hall_rows if row["status"] == "ALL_HIGH")
            high_56 = sum(1 for row in hall_rows if row["status"] == "HIGH_56")
            picks.append(
                {
                    "event_id": event.get("id") or "",
                    "event_title": event.get("event_title") or "",
                    "event_type": "合同",
                    "badge": event.get("badge") or event_badge("合同", event.get("event_title") or ""),
                    "group": group_id,
                    "group_label": group_label(group_id),
                    "machine_key": key,
                    "machine_name": titles.get(key) or "(機種不明)",
                    "halls": hall_rows,
                    "hall_count": len(hall_rows),
                    "all_high_count": all_high,
                    "high_56_count": high_56,
                }
            )
    picks.sort(
        key=lambda item: (
            -int(item["hall_count"]),
            -int(item["all_high_count"]),
            str(item["machine_name"]),
        )
    )
    return picks


_EXTERNAL_HALL_PREFIXES = ("daidata-", "teramoba-", "goraggio-", "site7-", "pscube-", "maruhan_app-")
_MEMBER_EVENT_HIDDEN = {"source", "source_key", "source_url"}


def public_hall_id(hall_id: str) -> str:
    """一般向けの店舗ID。外部サイト由来の接頭辞と店舗番号は出さない。"""
    text = str(hall_id or "")
    lowered = text.lower()
    for prefix in _EXTERNAL_HALL_PREFIXES:
        if lowered.startswith(prefix):
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
            return f"hall-{digest}"
    return text


def member_event(event: dict) -> dict:
    """会員画面へ返すイベント。取得元と外部URLは含めない。"""
    raw_id = str(event.get("hall_id") or "")
    cleaned_id = public_hall_id(raw_id)
    cleaned = {key: value for key, value in event.items() if key not in _MEMBER_EVENT_HIDDEN}
    cleaned["hall_id"] = cleaned_id
    label = str(cleaned.get("target_label") or "")
    if raw_id and raw_id != cleaned_id and raw_id in label:
        cleaned["target_label"] = label.replace(raw_id, cleaned_id)
    return cleaned


def present_member_payload(payload: dict) -> dict:
    """会員向けJSONから、取得元とローカルパスを除く。"""
    for key in ("source", "masters_path", "source_url", "source_key", "failure_details", "results"):
        payload.pop(key, None)
    for key in ("events", "upcoming_events"):
        rows = payload.get(key)
        if isinstance(rows, list):
            payload[key] = [member_event(row) if isinstance(row, dict) else row for row in rows]
    return payload


def public_event(raw: dict) -> dict:
    group_id = str(raw.get("group") or "")
    hall_id = str(raw.get("hall_id") or "")
    event_type = str(raw.get("event_type") or "")
    title = str(raw.get("event_title") or "")
    prefectures = [str(item) for item in (raw.get("prefectures") or []) if str(item).strip()]
    return {
        "id": str(raw.get("id") or ""),
        "date": str(raw.get("date") or ""),
        "hall_id": hall_id,
        "group": group_id,
        "event_type": event_type,
        "event_title": title,
        "badge": event_badge(event_type, title),
        "badge_kind": _BADGE_KIND.get(event_type, "day"),
        "target_label": _target_label(group_id, hall_id),
        "hall_names": _event_hall_names(group_id, hall_id, prefectures),
        "direct_hall_names": _direct_hall_names(hall_id),
        "prefectures": prefectures,
        "source": str(raw.get("source") or ""),
        "source_url": str(raw.get("source_url") or ""),
    }


def event_badge(event_type: str, title: str) -> str:
    label = (title or "").strip()
    if event_type == "合同":
        return f"🔥【合同】{label}"
    if event_type == "取材":
        return f"📸 {label}"
    if event_type == "特定日":
        return f"📌【特定日】{label}"
    return label


def dashboard_status(item: dict) -> str:
    """サマリーカードと同じ見え方の判定ランク。"""
    if item.get("status") == "EXCLUDED" or item.get("judgment_excluded"):
        return str(item.get("status") or "EXCLUDED")
    checks = item.get("checks") if isinstance(item.get("checks"), dict) else {}
    if checks.get("early_enough") is False:
        return "INCOMPLETE" if item.get("status") == "INCOMPLETE" else "INSUFFICIENT_GAMES"
    if checks.get("meets_setting_6"):
        return "ALL_HIGH"
    if checks.get("meets_setting_5"):
        return "HIGH_56"
    if item.get("status") == "EARLY_ALERT":
        return "HIGH_BASE"
    return str(item.get("status") or "")


def read_store() -> dict:
    path = EVENTS_PATH
    if not path.is_file():
        return {"updated_at": None, "events": [], "dismissed": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EventError("イベントファイルを読み取れません") from exc
    if not isinstance(payload, dict):
        raise EventError("イベントファイルの形式が正しくありません")
    events = payload.get("events")
    if events is None:
        events = []
    if not isinstance(events, list):
        raise EventError("イベントファイルの形式が正しくありません")
    cleaned = [item for item in events if isinstance(item, dict)]
    changed = False
    for item in cleaned:
        if not str(item.get("id") or "").strip():
            item["id"] = _new_id()
            changed = True
    dismissed = payload.get("dismissed") if isinstance(payload.get("dismissed"), list) else []
    if changed:
        _write_store(cleaned, updated_at=payload.get("updated_at"), dismissed=dismissed)
    return {"updated_at": payload.get("updated_at"), "events": cleaned, "dismissed": dismissed}


def _normalize_payload(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise EventError("保存形式が正しくありません")
    date = str(payload.get("date") or "").strip()
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError as exc:
        raise EventError("日付は YYYY-MM-DD で指定してください") from exc
    event_type = str(payload.get("event_type") or "").strip()
    if event_type not in EVENT_TYPES:
        raise EventError("イベント種別は 合同 / 取材 / 特定日 から選んでください")
    title = str(payload.get("event_title") or "").strip()
    if not title:
        raise EventError("イベント名を入力してください")
    if len(title) > 80:
        raise EventError("イベント名は80文字以内にしてください")
    group_id, hall_id = _resolve_target(payload)
    return {
        "date": date,
        "hall_id": hall_id,
        "group": group_id,
        "event_type": event_type,
        "event_title": title,
    }


def _resolve_target(payload: dict) -> tuple[str, str]:
    raw_group = str(payload.get("group") or "").strip()
    raw_hall = str(payload.get("hall_id") or "").strip()
    target = str(payload.get("target") or "").strip()
    if target.startswith("group:"):
        raw_group = target.split(":", 1)[1].strip()
        raw_hall = ""
    elif target.startswith("hall:"):
        raw_hall = target.split(":", 1)[1].strip()
        raw_group = ""
    elif target and not raw_group and not raw_hall:
        if canonical_group(target) and not canonical_hall_id(target):
            raw_group = target
        else:
            raw_hall = target
    group_id = ""
    hall_id = ""
    if raw_group:
        group_id = canonical_group(raw_group) or ""
        if not group_id:
            raise EventError("対象の系列が見つかりません")
    if raw_hall:
        hall_id = canonical_hall_id(raw_hall) or ""
        if not hall_id:
            raise EventError("対象のホールが見つかりません")
    if not group_id and not hall_id:
        raise EventError("対象のホールまたは系列を選んでください")
    return group_id, hall_id


def canonical_group(value: str) -> str | None:
    raw = (value or "").strip()
    if not raw:
        return None
    folded = raw.lower().replace("-", "_")
    if folded.endswith("_group"):
        folded = folded[: -len("_group")]
    for key, label in GROUP_LABELS.items():
        if folded == key or raw == label:
            return key
    return None


def canonical_hall_id(value: str) -> str | None:
    raw = (value or "").strip()
    if not raw:
        return None
    needle = raw.lower().replace("_", "-")
    underscored = raw.lower().replace("-", "_")
    for hall in load_halls(include_disabled=True):
        hall_ids = {hall.id.lower(), hall.id.lower().replace("-", "_"), hall.id.lower().replace("_", "-")}
        if needle in hall_ids or underscored in hall_ids or raw == hall.id:
            return hall.id
        if raw == hall.name or raw in hall.aliases:
            return hall.id
    return None


def _direct_hall_names(hall_id: str) -> list[str]:
    if not hall_id:
        return []
    hall = _hall_by_id(hall_id)
    return [hall.name] if hall else []


def group_hall_names(group_id: str, prefectures: list | None = None) -> list[str]:
    """系列の有効ホール名。県リストがあるときはその県だけに限る。"""
    allowed = {str(item).strip() for item in (prefectures or []) if str(item).strip()}
    names: list[str] = []
    for hall in load_halls():
        if (hall.group or "other") != group_id:
            continue
        if allowed and hall.prefecture not in allowed:
            continue
        if hall.name not in names:
            names.append(hall.name)
    return names


def _event_source(item: dict) -> str:
    source = str(item.get("source") or "").strip()
    if source:
        return source
    key = str(item.get("source_key") or "")
    parts = key.split(":")
    if len(parts) >= 2 and parts[0] == "x" and parts[1]:
        return f"x:{parts[1]}"
    return ""


def _accept_auto_event(raw: dict) -> dict | None:
    """無効ホールや未知の系列を落とし、有効ホールのマスターへ寄せる。"""
    if not isinstance(raw, dict):
        return None
    hall_id = str(raw.get("hall_id") or "").strip()
    group = str(raw.get("group") or "").strip()
    if hall_id:
        resolved = canonical_hall_id(hall_id)
        hall = _hall_by_id(resolved or "")
        hall_id = hall.id if hall is not None and hall.enabled else ""
    if group and not canonical_group(group):
        group = ""
    if not hall_id and not group:
        return None
    payload = dict(raw)
    payload["hall_id"] = hall_id
    payload["group"] = group
    prefs = payload.get("prefectures")
    payload["prefectures"] = [str(item).strip() for item in prefs if str(item).strip()] if isinstance(prefs, list) else []
    return payload


def _event_hall_names(group_id: str, hall_id: str, prefectures: list | None = None) -> list[str]:
    names: list[str] = []
    if group_id:
        for name in group_hall_names(group_id, prefectures):
            if name not in names:
                names.append(name)
    if hall_id:
        hall = _hall_by_id(hall_id)
        if hall and hall.name not in names:
            names.append(hall.name)
    return names


def _target_label(group_id: str, hall_id: str) -> str:
    parts: list[str] = []
    if group_id:
        parts.append(f"{group_label(group_id)}（系列）")
    if hall_id:
        hall = _hall_by_id(hall_id)
        parts.append(hall.name if hall else hall_id)
    return " / ".join(parts)


def _hall_by_id(hall_id: str):
    for hall in load_halls(include_disabled=True):
        if hall.id == hall_id:
            return hall
    return None


def _write_store(events: list[dict], updated_at: str | None = None, dismissed: list | None = None) -> None:
    path = EVENTS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    if dismissed is None:
        dismissed = _read_dismissed(path)
    payload = {
        "updated_at": updated_at or datetime.now(JST).isoformat(timespec="seconds"),
        "events": events,
        "dismissed": dismissed,
    }
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _read_dismissed(path) -> list:
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    dismissed = payload.get("dismissed") if isinstance(payload, dict) else None
    return list(dismissed) if isinstance(dismissed, list) else []


def _new_id() -> str:
    return f"evt-{uuid.uuid4().hex[:12]}"


def _status_rank(status: str) -> int:
    if status == "ALL_HIGH":
        return 0
    if status == "HIGH_56":
        return 1
    return 9
