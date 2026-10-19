"""隣り合う台番号のうち、機種の高設定基準を満たす並びだけを抜き出す。"""

from __future__ import annotations

import zlib
from types import SimpleNamespace
from typing import Any

from src.analyzer.judgment import (
    STATUS_ALL_HIGH,
    STATUS_EXCLUDED,
    STATUS_HIGH_56,
    STATUS_HIGH_BASE,
    STATUS_LABELS,
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
    master_enabled,
    observe_hit_target,
    probability_source_of,
    uses_legacy_observation,
)

_HIGH_STATUSES = {STATUS_ALL_HIGH, STATUS_HIGH_56, STATUS_HIGH_BASE}
_BLOCK_CACHE: dict[tuple, list[dict[str, Any]]] = {}


def attach_lineup_blocks(machines: list[Any]) -> None:
    """判定済み機種へ、高設定の並びだけを足す。該当が無い機種には付けない。"""
    if len(_BLOCK_CACHE) > 20000:
        _BLOCK_CACHE.clear()
    for machine in machines:
        if not isinstance(machine, dict):
            continue
        key = _cache_key(machine)
        blocks = _BLOCK_CACHE.get(key)
        if blocks is None:
            try:
                blocks = find_lineup_blocks(machine)
            except (TypeError, ValueError, KeyError, ZeroDivisionError) as exc:
                print(f"並び分析をスキップ: {type(exc).__name__}", flush=True)
                blocks = []
            _BLOCK_CACHE[key] = blocks
        if blocks:
            machine["lineup_blocks"] = blocks
        else:
            machine.pop("lineup_blocks", None)


def _cache_key(machine: dict[str, Any]) -> tuple:
    parts: list[str] = []
    units = machine.get("units") or []
    if isinstance(units, list):
        for unit in units:
            if not isinstance(unit, dict):
                parts.append("-")
                continue
            parts.append(
                "|".join(
                    (
                        str(unit.get("dai_no") or ""),
                        str(unit.get("real_normal_games") or ""),
                        str(unit.get("first_hit_count") or ""),
                        str(unit.get("bb_count") or ""),
                        str(unit.get("rb_count") or ""),
                        str(unit.get("diff_medals") or ""),
                        str(unit.get("status") or ""),
                        "1" if unit.get("judgment_excluded") else "0",
                    )
                )
            )
    digest = zlib.crc32("\n".join(parts).encode("utf-8"))
    return (
        str(machine.get("hall_name") or ""),
        str(machine.get("machine_name") or ""),
        str(machine.get("status") or ""),
        bool(machine.get("judgment_excluded")),
        digest,
    )


def find_lineup_blocks(machine: dict[str, Any]) -> list[dict[str, Any]]:
    """台番が連続する区間のうち、全6・全56・全456になる最長の並びを返す。"""
    if machine.get("judgment_excluded") or machine.get("status") == STATUS_EXCLUDED:
        return []
    units = machine.get("units")
    if not isinstance(units, list) or len(units) < 2:
        return []
    master = _master_from_criteria(machine.get("criteria") if isinstance(machine.get("criteria"), dict) else None)
    if master is None:
        return []
    blocks: list[dict[str, Any]] = []
    for island in _islands(units):
        blocks.extend(_blocks_in_island(machine, island, master))
    blocks.sort(key=lambda block: (-int(block["length"]), int(block["start_dai"])))
    return blocks


def _master_from_criteria(criteria: dict[str, Any] | None) -> SimpleNamespace | None:
    if not isinstance(criteria, dict) or not master_enabled(criteria):
        return None
    raw_settings = criteria.get("settings") or {}
    bars: dict[int, SettingBar] = {}
    if isinstance(raw_settings, dict):
        for key, entry in raw_settings.items():
            try:
                level = int(key)
            except (TypeError, ValueError):
                continue
            primary = _setting_primary(entry)
            if primary is None:
                continue
            rb = entry.get("rb") if isinstance(entry, dict) else None
            bars[level] = SettingBar(primary, float(rb) if rb is not None else None)
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
    if 4 not in bars and 5 not in bars and 6 not in bars:
        return None
    return SimpleNamespace(
        min_games=int(criteria.get("min_games") or 3000),
        early_min_games=int(criteria.get("early_min_games") or 300),
        settings=bars,
    )


