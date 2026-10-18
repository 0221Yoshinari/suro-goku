"""管理画面向けの機種マスター一覧・保存・検品。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from src.analyzer.judgment import (
    DEFAULT_OUTPUT_DIR,
    DERIVE_SETTING4_RB_RATIO,
    DERIVE_SETTING4_SYNTHETIC_RATIO,
    DERIVE_SETTING5_RATIO,
    SETTING6_MARGIN_MIN_GAMES,
    SETTING6_RELATIVE_TOLERANCE,
    MachineMaster,
    load_masters,
)
from src.analyzer.juggler import (
    SCORE_BANDS,
    SCORE_GAMES_EARLY,
    SCORE_GAMES_FORMING,
    SCORE_GAMES_FULL,
    SCORE_GAMES_FULL_RATIO,
    SCORE_WEIGHT_GAMES,
    SCORE_WEIGHT_RB,
    SCORE_WEIGHT_SYNTHETIC,
)
from src.analyzer.overrides import (
    OverrideError,
    append_history,
    allocate_machine_id,
    build_machine_record,
    diff_against_base,
    history_summary,
    load_override_document,
    overrides_path,
    PROBABILITY_FIELDS,
    parse_edit,
    parse_new_machine,
    save_override_document,
)
from src.normalize import norm_name

INSPECTION_STATUSES = ("ALL_HIGH", "HIGH_56", "HIGH_BASE")


def list_master_rows() -> dict[str, Any]:
    base_rows = {item.id: _row(item, overridden=False) for item in load_masters(apply_override=False)}
    rows: list[dict[str, Any]] = []
    for item in load_masters():
        current = _row(item, overridden=bool((item.raw.get("override") or {}).get("active")))
        current["base"] = _baseline(base_rows[item.id]) if item.id in base_rows else _baseline(current)
        if item.id not in base_rows:
            current["added"] = True
        rows.append(current)
    rows.sort(key=lambda item: (not item["added"], not item["override_active"], item["series"] or "", item["name"]))
    document = load_override_document()
    return {
        "count": len(rows),
        "override_count": sum(1 for item in rows if item["override_active"]),
        "overrides_path": str(overrides_path()),
        "updated_at": document.get("updated_at"),
        "machines": rows,
        "logic": judgment_logic_public(),
    }


def save_master_edit(machine_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    machine_id = str(machine_id or "").strip()
    document = load_override_document()
    added = document.setdefault("added", {})
    if machine_id in added:
        return _save_added_machine(machine_id, payload, document)
    base = _find_master(machine_id, apply_override=False)
    if base is None:
        raise OverrideError("登録されていない機種です")
    parsed = parse_edit(payload)
    changed = diff_against_base(parsed, _comparable(base))
    machines = document.setdefault("machines", {})
    previous = machines.get(machine_id) if isinstance(machines.get(machine_id), dict) else {}
    summary_source = dict(changed)
    if "note" not in parsed and previous.get("note"):
        changed["note"] = previous.get("note")
    if changed:
        now = datetime.now(timezone.utc).isoformat()
        changed["updated_at"] = now
        changed["history"] = append_history(previous.get("history"), history_summary(summary_source), now)
        machines[machine_id] = changed
    else:
        machines.pop(machine_id, None)
    save_override_document(document)
    _clear_type_cache()
    saved = _find_master(machine_id, apply_override=True)
    if saved is None:
        raise OverrideError("保存後の機種を読み直せませんでした")
    active = isinstance(saved.raw.get("override"), dict) and bool(saved.raw["override"].get("active"))
    row = _row(saved, overridden=active)
    row["base"] = _baseline(_row(base, overridden=False))
    row["saved_fields"] = sorted(set(changed) - {"updated_at", "history"})
    return row


def create_master(payload: dict[str, Any]) -> dict[str, Any]:
    name, parsed = parse_new_machine(payload)
    if any(norm_name(item.name) == norm_name(name) for item in load_masters()):
        raise OverrideError("同じ機種名が既に登録されています")
    document = load_override_document()
    added = document.setdefault("added", {})
    taken = {item.id for item in load_masters()}
    taken.update(str(key) for key in added)
    machine_id = allocate_machine_id(name, taken)
    now = datetime.now(timezone.utc).isoformat()
    record = build_machine_record(
        machine_id=machine_id,
        name=name,
        parsed=parsed,
        history=append_history([], "機種を追加", now),
    )
    added[machine_id] = record
    save_override_document(document)
    _clear_type_cache()
    saved = _find_master(machine_id, apply_override=True)
    if saved is None:
        raise OverrideError("追加した機種を読み直せませんでした")
    row = _row(saved, overridden=False)
    row["base"] = _baseline(row)
    row["saved_fields"] = ["added"]
    return row


def clear_master_edit(machine_id: str) -> dict[str, Any]:
    machine_id = str(machine_id or "").strip()
    document = load_override_document()
    added = document.setdefault("added", {})
    if machine_id in added:
        added.pop(machine_id, None)
        save_override_document(document)
        _clear_type_cache()
        return {"id": machine_id, "deleted": True}
    base = _find_master(machine_id, apply_override=False)
    if base is None:
        raise OverrideError("登録されていない機種です")
    machines = document.setdefault("machines", {})
    previous = machines.get(machine_id) if isinstance(machines.get(machine_id), dict) else {}
    note = str(previous.get("note") or "")
    if note or previous.get("history"):
        now = datetime.now(timezone.utc).isoformat()
        machines[machine_id] = {
            "note": note,
            "updated_at": now,
            "history": append_history(previous.get("history"), "参照数値をマスター値に戻した", now),
        }
    else:
        machines.pop(machine_id, None)
    save_override_document(document)
    _clear_type_cache()
    saved = _find_master(machine_id, apply_override=True)
    if saved is None:
        raise OverrideError("保存後の機種を読み直せませんでした")
    row = _row(saved, overridden=False)
    row["base"] = _baseline(_row(base, overridden=False))
    row["saved_fields"] = []
    return row


def inspection_rows() -> dict[str, Any]:
    latest = DEFAULT_OUTPUT_DIR / "latest.json"
    if not latest.is_file():
        return {"judged_at": None, "fetched_at": None, "count": 0, "rows": []}
    try:
        payload = json.loads(latest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"judged_at": None, "fetched_at": None, "count": 0, "rows": []}
    if not isinstance(payload, dict):
        return {"judged_at": None, "fetched_at": None, "count": 0, "rows": []}
    rows: list[dict[str, Any]] = []
    for item in payload.get("machines") or []:
        if not isinstance(item, dict) or item.get("status") not in INSPECTION_STATUSES:
            continue
        criteria = item.get("criteria") if isinstance(item.get("criteria"), dict) else {}
        override = criteria.get("override") if isinstance(criteria.get("override"), dict) else {}
        rows.append(
            {
                "hall_name": item.get("hall_name"),
                "machine_name": item.get("machine_name"),
                "machine_id": criteria.get("id"),
                "status": item.get("status"),
                "status_label": item.get("status_label"),
                "judgment_type": item.get("judgment_type"),
                "hit_target": criteria.get("hit_target"),
                "unit_count": item.get("unit_count"),
                "total_games": item.get("total_games"),
                "avg_games": item.get("avg_games"),
                "bb_count": item.get("bb_count"),
                "rb_count": item.get("rb_count"),
                "first_hit_count": item.get("first_hit_count"),
                "synthetic_probability": item.get("synthetic_probability"),
                "rb_probability": item.get("rb_probability"),
                "setting_6": criteria.get("setting_6"),
                "setting_5": criteria.get("setting_5"),
                "setting_4": criteria.get("setting_4"),
                "override_active": bool(override.get("active")),
                "override_fields": override.get("fields") or [],
            }
        )
    return {
        "judged_at": payload.get("judged_at"),
        "fetched_at": payload.get("fetched_at"),
        "count": len(rows),
        "rows": rows,
    }


def judgment_logic_public() -> dict[str, Any]:
    """画面に出す、判定コードが実際に使っている共通パラメータ。"""
    return {
        "tolerance": SETTING6_RELATIVE_TOLERANCE,
        "margin_min_games": SETTING6_MARGIN_MIN_GAMES,
        "derive_setting5": DERIVE_SETTING5_RATIO,
        "derive_setting4_synthetic": DERIVE_SETTING4_SYNTHETIC_RATIO,
        "derive_setting4_rb": DERIVE_SETTING4_RB_RATIO,
        "score_weight_rb": SCORE_WEIGHT_RB,
        "score_weight_synthetic": SCORE_WEIGHT_SYNTHETIC,
        "score_weight_games": SCORE_WEIGHT_GAMES,
        "score_points": {level: points for level, points in SCORE_BANDS},
        "score_games_full": SCORE_GAMES_FULL,
        "score_games_full_ratio": SCORE_GAMES_FULL_RATIO,
        "score_games_forming": SCORE_GAMES_FORMING,
        "score_games_early": SCORE_GAMES_EARLY,
    }


def _clear_type_cache() -> None:
    from src.scraper.site7 import clear_master_type_cache

    clear_master_type_cache()


def _save_added_machine(machine_id: str, payload: dict[str, Any], document: dict[str, Any]) -> dict[str, Any]:
    current = _find_master(machine_id, apply_override=True)
    if current is None:
        raise OverrideError("登録されていない機種です")
    parsed = parse_edit(payload)
    name = str(payload.get("name") or current.name).strip()
    if not name or len(name) > 80:
        raise OverrideError("機種名は1〜80文字で入力してください")
    if norm_name(name) != norm_name(current.name) and any(
        norm_name(item.name) == norm_name(name) and item.id != machine_id for item in load_masters()
    ):
        raise OverrideError("同じ機種名が既に登録されています")
    comparable = _comparable(current)
    origins = field_origins(current)
    for field in PROBABILITY_FIELDS:
        value = parsed.get(field)
        if origins.get(field) == "derived" and value is not None and _same_number(value, comparable.get(field)):
            parsed[field] = None
    previous = document.get("added", {}).get(machine_id) if isinstance(document.get("added"), dict) else {}
    if not isinstance(previous, dict):
        previous = {}
    now = datetime.now(timezone.utc).isoformat()
    parts: list[str] = []
    if name != current.name:
        parts.append(f"機種名を{name}")
    changed = diff_against_base(parsed, comparable)
    summary = history_summary(changed)
    if summary:
        parts.append(summary)
    history = append_history(previous.get("history"), "、".join(parts), now)
    aliases = previous.get("aliases") if isinstance(previous.get("aliases"), list) else []
    record = build_machine_record(
        machine_id=machine_id,
        name=name,
        parsed=parsed,
        aliases=[str(alias) for alias in aliases],
        history=history,
    )
    document.setdefault("added", {})[machine_id] = record
    save_override_document(document)
    _clear_type_cache()
    saved = _find_master(machine_id, apply_override=True)
    if saved is None:
        raise OverrideError("保存後の機種を読み直せませんでした")
    row = _row(saved, overridden=False)
    row["base"] = _baseline(row)
    row["saved_fields"] = sorted(set(changed) - {"note"})
    if name != current.name:
        row["saved_fields"].append("name")
    return row


def _find_master(machine_id: str, *, apply_override: bool) -> MachineMaster | None:
    for item in load_masters(apply_override=apply_override):
        if item.id == machine_id:
            return item
    return None


def _bar_value(master: MachineMaster, level: int, *, rb: bool) -> float | None:
    bar = master.settings.get(level)
    if bar is None:
        return None
    return bar.rb if rb else bar.synthetic


def _comparable(master: MachineMaster) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": master.type,
        "hit_target": master.hit_target,
        "min_games": master.min_games,
        "early_min_games": master.early_min_games,
        "note": str(master.raw.get("note") or ""),
    }
    for level in range(1, 7):
        payload[f"setting_{level}"] = _bar_value(master, level, rb=False)
        payload[f"rb_{level}"] = _bar_value(master, level, rb=True)
    return payload


def _baseline(row: dict[str, Any]) -> dict[str, Any]:
    fields = ("type", "hit_target", "note", "min_games", "early_min_games")
    fields += tuple(f"setting_{level}" for level in range(1, 7))
    fields += tuple(f"rb_{level}" for level in range(1, 7))
    return {field: row.get(field) for field in fields}


def field_origins(master: MachineMaster) -> dict[str, str]:
    """各数値がマスター記載・上書き・設定6からの算出・未使用のどれか。"""
    raw = master.raw if isinstance(master.raw, dict) else {}
    override = raw.get("override") if isinstance(raw.get("override"), dict) else {}
    overridden = {str(field) for field in (override.get("fields") or [])}
    origins: dict[str, str] = {}
    for level in range(1, 7):
        for field, is_rb in ((f"setting_{level}", False), (f"rb_{level}", True)):
            if field in overridden:
                origins[field] = "override"
            elif (_explicit_rb(raw, level) if is_rb else _explicit_synthetic(raw, level)):
                origins[field] = "stored"
            elif _bar_value(master, level, rb=is_rb) is not None:
                origins[field] = "derived"
            else:
                origins[field] = "empty"
    return origins


def _explicit_synthetic(raw: dict[str, Any], level: int) -> bool:
    if _number(raw.get(f"setting_{level}")) is not None:
        return True
    entry = _setting_entry(raw, level)
    if not isinstance(entry, dict):
        return False
    return any(entry.get(key) is not None for key in ("first_hit", "synthetic", "at_first_hit"))


def _explicit_rb(raw: dict[str, Any], level: int) -> bool:
    entry = _setting_entry(raw, level)
    return isinstance(entry, dict) and entry.get("rb") is not None


def _setting_entry(raw: dict[str, Any], level: int) -> Any:
    criteria = raw.get("criteria") if isinstance(raw.get("criteria"), dict) else {}
    settings = criteria.get("settings") if isinstance(criteria.get("settings"), dict) else {}
    return settings.get(str(level), settings.get(level))


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _same_number(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return False
    return round(float(left), 4) == round(float(right), 4)


def _row(master: MachineMaster, *, overridden: bool) -> dict[str, Any]:
    override = master.raw.get("override") if isinstance(master.raw.get("override"), dict) else {}
    history = override.get("history") if isinstance(override.get("history"), list) else None
    if not history:
        history = master.raw.get("history") if isinstance(master.raw.get("history"), list) else []
    row = {
        "id": master.id,
        "name": master.name,
        "series": master.raw.get("series"),
        "maker": master.raw.get("maker"),
        "enabled": master.enabled,
        "added": bool(master.raw.get("added")),
        "override_active": overridden,
        "override_fields": override.get("fields") or [],
        "override_updated_at": override.get("updated_at"),
        "origins": field_origins(master),
        "history": [item for item in history if isinstance(item, dict)][-12:],
    }
    row.update(_comparable(master))
    return row
