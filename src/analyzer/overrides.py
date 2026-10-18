"""機種マスターの手動補正。次回の判定（40分巡回を含む）で公表値より優先する。"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OVERRIDES_PATH = PROJECT_ROOT / "data" / "masters" / "overrides.json"

HIT_TARGETS = ("bb", "rb", "combined")
MACHINE_TYPES = ("A", "AT")
SETTING_LEVELS = (1, 2, 3, 4, 5, 6)
REQUIRED_SETTINGS = ("setting_6", "setting_5", "setting_4")
PROBABILITY_FIELDS = tuple(f"setting_{level}" for level in SETTING_LEVELS) + tuple(
    f"rb_{level}" for level in SETTING_LEVELS
)
THRESHOLD_FIELDS = ("min_games", "early_min_games")
JUDGMENT_FIELDS = set(PROBABILITY_FIELDS) | {"type", "hit_target", *THRESHOLD_FIELDS}
LEVEL_BY_FIELD = {f"setting_{level}": level for level in SETTING_LEVELS}
LEVEL_BY_FIELD.update({f"rb_{level}": level for level in SETTING_LEVELS})
FIELD_LABELS = {
    "type": "タイプ",
    "hit_target": "初当り対象",
    "min_games": "規定G",
    "early_min_games": "朝一G",
    "note": "備考",
}
FIELD_LABELS.update({f"setting_{level}": f"設定{level}の初当り・合算" for level in SETTING_LEVELS})
FIELD_LABELS.update({f"rb_{level}": f"設定{level}のREG" for level in SETTING_LEVELS})
NOTE_LIMIT = 400
HISTORY_LIMIT = 12


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
    added = payload.get("added")
    if not isinstance(added, dict):
        added = {}
    return {
        "version": 1,
        "updated_at": payload.get("updated_at"),
        "machines": {str(key): value for key, value in machines.items() if isinstance(value, dict)},
        "added": {str(key): value for key, value in added.items() if isinstance(value, dict)},
    }


def save_override_document(document: dict[str, Any], path: Path | None = None) -> None:
    target = overrides_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "description": "管理画面で保存した機種マスターの上書き。判定時に config/machines.json より優先する。",
        "machines": document.get("machines") or {},
        "added": document.get("added") or {},
    }
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)


def apply_overrides(payload: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    """マスターJSONの machines に手動補正を重ねたコピーを返す。"""
    document = load_override_document(path)
    stored = document.get("machines") or {}
    added = document.get("added") or {}
    if not stored and not added:
        return payload
    machines = payload.get("machines") or []
    merged_machines: list[Any] = []
    seen: set[str] = set()
    for item in machines:
        if not isinstance(item, dict):
            merged_machines.append(item)
            continue
        machine_id = str(item.get("id") or "")
        if machine_id:
            seen.add(machine_id)
        override = stored.get(machine_id)
        if not isinstance(override, dict):
            merged_machines.append(item)
            continue
        merged_machines.append(merge_override(item, override))
    for machine_id, record in added.items():
        if machine_id in seen or not isinstance(record, dict):
            continue
        merged_machines.append(copy.deepcopy(record))
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
    for field in THRESHOLD_FIELDS:
        if override.get(field) is None:
            continue
        criteria[field] = int(override[field])
        changed.append(field)
    if "note" in override:
        merged["note"] = str(override.get("note") or "")
    history = override.get("history") if isinstance(override.get("history"), list) else []
    merged["override"] = {
        "active": any(field in JUDGMENT_FIELDS for field in changed),
        "fields": [field for field in changed if field in JUDGMENT_FIELDS],
        "updated_at": override.get("updated_at"),
        "note": merged.get("note") or "",
        "history": [item for item in history if isinstance(item, dict)][-HISTORY_LIMIT:],
    }
    return merged


def parse_edit(payload: dict[str, Any], *, require_mid_settings: bool = True) -> dict[str, Any]:
    """管理画面の1行を正規化する。空のREGと未入力の設定1〜3は未指定。"""
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
    for field in PROBABILITY_FIELDS:
        if field not in payload and field not in REQUIRED_SETTINGS:
            continue
        required = field in REQUIRED_SETTINGS and (require_mid_settings or field == "setting_6")
        if field in payload or required:
            parsed[field] = _parse_probability(payload.get(field), FIELD_LABELS[field], required=required)
    for field in THRESHOLD_FIELDS:
        if field not in payload:
            continue
        parsed[field] = _parse_games(payload.get(field), FIELD_LABELS[field])
    if "note" in payload:
        parsed["note"] = _parse_note(payload.get("note"))
    return parsed


def diff_against_base(parsed: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    """ベースマスターと違う項目だけを残す。全部同じなら空。"""
    changed: dict[str, Any] = {}
    if parsed["type"] != base["type"]:
        changed["type"] = parsed["type"]
    if parsed["hit_target"] != base["hit_target"]:
        changed["hit_target"] = parsed["hit_target"]
    for field in PROBABILITY_FIELDS:
        if field not in parsed:
            continue
        current = parsed.get(field)
        original = base.get(field)
        if current is None and original is None:
            continue
        if current is None:
            continue
        if original is None or not _same_number(current, original):
            changed[field] = current
    for field in THRESHOLD_FIELDS:
        if field not in parsed or parsed.get(field) is None:
            continue
        if parsed[field] != base.get(field):
            changed[field] = parsed[field]
    if "note" in parsed and (parsed.get("note") or "") != (base.get("note") or ""):
        changed["note"] = parsed.get("note") or ""
    return changed


def history_summary(changed: dict[str, Any]) -> str:
    """保存差分を備考の履歴1行にする。"""
    parts: list[str] = []
    for field in ("type", "hit_target", *PROBABILITY_FIELDS, *THRESHOLD_FIELDS):
        if field not in changed:
            continue
        parts.append(f"{FIELD_LABELS.get(field, field)}を{changed[field]}")
    if "note" in changed:
        parts.append("備考を更新")
    return "、".join(parts)


def append_history(previous: list[Any] | None, summary: str, at: str) -> list[dict[str, str]]:
    history = [item for item in (previous or []) if isinstance(item, dict) and item.get("summary")]
    if summary:
        history.append({"at": at, "summary": summary})
    return history[-HISTORY_LIMIT:]


def allocate_machine_id(name: str, taken: set[str]) -> str:
    digest = hashlib.sha1(name.strip().encode("utf-8")).hexdigest()[:8]
    candidate = f"custom-{digest}"
    index = 2
    while candidate in taken:
        candidate = f"custom-{digest}-{index}"
        index += 1
    return candidate


def build_machine_record(
    *,
    machine_id: str,
    name: str,
    parsed: dict[str, Any],
    aliases: list[str] | None = None,
    history: list[dict[str, str]] | None = None,
    added: bool = True,
) -> dict[str, Any]:
    """追加機種を、load_masters が読むマスター1件の形にする。"""
    settings: dict[str, dict[str, float]] = {}
    for level in SETTING_LEVELS:
        entry: dict[str, float] = {}
        synthetic = parsed.get(f"setting_{level}")
        rb = parsed.get(f"rb_{level}")
        if synthetic is not None:
            entry["synthetic"] = float(synthetic)
            entry["first_hit"] = float(synthetic)
        if rb is not None:
            entry["rb"] = float(rb)
        if entry:
            settings[str(level)] = entry
    criteria: dict[str, Any] = {
        "min_games": int(parsed.get("min_games") or 3000),
        "early_min_games": int(parsed.get("early_min_games") or 300),
        "settings": settings,
    }
    record: dict[str, Any] = {
        "id": machine_id,
        "name": name,
        "type": parsed["type"],
        "hit_target": parsed["hit_target"],
        "aliases": list(aliases or []),
        "note": parsed.get("note") or "",
        "added": added,
        "criteria": criteria,
        "history": list(history or []),
    }
    if parsed.get("setting_6") is not None:
        record["setting_6"] = float(parsed["setting_6"])
        criteria["synthetic_probability_threshold"] = float(parsed["setting_6"])
    for level in (5, 4, 3, 2, 1):
        if parsed.get(f"setting_{level}") is not None:
            record[f"setting_{level}"] = float(parsed[f"setting_{level}"])
    if parsed.get("rb_6") is not None:
        criteria["rb_probability_threshold"] = float(parsed["rb_6"])
    return record


def parse_new_machine(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if not isinstance(payload, dict):
        raise OverrideError("保存形式が正しくありません")
    name = str(payload.get("name") or "").strip()
    if not name or len(name) > 80:
        raise OverrideError("機種名は1〜80文字で入力してください")
    parsed = parse_edit(payload, require_mid_settings=False)
    if parsed.get("min_games") is None:
        parsed["min_games"] = _parse_games(3000, FIELD_LABELS["min_games"])
    if parsed.get("early_min_games") is None:
        parsed["early_min_games"] = _parse_games(300, FIELD_LABELS["early_min_games"])
    if "note" not in parsed:
        parsed["note"] = ""
    return name, parsed


def _parse_games(value: Any, label: str) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise OverrideError(f"{label}は整数で入力してください") from exc
    if number < 1 or number > 100000:
        raise OverrideError(f"{label}は 1 以上 100000 以下にしてください")
    return number


def _parse_note(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) > NOTE_LIMIT:
        raise OverrideError(f"備考は{NOTE_LIMIT}文字以内にしてください")
    return text


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