def _dai_number(unit: dict[str, Any]) -> int | None:
    raw = unit.get("dai_no")
    if raw is None:
        return None
    digits = "".join(ch for ch in str(raw) if ch.isdigit())
    if not digits:
        return None
    return int(digits)


def _usable(unit: dict[str, Any]) -> bool:
    if not isinstance(unit, dict):
        return False
    if unit.get("judgment_excluded") or unit.get("status") == STATUS_EXCLUDED:
        return False
    if unit.get("chain_rule_id") and not unit.get("chain_separated"):
        return False
    return _dai_number(unit) is not None


def _islands(units: list[Any]) -> list[list[dict[str, Any]]]:
    ordered = [unit for unit in units if _usable(unit)]
    ordered.sort(key=lambda unit: _dai_number(unit) or 0)
    islands: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    prev: int | None = None
    for unit in ordered:
        number = _dai_number(unit)
        if number is None:
            continue
        if prev is not None and number == prev:
            incoming_games = _as_int(unit.get("real_normal_games"))
            previous_games = _as_int(current[-1].get("real_normal_games")) if current else None
            if current and incoming_games is not None and (previous_games or 0) < incoming_games:
                current[-1] = unit
            continue
        if prev is not None and number == prev + 1 and current:
            current.append(unit)
        else:
            if len(current) >= 2:
                islands.append(current)
            current = [unit]
        prev = number
    if len(current) >= 2:
        islands.append(current)
    return islands


def _blocks_in_island(
    machine: dict[str, Any],
    island: list[dict[str, Any]],
    master: SimpleNamespace,
) -> list[dict[str, Any]]:
    count = len(island)
    kept: list[tuple[int, int, dict[str, Any]]] = []
    for length in range(count, 1, -1):
        for start in range(0, count - length + 1):
            end = start + length
            if any(kept_start <= start and end <= kept_end for kept_start, kept_end, _block in kept):
                continue
            block = _judge_window(machine, island[start:end], master)
            if block is not None:
                kept.append((start, end, block))
    kept.sort(key=lambda item: item[0])
    return [block for _start, _end, block in kept]


def _judge_window(
    machine: dict[str, Any],
    units: list[dict[str, Any]],
    master: SimpleNamespace,
) -> dict[str, Any] | None:
    if len(units) < 2:
        return None
    observed = _observe(machine, units)
    if observed is None:
        return None
    games_for_class = observed["games_for_class"]
    synthetic = observed["synthetic"]
    rb_probability = observed["rb_probability"]
    avg_games = observed["avg_games"]
    compare_as = observed["compare_as"]
    if games_for_class is None or synthetic is None or avg_games is None:
        return None
    early_enough = games_for_class >= master.early_min_games
    meets6 = _meets_setting6(
        compare_as,
        synthetic,
        rb_probability,
        master.settings.get(6),
        master.settings.get(5),
        sample_games=avg_games,
    )
    meets5 = _meets_setting5(
        compare_as,
        synthetic,
        rb_probability,
        master.settings.get(5),
        master.settings.get(4),
        sample_games=avg_games,
    )
    meets4 = _meets_setting(compare_as, synthetic, rb_probability, master.settings.get(4))
    if not early_enough:
        meets6 = False
        meets5 = False
        meets4 = False
    if None in (meets6, meets5, meets4):
        return None
    status = _classify(games_for_class, master, bool(meets6), bool(meets5), bool(meets4))
    if status not in _HIGH_STATUSES:
        return None
    start = _dai_number(units[0])
    end = _dai_number(units[-1])
    if start is None or end is None:
        return None
    length = len(units)
    return {
        "start_dai": start,
        "end_dai": end,
        "length": length,
        "title": f"{start}号機〜{end}号機【{length}台並び】",
        "status": status,
        "status_label": STATUS_LABELS[status],
        "avg_first_hit_probability": synthetic,
        "avg_games": avg_games,
        "avg_diff_medals": _avg_diff(units),
        "units": [_unit_row(unit) for unit in units],
    }


