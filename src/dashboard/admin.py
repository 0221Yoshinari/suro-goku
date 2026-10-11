"""管理画面向けの機種マスター一覧・保存・検品。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from src.analyzer.judgment import (
    DEFAULT_OUTPUT_DIR,
    MachineMaster,
    load_masters,
)
from src.analyzer.overrides import (
    OverrideError,
    diff_against_base,
    load_override_document,
    overrides_path,
    parse_edit,
    save_override_document,
)

INSPECTION_STATUSES = ("ALL_HIGH", "HIGH_56", "HIGH_BASE")


def list_master_rows() -> dict[str, Any]:
    base_rows = {item.id: _row(item, overridden=False) for item in load_masters(apply_override=False)}
    rows: list[dict[str, Any]] = []
    for item in load_masters():
        current = _row(item, overridden=bool((item.raw.get("override") or {}).get("active")))
        base = base_rows.get(item.id) or current
        current["base"] = {
            "type": base["type"],
            "hit_target": base["hit_target"],
            "setting_6": base["setting_6"],
            "setting_5": base["setting_5"],
            "setting_4": base["setting_4"],
            "rb_6": base["rb_6"],
            "rb_5": base["rb_5"],
            "rb_4": base["rb_4"],
        }
        rows.append(current)
    rows.sort(key=lambda item: (not item["override_active"], item["series"] or "", item["name"]))
    document = load_override_document()
    return {
        "count": len(rows),
        "override_count": sum(1 for item in rows if item["override_active"]),
        "overrides_path": str(overrides_path()),
        "updated_at": document.get("updated_at"),
        "machines": rows,
    }


def save_master_edit(machine_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    machine_id = str(machine_id or "").strip()
    base = _find_master(machine_id, apply_override=False)
    if base is None:
        raise OverrideError("登録されていない機種です")
    parsed = parse_edit(payload)
    changed = diff_against_base(parsed, _comparable(base))
    document = load_override_document()
    machines = document.setdefault("machines", {})
    if changed:
        changed["updated_at"] = datetime.now(timezone.utc).isoformat()
        machines[machine_id] = changed
    else:
        machines.pop(machine_id, None)
    save_override_document(document)
    saved = _find_master(machine_id, apply_override=True)
    if saved is None:
        raise OverrideError("保存後の機種を読み直せませんでした")
    row = _row(saved, overridden=bool(changed))
    row["base"] = _comparable(base)
    row["saved_fields"] = sorted(changed.keys() - {"updated_at"})
    return row


def clear_master_edit(machine_id: str) -> dict[str, Any]:
    machine_id = str(machine_id or "").strip()
    base = _find_master(machine_id, apply_override=False)
    if base is None:
        raise OverrideError("登録されていない機種です")
    document = load_override_document()
    machines = document.setdefault("machines", {})
    machines.pop(machine_id, None)
    save_override_document(document)
    row = _row(base, overridden=False)
    row["base"] = _comparable(base)
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
    return {
        "type": master.type,
        "hit_target": master.hit_target,
        "setting_6": _bar_value(master, 6, rb=False),
        "setting_5": _bar_value(master, 5, rb=False),
        "setting_4": _bar_value(master, 4, rb=False),
        "rb_6": _bar_value(master, 6, rb=True),
        "rb_5": _bar_value(master, 5, rb=True),
        "rb_4": _bar_value(master, 4, rb=True),
    }


def _row(master: MachineMaster, *, overridden: bool) -> dict[str, Any]:
    override = master.raw.get("override") if isinstance(master.raw.get("override"), dict) else {}
    row = {
        "id": master.id,
        "name": master.name,
        "series": master.raw.get("series"),
        "maker": master.raw.get("maker"),
        "enabled": master.enabled,
        "override_active": overridden,
        "override_fields": override.get("fields") or [],
        "override_updated_at": override.get("updated_at"),
    }
    row.update(_comparable(master))
    return row
