"""末尾判定の補助。"""

from __future__ import annotations

from typing import Any

from src.analyzer.judgment import (
    STATUS_ALL_HIGH,
    STATUS_HIGH_BASE,
    STATUS_INCOMPLETE,
    STATUS_LABELS,
    STATUS_NOT_HIGH,
    SettingBar,
    _as_int,
    _classify,
    _meets_setting,
    _meets_setting5,
    _meets_setting6,
    _ratio_from_counts,
    _setting_primary,
    _sum_exact,
    _sum_optional,
    at_observation,
    compare_type_for,
    default_hit_target,
    forced_hit_target,
    observe_hit_target,
    probability_source_of,
    uses_legacy_observation,
)

STATUS_HIGH_56 = "HIGH_56"
STATUS_NORMAL = "NORMAL"
STATUS_LABELS_EXT = {
    **STATUS_LABELS,
    STATUS_HIGH_56: "全56数値",
    STATUS_NORMAL: "通常",
    STATUS_NOT_HIGH: "通常",
}


def dai_number(value: Any) -> int | None:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if not digits:
        return None
    return int(digits)


def dai_digit(value: Any) -> int | None:
    number = dai_number(value)
    if number is None:
        return None
    return number % 10


def classify_extended(
    total_games: int,
    master: Any,
    meets6: bool,
    meets5: bool,
    meets4: bool,
) -> str:
    status = _classify(total_games, master, meets6, meets5, meets4)
    if status == STATUS_HIGH_BASE and meets5:
        return STATUS_HIGH_56
    return status


def _bars_from_criteria(criteria: dict[str, Any] | None) -> dict[int, SettingBar]:
    bars: dict[int, SettingBar] = {}
    if not criteria:
        return bars
    raw_settings = criteria.get("settings") or {}
    for key, entry in raw_settings.items():
        try:
            level = int(key)
        except (TypeError, ValueError):
            continue
        primary = _setting_primary(entry)
        if primary is None:
            continue
        rb = entry.get("rb") if isinstance(entry, dict) else None
        bars[level] = SettingBar(
            synthetic=primary,
            rb=float(rb) if rb is not None else None,
        )
    syn6 = criteria.get("synthetic_probability_threshold")
    if 6 not in bars and syn6 is not None:
        rb6 = criteria.get("rb_probability_threshold")
        bars[6] = SettingBar(float(syn6), float(rb6) if rb6 is not None else None)
    for level in (1, 2, 3, 4, 5, 6):
        top = criteria.get(f"setting_{level}")
        if top is None:
            continue
        existing = bars.get(level)
        bars[level] = SettingBar(float(top), existing.rb if existing else None)
    return bars