def _observe(machine: dict[str, Any], units: list[dict[str, Any]]) -> dict[str, Any] | None:
    judgment_type = machine.get("judgment_type")
    criteria = machine.get("criteria") if isinstance(machine.get("criteria"), dict) else None
    target_count = str(
        (criteria or {}).get("target_count")
        or (criteria or {}).get("hit_source")
        or ("cz" if judgment_type == "AT" else "bonus")
    )
    hit_target = forced_hit_target(
        judgment_type,
        str((criteria or {}).get("hit_target") or default_hit_target(judgment_type)),
    )
    compare_as = compare_type_for(judgment_type, hit_target)
    games = _sum_exact([_as_int(unit.get("real_normal_games")) for unit in units])
    bb = _sum_optional([_as_int(unit.get("bb_count")) for unit in units])
    rb = _sum_optional([_as_int(unit.get("rb_count")) for unit in units])
    art = _sum_optional([_as_int(unit.get("art_count")) for unit in units])
    unit_count = len(units)
    if judgment_type == "AT":
        first_hit, synthetic, rb_probability = at_observation(
            bb,
            rb,
            art,
            games,
            target_count=target_count,
            probability_source=probability_source_of(criteria),
        )
    elif judgment_type == "A" and not uses_legacy_observation(judgment_type, hit_target):
        first_hit, synthetic, rb_probability = observe_hit_target(hit_target, bb, rb, games)
    elif judgment_type == "A":
        if bb is not None or rb is not None:
            first_hit = (bb or 0) + (rb or 0)
            if first_hit <= 0:
                first_hit = None
        else:
            first_hit = _sum_optional([_as_int(unit.get("first_hit_count")) for unit in units])
        synthetic = _ratio_from_counts(games, first_hit)
        rb_probability = _ratio_from_counts(games, rb)
    else:
        first_hit = _sum_optional([_as_int(unit.get("first_hit_count")) for unit in units])
        synthetic = _ratio_from_counts(games, first_hit)
        rb_probability = _ratio_from_counts(games, rb)

    ruled = [unit for unit in units if unit.get("chain_rule_id")]
    if judgment_type == "AT" and ruled:
        usable = [
            unit
            for unit in ruled
            if unit.get("chain_separated")
            and not unit.get("judgment_excluded")
            and unit.get("status") != STATUS_EXCLUDED
        ]
        if len(usable) < 2:
            return None
        first_hit = _sum_optional([_as_int(unit.get("first_hit_count")) for unit in usable])
        games = _sum_optional([_as_int(unit.get("real_normal_games")) for unit in usable])
        synthetic = _ratio_from_counts(games, first_hit)
        rb_probability = synthetic
        unit_count = len(usable)

    avg_games = round(games / unit_count, 1) if games is not None and unit_count else None
    games_for_class = int(avg_games) if avg_games is not None else None
    return {
        "compare_as": compare_as,
        "synthetic": synthetic,
        "rb_probability": rb_probability,
        "avg_games": avg_games,
        "games_for_class": games_for_class,
        "first_hit": first_hit,
    }


def _avg_diff(units: list[dict[str, Any]]) -> int | None:
    known = [value for value in (_as_int(unit.get("diff_medals")) for unit in units) if value is not None]
    if not known:
        return None
    return int(round(sum(known) / len(known)))


def _unit_row(unit: dict[str, Any]) -> dict[str, Any]:
    games = _as_int(unit.get("real_normal_games"))
    if games is None:
        games = _as_int(unit.get("total_games"))
    hits = _as_int(unit.get("first_hit_count"))
    probability = unit.get("synthetic_probability")
    if probability is None:
        probability = _ratio_from_counts(games, hits)
    return {
        "dai_no": str(_dai_number(unit) if _dai_number(unit) is not None else unit.get("dai_no") or ""),
        "total_games": games,
        "first_hit_count": hits,
        "first_hit_probability": probability,
        "diff_medals": _as_int(unit.get("diff_medals")),
    }
