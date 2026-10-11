#!/usr/bin/env python3
"""機種マスターを CSV で一括作成・更新する。

CSV 列:
  id,name,type,target_count,setting_4,setting_5,setting_6,min_games,early_min_games,aliases,
  enabled,probability_source,setting_1,setting_2,setting_3

enabled:
  false / 0 で判定除外（is_disabled）。空欄は既存値を維持する。
probability_source:
  rb のとき初当たりはサイトの RB 回数だけを使う。空欄は既存値を維持する。

target_count:
  bonus  サイトセブン BB（無ければ ART → RB）とボーナス確率を突合
  st_at  ART/AT 列と ST/AT 確率を突合
  cz     RB 列と CZ 確率を突合
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_JSON = PROJECT_ROOT / "config" / "machines.json"
LEGACY_JSON = PROJECT_ROOT / "data" / "masters" / "machines.json"
DEFAULT_CSV = PROJECT_ROOT / "config" / "machines_settings.csv"

KNOWN_OVERRIDES = {
    "tokyo-ghoul": {
        "target_count": "cz",
        "setting_4": 233.1,
        "setting_5": 216.4,
        "setting_6": 203.7,
        "comment": "サイトセブンRB列はCZ合算。設定4/5/6はCZ確率 1/233.1 / 1/216.4 / 1/203.7",
    },
    "kabaneri-kaimon": {
        "target_count": "bonus",
        "setting_4": 214.0,
        "setting_5": 203.2,
        "setting_6": 195.1,
        "comment": "サイトセブン集計はボーナス初当たり。設定4/5/6は 1/214.0 / 1/203.2 / 1/195.1（ST 1/318.5 は使わない）",
    },
}

CSV_FIELDS = [
    "id",
    "name",
    "type",
    "target_count",
    "setting_4",
    "setting_5",
    "setting_6",
    "min_games",
    "early_min_games",
    "aliases",
    "enabled",
    "probability_source",
    "setting_1",
    "setting_2",
    "setting_3",
]


def _as_bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return None


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _setting_primary(entry: dict[str, Any] | None) -> float | None:
    if not isinstance(entry, dict):
        return None
    for key in ("first_hit", "synthetic", "at_first_hit"):
        if entry.get(key) is not None:
            return float(entry[key])
    return None


def infer_target_count(item: dict[str, Any]) -> str:
    if str(item.get("type") or "").upper() == "A":
        return "bonus"
    raw = str(item.get("target_count") or item.get("hit_source") or "rb").strip().lower()
    aliases = {
        "bonus": "bonus",
        "bb": "bonus",
        "st_at": "st_at",
        "st": "st_at",
        "at": "st_at",
        "art": "st_at",
        "cz": "cz",
        "rb": "cz",
    }
    return aliases.get(raw, "cz")


def current_settings(item: dict[str, Any]) -> dict[int, float]:
    out: dict[int, float] = {}
    for level in (1, 2, 3, 4, 5, 6):
        top = _as_float(item.get(f"setting_{level}"))
        if top is not None:
            out[level] = top
    raw = ((item.get("criteria") or {}).get("settings") or {})
    for key, entry in raw.items():
        try:
            level = int(key)
        except (TypeError, ValueError):
            continue
        primary = _setting_primary(entry)
        if primary is not None and level not in out:
            out[level] = primary
    return out


def apply_fields(item: dict[str, Any], *, target_count: str | None, settings: dict[int, float], comment: str | None = None) -> None:
    machine_type = str(item.get("type") or "").strip().upper() or "AT"
    if machine_type != "A" and target_count:
        item["target_count"] = target_count
        item["hit_source"] = {"bonus": "bb", "st_at": "art", "cz": "rb"}.get(target_count, "rb")
    criteria = item.setdefault("criteria", {})
    nested = criteria.setdefault("settings", {})
    for level, value in settings.items():
        item[f"setting_{level}"] = value
        entry = nested.get(str(level))
        if not isinstance(entry, dict):
            entry = {}
            nested[str(level)] = entry
        key = "synthetic" if machine_type == "A" else "first_hit"
        entry[key] = value
        if machine_type != "A":
            entry["synthetic"] = value
    if 6 in settings:
        criteria["synthetic_probability_threshold"] = settings[6]
        published = item.setdefault("published", {})
        published["first_hit_denominator"] = settings[6]
        if comment:
            published["comment"] = comment


def export_csv(payload: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for item in payload.get("machines") or []:
            settings = current_settings(item)
            writer.writerow(
                {
                    "id": item.get("id") or "",
                    "name": item.get("name") or "",
                    "type": item.get("type") or "",
                    "target_count": infer_target_count(item),
                    "setting_4": settings.get(4, ""),
                    "setting_5": settings.get(5, ""),
                    "setting_6": settings.get(6, ""),
                    "min_games": (item.get("criteria") or {}).get("min_games") or 3000,
                    "early_min_games": (item.get("criteria") or {}).get("early_min_games") or 300,
                    "aliases": "|".join(str(a) for a in (item.get("aliases") or [])),
                    "enabled": "" if "enabled" not in item and "is_disabled" not in item else (
                        "false" if item.get("is_disabled") is True or item.get("enabled") is False else "true"
                    ),
                    "probability_source": item.get("probability_source") or "",
                    "setting_1": settings.get(1, ""),
                    "setting_2": settings.get(2, ""),
                    "setting_3": settings.get(3, ""),
                }
            )


def import_csv(payload: dict[str, Any], path: Path) -> int:
    by_id = {str(item.get("id")): item for item in payload.get("machines") or [] if item.get("id")}
    updated = 0
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            machine_id = (row.get("id") or "").strip()
            if not machine_id or machine_id not in by_id:
                continue
            item = by_id[machine_id]
            settings = {
                level: value
                for level, key in (
                    (1, "setting_1"),
                    (2, "setting_2"),
                    (3, "setting_3"),
                    (4, "setting_4"),
                    (5, "setting_5"),
                    (6, "setting_6"),
                )
                if (value := _as_float(row.get(key))) is not None
            }
            target_count = (row.get("target_count") or "").strip() or None
            apply_fields(item, target_count=target_count, settings=settings)
            if row.get("name"):
                item["name"] = row["name"].strip()
            if row.get("type"):
                item["type"] = row["type"].strip()
            if row.get("aliases"):
                item["aliases"] = [a for a in row["aliases"].split("|") if a.strip()]
            criteria = item.setdefault("criteria", {})
            if row.get("min_games"):
                criteria["min_games"] = int(float(row["min_games"]))
            if row.get("early_min_games"):
                criteria["early_min_games"] = int(float(row["early_min_games"]))
            enabled = _as_bool(row.get("enabled"))
            if enabled is not None:
                item["enabled"] = enabled
                item["is_disabled"] = not enabled
            source = (row.get("probability_source") or "").strip().lower()
            if source:
                item["probability_source"] = source
            updated += 1
    return updated


def apply_known(payload: dict[str, Any]) -> int:
    by_id = {str(item.get("id")): item for item in payload.get("machines") or []}
    updated = 0
    for machine_id, override in KNOWN_OVERRIDES.items():
        item = by_id.get(machine_id)
        if not item:
            continue
        apply_fields(
            item,
            target_count=str(override["target_count"]),
            settings={4: float(override["setting_4"]), 5: float(override["setting_5"]), 6: float(override["setting_6"])},
            comment=str(override.get("comment") or ""),
        )
        updated += 1
    return updated


def backfill_fields(payload: dict[str, Any]) -> int:
    updated = 0
    for item in payload.get("machines") or []:
        settings = current_settings(item)
        if not settings:
            continue
        apply_fields(item, target_count=infer_target_count(item), settings=settings)
        updated += 1
    return updated


def write_json(path: Path, payload: dict[str, Any]) -> None:
    payload["schema"] = {
        "type": "A = BIG/REG/合算判定、AT = target_count に応じた回数判定",
        "target_count": "bonus=BB（無ければART/RB）、st_at=ART、cz=RB。設定1〜6は同じカウントの公表1/N。",
        "enabled": "false または is_disabled=true で判定対象外（判定非表示）",
        "probability_source": "rb のとき初当たりはサイトのRB回数のみ。ART/BBへフォールバックしない",
        "setting_1": "設定1の対象確率 1/N（任意）",
        "setting_2": "設定2の対象確率 1/N（任意）",
        "setting_3": "設定3の対象確率 1/N（任意）",
        "setting_4": "設定4の対象確率 1/N",
        "setting_5": "設定5の対象確率 1/N",
        "setting_6": "設定6の対象確率 1/N",
        "add_machine": "CSV（config/machines_settings.csv）を編集して --import するか、machines 配列にオブジェクトを追加する。",
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="機種マスターの target_count / 設定4-6 を CSV で一括更新する")
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--export", action="store_true", help="JSON から CSV を書き出す")
    parser.add_argument("--import-csv", action="store_true", help="CSV を JSON に反映する")
    parser.add_argument("--apply-known", action="store_true", help="東京喰種・カバネリ海門決戦の補正を適用する")
    parser.add_argument("--backfill", action="store_true", help="既存 criteria.settings から setting_4/5/6 と target_count を埋める")
    args = parser.parse_args()

    payload = json.loads(args.json.read_text(encoding="utf-8"))
    changed = False
    if args.backfill:
        n = backfill_fields(payload)
        print(f"既存値をフラット化: {n} 件")
        changed = True
    if args.apply_known:
        n = apply_known(payload)
        print(f"既知機種を更新: {n} 件")
        changed = True
    if args.import_csv:
        n = import_csv(payload, args.csv)
        print(f"CSV を取り込み: {n} 件")
        changed = True
    if changed:
        write_json(args.json, payload)
        print(f"保存: {args.json}")
        if args.json.resolve() == DEFAULT_JSON.resolve() and LEGACY_JSON.parent.is_dir():
            LEGACY_JSON.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(args.json, LEGACY_JSON)
            print(f"同期: {LEGACY_JSON}")
    if args.export:
        if changed:
            payload = json.loads(args.json.read_text(encoding="utf-8"))
        export_csv(payload, args.csv)
        print(f"CSV を書き出しました: {args.csv}")
        return
    if not changed:
        parser.error("--export / --import-csv / --apply-known / --backfill のいずれかを指定してください")


if __name__ == "__main__":
    main()