def aggregate_units(
    units: list[dict[str, Any]],
    *,
    judgment_type: str | None,
    criteria: dict[str, Any] | None,
    games_key: str = "total",
) -> dict[str, Any]:
    bars = _bars_from_criteria(criteria)
    games = _sum_exact([_as_int(u.get("real_normal_games") if "real_normal_games" in u else u.get("total_games")) for u in units])
    bb = _sum_optional([_as_int(u.get("bb_count")) for u in units])
    rb = _sum_optional([_as_int(u.get("rb_count")) for u in units])
    art = _sum_optional([_as_int(u.get("art_count")) for u in units])
    unit_count = len(units)
    avg_games = round(games / unit_count, 1) if games is not None and unit_count else None
    hit_source = str((criteria or {}).get("hit_source") or ("rb" if judgment_type == "AT" else "counts"))
    target_count = str((criteria or {}).get("target_count") or hit_source)
    hit_target = forced_hit_target(
        judgment_type,
        str((criteria or {}).get("hit_target") or default_hit_target(judgment_type)),
    )
    compare_as = compare_type_for(judgment_type, hit_target)
    legacy = uses_legacy_observation(judgment_type, hit_target)
    if judgment_type == "AT":
        first_hit, synthetic, rb_probability = at_observation(
            bb,
            rb,
            art,
            games,
            target_count="cz",
            probability_source=probability_source_of(criteria),
        )
    elif not legacy and judgment_type == "A":
        first_hit, synthetic, rb_probability = observe_hit_target(hit_target, bb, rb, games)
    elif judgment_type == "A":
        first_hit = (bb or 0) + (rb or 0)
        first_hit = first_hit if first_hit > 0 else None
        synthetic = _ratio_from_counts(games, first_hit)
    else:
        first_hit = _sum_optional([_as_int(u.get("first_hit_count")) for u in units])
        synthetic = _ratio_from_counts(games, first_hit)
    if legacy and judgment_type != "AT":
        rb_probability = _ratio_from_counts(games, rb)
    bb_probability = _ratio_from_counts(games, bb)

    min_games = int((criteria or {}).get("min_games") or 3000)
    early_min = int((criteria or {}).get("early_min_games") or 300)
    games_for_class = games if games_key == "total" else (int(avg_games) if avg_games is not None else None)
    master = type("M", (), {"min_games": min_games, "early_min_games": early_min})()

    meets6 = (
        _meets_setting6(
            compare_as,
            synthetic,
            rb_probability,
            bars.get(6),
            bars.get(5),
            sample_games=avg_games,
        )
        if bars.get(6)
        else None
    )
    meets5 = (
        _meets_setting5(
            compare_as,
            synthetic,
            rb_probability,
            bars.get(5),
            bars.get(4),
            sample_games=avg_games,
        )
        if bars.get(5)
        else None
    )
    meets4 = _meets_setting(compare_as, synthetic, rb_probability, bars.get(4)) if bars.get(4) else None
    if games_for_class is not None and games_for_class < early_min:
        meets6 = False
        meets5 = False
        meets4 = False
    if games_for_class is None or synthetic is None or None in (meets6, meets5, meets4):
        status = STATUS_INCOMPLETE
    else:
        status = classify_extended(games_for_class, master, bool(meets6), bool(meets5), bool(meets4))

    return {
        "unit_count": unit_count,
        "total_games": games,
        "avg_games": avg_games,
        "bb_count": bb,
        "rb_count": rb,
        "art_count": art,
        "synthetic_probability": synthetic,
        "bb_probability": bb_probability,
        "rb_probability": rb_probability,
        "meets_setting_6": meets6,
        "meets_setting_5": meets5,
        "meets_setting_4": meets4,
        "status": status,
        "status_label": STATUS_LABELS_EXT.get(status, STATUS_LABELS.get(status, "データ不足")),
        "is_all_high": status == STATUS_ALL_HIGH,
        "is_high_56": status == STATUS_HIGH_56 or status == STATUS_ALL_HIGH,
        "is_high_base": status in (STATUS_HIGH_BASE, STATUS_HIGH_56, STATUS_ALL_HIGH),
    }


def _shown_setting(level: int) -> int:
    """平均に使う設定値。基準未満は設定2として数える。"""
    if level >= 6:
        return 6
    if level <= 0:
        return 2
    return level


def _unit_setting_level(unit: dict[str, Any]) -> int | None:
    if unit.get("judgment_excluded") or unit.get("status") == "EXCLUDED":
        return None
    estimated = unit.get("estimated_setting")
    if isinstance(estimated, (int, float)) and not isinstance(estimated, bool):
        return int(estimated)
    checks = unit.get("checks") or {}
    if checks.get("meets_setting_6") is True:
        return 6
    if checks.get("meets_setting_5") is True:
        return 5
    if checks.get("meets_setting_4") is True:
        return 4
    if checks.get("meets_setting_4") is False:
        return 0
    if unit.get("judgment_type") != "AT":
        return None
    rb = unit.get("rb_probability")
    settings = ((unit.get("criteria") or {}).get("settings") or {})
    if rb is None:
        return None
    for level in (6, 5, 4):
        thresh = (settings.get(str(level)) or {}).get("rb")
        if thresh is not None and rb <= float(thresh):
            return level
    if any((settings.get(str(level)) or {}).get("rb") is not None for level in (4, 5, 6)):
        return 0
    return None


