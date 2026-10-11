"""機種マスターの手動補正。次回の判定（40分巡回を含む）で公表値より優先する。"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OVERRIDES_PATH = PROJECT_ROOT / "data" / "masters" / "overrides.json"

HIT_TARGETS = ("bb", "rb", "combined")
MACHINE_TYPES = ("A", "AT")
PROBABILITY_FIELDS = ("setting_6", "setting_5", "setting_4", "rb_6", "rb_5", "rb_4")
LEVEL_BY_FIELD = {
    "setting_6": 6,
    "setting_5": 5,
    "setting_4": 4,
    "rb_6": 6,
    "rb_5": 5,
    "rb_4": 4,
}


class OverrideError(ValueError):
    """管理画面から受け取った補正値が不正。"""


def overrides_path(path: Path | None = None) -> Path:
    return path if path is not None else DEFAULT_OVERRIDES_PATH


def load_override_document(path: Path | None = None) -> dict[str, Any]:
    target = overrides_path(path)
    if not target.is_file():
        return {"version": 1, "updated_at": None, "machines": {}}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"オーバーライドを読めませんでした ({target}): {exc}", flush=True)
        return {"version": 1, "updated_at": None, "machines": {}}
    if not isinstance(payload, dict):
        return {"version": 1, "updated_at": None, "machines": {}}
    machines = payload.get("machines")
    if not isinstance(machines, dict):
        machines = {}
    return {
        "version": 1,
        "updated_at": payload.get("updated_at"),
        "machines": {str(key): value for key, value in machines.items() if isinstance(value, dict)},
    }


def save_override_document(document: dict[str, Any], path: Path | None = None) -> None:
    target = overrides_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "description": "管理画面で保存した機種マスターの上書き。判定時に config/machines.json より優先する。",
        "machines": document.get("machines") or {},
    }
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)


def apply_overrides(payload: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    """マスターJSONの machines に手動補正を重ねたコピーを返す。"""
    document = load_override_document(path)
    stored = document.get("machines") or {}
    if not stored:
        return payload
    machines = payload.get("machines") or []
    merged_machines: list[Any] = []
    for item in machines:
        if not isinstance(item, dict):
            merged_machines.append(item)
            continue
        machine_id = str(item.get("id") or "")
        override = stored.get(machine_id)
        if not isinstance(override, dict):
            merged_machines.append(item)
            continue
        merged_machines.append(merge_override(item, override))
    return {**payload, "machines": merged_machines}


def merge_override(item: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(item)
    changed: list[str] = []
    machine_type = str(override.get("type") or "").strip().upper()
    if machine_type in MACHINE_TYPES:
        merged["type"] = machine_type
        changed.append("type")
    hit_target = str(override.get("hit_target") or "").strip().lower()
    if hit_target in HIT_TARGETS:
        if str(merged.get("type") or "").strip().upper() != "A":
            hit_target = "rb"
        merged["hit_target"] = hit_target
        changed.append("hit_target")
    criteria = merged.setdefault("criteria", {})
    if not isinstance(criteria, dict):
        criteria = {}
        merged["criteria"] = criteria
    settings = criteria.setdefault("settings", {})
    if not isinstance(settings, dict):
        settings = {}
        criteria["settings"] = settings
    for field, level in LEVEL_BY_FIELD.items():
        if override.get(field) is None:
            continue
        value = float(override[field])
        entry = settings.get(str(level))
        if not isinstance(entry, dict):
            entry = {}
        if field.startswith("rb_"):
            entry["rb"] = value
            if level == 6:
                criteria["rb_probability_threshold"] = value
        else:
            merged[field] = value
            entry["synthetic"] = value
            entry["first_hit"] = value
            if level == 6:
                criteria["synthetic_probability_threshold"] = value
        settings[str(level)] = entry
        changed.append(field)
    merged["override"] = {
        "active": True,
        "fields": changed,
        "updated_at": override.get("updated_at"),
    }
    return merged


def parse_edit(payload: dict[str, Any]) -> dict[str, Any]:
    """管理画面の1行を正規化する。空のREGは未指定。"""
    if not isinstance(payload, dict):
        raise OverrideError("保存形式が正しくありません")
    machine_type = str(payload.get("type") or "").strip().upper()
    if machine_type not in MACHINE_TYPES:
        raise OverrideError("タイプは Aタイプ または AT機 を選んでください")
    hit_target = str(payload.get("hit_target") or "").strip().lower()
    if machine_type == "AT":
        hit_target = "rb"
    elif hit_target not in HIT_TARGETS:
        raise OverrideError("初当り判定対象は BB / RB / 合算 を選んでください")
    parsed: dict[str, Any] = {"type": machine_type, "hit_target": hit_target}
    labels = {
        "setting_6": "設定6理論値",
        "setting_5": "全56（設定5）理論値",
        "setting_4": "全456（設定4）理論値",
        "rb_6": "設定6のREG理論値",
        "rb_5": "全56のREG理論値",
        "rb_4": "全456のREG理論値",
    }
    for field in ("setting_6", "setting_5", "setting_4"):
        parsed[field] = _parse_probability(payload.get(field), labels[field], required=True)
    for field in ("rb_6", "rb_5", "rb_4"):
        parsed[field] = _parse_probability(payload.get(field), labels[field], required=False)
    return parsed


def diff_against_base(parsed: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    """ベースマスターと違う項目だけを残す。全部同じなら空。"""
    changed: dict[str, Any] = {}
    if parsed["type"] != base["type"]:
        changed["type"] = parsed["type"]
    if parsed["hit_target"] != base["hit_target"]:
        changed["hit_target"] = parsed["hit_target"]
    for field in PROBABILITY_FIELDS:
        current = parsed.get(field)
        original = base.get(field)
        if current is None and original is None:
            continue
        if current is None:
            continue
        if original is None or not _same_number(current, original):
            changed[field] = current
    return changed


def _parse_probability(value: Any, label: str, *, required: bool) -> float | None:
    if value is None or value == "":
        if required:
            raise OverrideError(f"{label}を入力してください")
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise OverrideError(f"{label}は数値で入力してください") from exc
    if number != number or number <= 0 or number > 100000:
        raise OverrideError(f"{label}は 0 より大きく 100000 以下にしてください")
    return round(number, 4)


def _same_number(left: float, right: float) -> bool:
    return round(float(left), 4) == round(float(right), 4)