def _setting_from_avg(avg: float | None, rated_count: int) -> tuple[str, str]:
    if rated_count <= 0 or avg is None:
        return STATUS_INCOMPLETE, "データ不足"
    if avg >= 5.5:
        return STATUS_ALL_HIGH, STATUS_LABELS_EXT[STATUS_ALL_HIGH]
    if avg >= 4.75:
        return STATUS_HIGH_56, STATUS_LABELS_EXT[STATUS_HIGH_56]
    if avg >= 3.75:
        return STATUS_HIGH_BASE, STATUS_LABELS_EXT[STATUS_HIGH_BASE]
    return STATUS_NORMAL, STATUS_LABELS_EXT[STATUS_NORMAL]


def summarize_digit_units(units: list[dict[str, Any]], digit: int) -> dict[str, Any]:
    usable = [
        unit
        for unit in units
        if not unit.get("judgment_excluded") and unit.get("status") != "EXCLUDED"
    ]
    a_units = [unit for unit in usable if unit.get("judgment_type") == "A"]
    at_units = [unit for unit in usable if unit.get("judgment_type") == "AT"]
    games = _sum_exact([_as_int(unit.get("real_normal_games") if "real_normal_games" in unit else unit.get("total_games")) for unit in usable])
    unit_count = len(usable)
    avg_games = round(games / unit_count, 1) if games is not None and unit_count else None

    a_games = _sum_exact([_as_int(unit.get("real_normal_games") if "real_normal_games" in unit else unit.get("total_games")) for unit in a_units])
    a_bb = _sum_optional([_as_int(unit.get("bb_count")) for unit in a_units])
    a_rb = _sum_optional([_as_int(unit.get("rb_count")) for unit in a_units])
    a_first = None
    if a_bb is not None or a_rb is not None:
        a_first = (a_bb or 0) + (a_rb or 0)
        a_first = a_first if a_first > 0 else None
    a_synthetic = _ratio_from_counts(a_games, a_first)
    a_rb_probability = _ratio_from_counts(a_games, a_rb)
    a_bb_probability = _ratio_from_counts(a_games, a_bb)

    at_games = _sum_exact([_as_int(unit.get("real_normal_games") if "real_normal_games" in unit else unit.get("total_games")) for unit in at_units])
    at_rb = _sum_optional([_as_int(unit.get("rb_count")) for unit in at_units])
    at_first_hit = _ratio_from_counts(at_games, at_rb)

    levels = [_unit_setting_level(unit) for unit in usable]
    weighted_sum = 0.0
    weight_total = 0.0
    rated_levels: list[int] = []
    for unit, level in zip(usable, levels):
        if level is None:
            continue
        shown = _shown_setting(level)
        games_n = _as_int(unit.get("real_normal_games") if "real_normal_games" in unit else unit.get("total_games"))
        weight = float(games_n) if games_n and games_n > 0 else 1.0
        weighted_sum += shown * weight
        weight_total += weight
        rated_levels.append(level)
    avg_setting = round(weighted_sum / weight_total, 2) if weight_total else None
    setting6_count = sum(1 for level in rated_levels if level == 6)
    setting5_count = sum(1 for level in rated_levels if level >= 5)
    setting4_count = sum(1 for level in rated_levels if level >= 4)
    rated_count = len(rated_levels)
    if unit_count <= 0:
        status, status_label = STATUS_INCOMPLETE, "台なし"
    elif avg_setting is None:
        status, status_label = STATUS_NORMAL, "通常"
    else:
        status, status_label = _setting_from_avg(avg_setting, rated_count)
    rank_label = f"末尾{digit}：{status_label}" if unit_count else f"末尾{digit}：台なし"
    return {
        "digit": digit,
        "unit_count": unit_count,
        "a_unit_count": len(a_units),
        "at_unit_count": len(at_units),
        "total_games": games,
        "avg_games": avg_games,
        "a_synthetic_probability": a_synthetic,
        "a_bb_probability": a_bb_probability,
        "a_rb_probability": a_rb_probability,
        "at_first_hit_probability": at_first_hit,
        "avg_setting": avg_setting,
        "rated_count": rated_count,
        "setting6_count": setting6_count,
        "setting5_count": setting5_count,
        "setting4_count": setting4_count,
        "status": status,
        "status_label": status_label,
        "rank_label": rank_label,
        "is_all_high": status == STATUS_ALL_HIGH,
        "is_high_56": status in (STATUS_HIGH_56, STATUS_ALL_HIGH),
        "is_high_base": status in (STATUS_HIGH_BASE, STATUS_HIGH_56, STATUS_ALL_HIGH),
        "is_hit": status in (STATUS_ALL_HIGH, STATUS_HIGH_56, STATUS_HIGH_BASE),
    }


def _dai_sort_key(value: Any) -> int:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if not digits:
        return 10**9
    return int(digits)


def digit_unit_rows(machines: list[dict[str, Any]], hall_name: str, digit: int) -> list[dict[str, Any]]:
    """1店舗・1末尾に属する台だけを、詳細表向けに返す。"""
    wanted = (hall_name or "").strip()
    rows: list[dict[str, Any]] = []
    for machine in machines:
        if not isinstance(machine, dict):
            continue
        if str(machine.get("hall_name") or "").strip() != wanted:
            continue
        machine_name = str(machine.get("machine_name") or "")
        for unit in machine.get("units") or []:
            if not isinstance(unit, dict):
                continue
            if unit.get("judgment_excluded") or unit.get("status") == "EXCLUDED":
                continue
            if dai_digit(unit.get("dai_no")) != digit:
                continue
            games = unit.get("real_normal_games")
            if games is None:
                games = unit.get("total_games")
            rows.append(
                {
                    "dai_no": unit.get("dai_no"),
                    "hall_name": wanted,
                    "machine_name": str(unit.get("machine_name") or machine_name),
                    "real_normal_games": games,
                    "diff_medals": unit.get("diff_medals"),
                    "bb_count": unit.get("bb_count"),
                    "rb_count": unit.get("rb_count"),
                    "estimated_setting": unit.get("estimated_setting"),
                    "estimated_setting_label": unit.get("estimated_setting_label"),
                    "status": unit.get("status"),
                    "status_label": unit.get("status_label"),
                }
            )
    rows.sort(key=lambda row: (_dai_sort_key(row.get("dai_no")), str(row.get("machine_name") or "")))
    return rows


def hall_digit_summaries(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """店舗全体の台を末尾0〜9で合算し、平均推測設定で数値判定する。"""
    by_hall: dict[str, dict[int, list[dict[str, Any]]]] = {}
    for unit in results:
        hall = str(unit.get("hall_name") or "")
        if not hall:
            continue
        digit = dai_digit(unit.get("dai_no"))
        if digit is None:
            continue
        by_hall.setdefault(hall, {d: [] for d in range(10)})[digit].append(unit)

    boards: list[dict[str, Any]] = []
    for hall in sorted(by_hall):
        digits = [summarize_digit_units(by_hall[hall][digit], digit) for digit in range(10)]
        hits = [row for row in digits if row.get("is_hit")]
        boards.append(
            {
                "hall_name": hall,
                "unit_count": sum(row["unit_count"] for row in digits),
                "digits": digits,
                "hit_digits": [
                    {
                        "digit": row["digit"],
                        "status": row["status"],
                        "status_label": row["status_label"],
                        "avg_setting": row.get("avg_setting"),
                    }
                    for row in hits
                ],
            }
        )
    return boards


def digit_judgments(
    units: list[dict[str, Any]],
    *,
    judgment_type: str | None,
    criteria: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    buckets: dict[int, list[dict[str, Any]]] = {d: [] for d in range(10)}
    for unit in units:
        digit = dai_digit(unit.get("dai_no"))
        if digit is None:
            continue
        buckets[digit].append(unit)
    rows = []
    for digit in range(10):
        group = buckets[digit]
        if not group:
            rows.append(
                {
                    "digit": digit,
                    "unit_count": 0,
                    "status": STATUS_INCOMPLETE,
                    "status_label": "台なし",
                    "synthetic_probability": None,
                    "bb_probability": None,
                    "rb_probability": None,
                    "total_games": None,
                    "is_all_high": False,
                    "is_high_56": False,
                    "is_high_base": False,
                }
            )
            continue
        stats = aggregate_units(group, judgment_type=judgment_type, criteria=criteria, games_key="total")
        rows.append({"digit": digit, **stats})
    return rows
