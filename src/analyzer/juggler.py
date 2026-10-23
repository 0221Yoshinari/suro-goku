"""ジャグラー島の抽出と、全6 / 全56 / 全456 の判定。

店舗×機種ファミリーを1島として、合算確率とREG確率を設定6/5/4の
公表ラインと比較する。平均回転が低くても欄からは外さず、低いことだけを残す。
"""

from __future__ import annotations

from typing import Any

from src.analyzer.judgment import SettingBar, _meets_setting, _meets_setting5, _meets_setting6
from src.normalize import norm_name

# 公表値（設定6 / 5 / 4 の合算・REG）。機種マスターが無い島のフォールバック。
_FAMILY_BARS: dict[str, dict[str, Any]] = {
    "im-juggler": {
        "min_games": 3000,
        "6": {"synthetic": 127.5, "rb": 255.0},
        "5": {"synthetic": 136.5, "rb": 273.1},
        "4": {"synthetic": 147.6, "rb": 321.3},
    },
    "neo-im-juggler": {
        "min_games": 3000,
        "6": {"synthetic": 127.5, "rb": 255.0},
        "5": {"synthetic": 136.5, "rb": 273.1},
        "4": {"synthetic": 147.6, "rb": 321.3},
    },
    "my-juggler-v": {
        "min_games": 3000,
        "6": {"synthetic": 114.6, "rb": 229.1},
        "5": {"synthetic": 126.8, "rb": 268.6},
        "4": {"synthetic": 135.4, "rb": 290.0},
    },
    "my-juggler-vi": {
        "min_games": 3000,
        "6": {"synthetic": 114.6, "rb": 229.1},
        "5": {"synthetic": 126.8, "rb": 268.6},
        "4": {"synthetic": 135.4, "rb": 290.0},
    },
    "gogo-juggler-3": {
        "min_games": 3000,
        "6": {"synthetic": 117.4, "rb": 234.1},
        "5": {"synthetic": 122.3, "rb": 248.5},
        "4": {"synthetic": 128.5, "rb": 268.6},
    },
    "funky-juggler-2": {
        "min_games": 3000,
        "6": {"synthetic": 119.6, "rb": 239.2},
        "5": {"synthetic": 124.2, "rb": 252.1},
        "4": {"synthetic": 129.8, "rb": 273.1},
    },
    "happy-juggler": {
        "min_games": 3000,
        "6": {"synthetic": 119.2, "rb": 238.4},
        "5": {"synthetic": 124.0, "rb": 252.1},
        "4": {"synthetic": 129.6, "rb": 273.1},
    },
    "miracle-juggler": {
        "min_games": 3000,
        "6": {"synthetic": 121.6, "rb": 243.2},
        "5": {"synthetic": 126.5, "rb": 256.0},
        "4": {"synthetic": 132.2, "rb": 277.7},
    },
    "mister-juggler": {
        "min_games": 3000,
        "6": {"synthetic": 118.7, "rb": 237.4},
        "5": {"synthetic": 123.4, "rb": 250.3},
        "4": {"synthetic": 129.0, "rb": 270.8},
    },
    "juggler-girls": {
        "min_games": 3000,
        "6": {"synthetic": 119.2, "rb": 238.4},
        "5": {"synthetic": 124.0, "rb": 252.1},
        "4": {"synthetic": 129.6, "rb": 273.1},
    },
}

# 長い・固有の名前を先に照合する。ネオアイムはアイムより前。
_FAMILIES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("my-juggler-vi", "マイジャグラーVI", ("マイジャグラーvi", "マイジャグラー6")),
    ("my-juggler-v", "マイジャグラーV", ("マイジャグラー",)),
    ("funky-juggler-2", "ファンキージャグラー2", ("ファンキージャグラー", "ファンキー2", "ファンキー２")),
    ("gogo-juggler-3", "ゴーゴージャグラー3", ("ゴーゴージャグラー", "ゴージャグ")),
    ("happy-juggler", "ハッピージャグラー", ("ハッピージャグラー", "ハッピージャグ")),
    ("mister-juggler", "ミスタージャグラー", ("ミスタージャグラー",)),
    ("juggler-girls", "ジャグラーガールズ", ("ジャグラーガールズ",)),
    ("miracle-juggler", "ウルトラミラクルジャグラー", ("ウルトラミラクル", "ミラクルジャグ", "ウルミラ")),
    ("neo-im-juggler", "ネオアイムジャグラーEX", ("ネオアイム", "ネオaim")),
    ("im-juggler", "アイムジャグラーEX", ("アイムジャグラー",)),
)

_DIFF_KEYS = ("diff_medals", "diff", "差玉", "差枚", "sadama", "substraction")
_BADGE_RANK = {
    "all6": 0,
    "high56": 1,
    "high456": 2,
    "none": 7,
}
_BADGE_LABELS = {
    "all6": "全6",
    "high56": "全56",
    "high456": "全456",
    "none": "",
}
_FORMING_MIN_GAMES = 1000
# ジャグラー島スコア。合算・REGは設定6/5/4のライン、回転は規定Gとの差。
SCORE_WEIGHT_RB = 0.45
SCORE_WEIGHT_SYNTHETIC = 0.30
SCORE_WEIGHT_GAMES = 0.25
SCORE_BANDS = (("6", 100.0), ("5", 72.0), ("4", 44.0))
SCORE_GAMES_FULL = 5000
SCORE_GAMES_FULL_RATIO = 1.6
SCORE_GAMES_FORMING = _FORMING_MIN_GAMES
SCORE_GAMES_EARLY = 300


def _rate_marker(name: str | None) -> str:
    """貸玉種別。20円と5円のように別フロアの台を、同じ島に足さない。"""
    text = str(name or "")
    start = text.rfind("〔")
    end = text.rfind("〕")
    if start >= 0 and end > start:
        return text[start + 1:end].strip()
    return ""


def classify_juggler_family(name: str | None, *, series: str | None = None) -> dict[str, str] | None:
    """ジャグラー系ならファミリー id と表示名。それ以外は None。"""
    key = norm_name(name)
    series_text = str(series or "")
    for family_id, label, patterns in _FAMILIES:
        if any(norm_name(pattern) in key for pattern in patterns):
            return {"id": family_id, "label": label}
    if "ジャグラー" in key or series_text == "ジャグラー":
        return {"id": "juggler-other", "label": "ジャグラー"}
    return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip().replace(",", "").replace("+", "")
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
        if number.is_integer():
            return int(number)
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _ratio(games: int | None, hits: int | None) -> float | None:
    if games is None or hits is None or hits <= 0:
        return None
    return round(games / hits, 1)


def _level_bar(entry: Any) -> dict[str, float | None] | None:
    if not isinstance(entry, dict):
        return None
    synthetic = _as_float(entry.get("synthetic") if entry.get("synthetic") is not None else entry.get("first_hit"))
    if synthetic is None:
        return None
    rb = _as_float(entry.get("rb"))
    return {"synthetic": synthetic, "rb": rb}


def bars_from_criteria(criteria: Any) -> dict[str, Any] | None:
    """機種マスターの設定ライン。合算が無ければ使わない。"""
    if not isinstance(criteria, dict):
        return None
    settings = criteria.get("settings") if isinstance(criteria.get("settings"), dict) else {}
    six = _level_bar(settings.get("6") or settings.get(6))
    if six is None:
        synthetic = _as_float(criteria.get("synthetic_probability_threshold"))
        if synthetic is None:
            return None
        six = {"synthetic": synthetic, "rb": _as_float(criteria.get("rb_probability_threshold"))}
    min_games = _as_int(criteria.get("min_games")) or 3000
    return {
        "min_games": min_games,
        "6": six,
        "5": _level_bar(settings.get("5") or settings.get(5)),
        "4": _level_bar(settings.get("4") or settings.get(4)),
    }


def _copy_bars(bars: dict[str, Any]) -> dict[str, Any]:
    copied: dict[str, Any] = {"min_games": int(bars.get("min_games") or 3000)}
    for level in ("6", "5", "4"):
        entry = bars.get(level)
        if isinstance(entry, dict) and entry.get("synthetic") is not None:
            copied[level] = {"synthetic": float(entry["synthetic"]), "rb": _as_float(entry.get("rb"))}
    return copied


def _better(actual: float | None, theory: float | None) -> bool:
    """分母が理論値以下なら、その設定ラインを満たす。"""
    if actual is None or theory is None:
        return False
    return actual <= theory


def _band_score_levels(actual: float | None, bars: dict[str, Any], field: str) -> float:
    if actual is None:
        return 0.0
    theories = []
    for level, points in SCORE_BANDS:
        theory = _as_float((bars.get(level) or {}).get(field))
        if theory is None:
            continue
        theories.append((theory, points))
        if actual <= theory:
            return points
    if not theories:
        return 0.0
    loosest = max(theory for theory, _points in theories)
    floor = SCORE_BANDS[-1][1]
    return max(0.0, floor - ((actual - loosest) / loosest) * floor)


def _games_score(avg_games: float | None, min_games: int) -> float:
    if avg_games is None or avg_games <= 0:
        return 0.0
    if avg_games >= max(min_games * SCORE_GAMES_FULL_RATIO, SCORE_GAMES_FULL):
        return 100.0
    if avg_games >= min_games:
        return 80.0
    if avg_games >= SCORE_GAMES_FORMING:
        return 45.0
    if avg_games >= SCORE_GAMES_EARLY:
        return 20.0
    return 0.0


def _setting_bar(entry: Any) -> SettingBar | None:
    if not isinstance(entry, dict) or entry.get("synthetic") is None:
        return None
    return SettingBar(float(entry["synthetic"]), _as_float(entry.get("rb")))


def score_island(
    *,
    unit_count: int,
    avg_games: float | None,
    synthetic: float | None,
    rb_probability: float | None,
    bars: dict[str, Any],
) -> dict[str, Any]:
    """全6 / 全56 / 全456。合算とREGが両方ラインに乗れば、平均回転が低くても載せる。

    設定6と設定5は、通常画面と同じく規定G以上で理論値の+4%までを同じランクにする。
    """
    del unit_count  # 台数では外さない。1台でもラインに乗れば同じ欄に出す。
    min_games = int(bars.get("min_games") or 3000)
    six = bars.get("6") or {}
    five = bars.get("5") or {}
    four = bars.get("4") or {}
    bar6 = _setting_bar(six if isinstance(six, dict) else None)
    bar5 = _setting_bar(five if isinstance(five, dict) else None)
    bar4 = _setting_bar(four if isinstance(four, dict) else None)
    meets6 = _meets_setting6("A", synthetic, rb_probability, bar6, bar5, sample_games=avg_games) is True
    meets5 = _meets_setting5("A", synthetic, rb_probability, bar5, bar4, sample_games=avg_games) is True
    meets4 = _meets_setting("A", synthetic, rb_probability, bar4) is True
    reg5 = _better(rb_probability, _as_float(five.get("rb")) if isinstance(five, dict) else None)
    reg6 = _better(rb_probability, _as_float(six.get("rb")) if isinstance(six, dict) else None)
    games_ready = avg_games is not None and avg_games >= min_games
    low_sample = not games_ready
    if meets6:
        badge = "all6"
    elif meets5:
        badge = "high56"
    elif meets4:
        badge = "high456"
    else:
        badge = "none"
    reg_score = _band_score_levels(rb_probability, bars, "rb")
    syn_score = _band_score_levels(synthetic, bars, "synthetic")
    spun = _games_score(avg_games, min_games)
    score = int(round(SCORE_WEIGHT_RB * reg_score + SCORE_WEIGHT_SYNTHETIC * syn_score + SCORE_WEIGHT_GAMES * spun))
    return {
        "score": score,
        "badge": badge,
        "badge_label": _BADGE_LABELS[badge],
        "hot": badge != "none",
        "meets_setting_6": meets6,
        "meets_setting_5": meets5,
        "meets_setting_4": meets4,
        "reg_meets_setting_6": reg6,
        "reg_meets_setting_5": reg5,
        "games_ready": games_ready,
        "low_sample": low_sample,
    }


def _diff_of(row: dict[str, Any]) -> int | None:
    for key in _DIFF_KEYS:
        value = _as_int(row.get(key))
        if value is not None:
            return value
    return None


def slump_from_history(history: Any) -> list[dict[str, Any]]:
    """履歴に差枚があれば、その推移を点列にする。無ければ空。"""
    if not isinstance(history, list):
        return []
    points: list[dict[str, Any]] = []
    for index, item in enumerate(history):
        if not isinstance(item, dict):
            continue
        diff = _diff_of(item)
        if diff is None:
            continue
        games = _as_int(item.get("games") if item.get("games") is not None else item.get("total_games"))
        label = item.get("day") or item.get("label") or index + 1
        points.append({"games": games, "diff": diff, "label": str(label)})
        if len(points) >= 80:
            break
    return points


def _dai_sort_key(value: Any) -> str:
    return str(value or "99999").zfill(8)


def _diff_label(total: int | None) -> str:
    if total is None:
        return "差枚なし"
    if total > 0:
        return "差枚プラス"
    if total < 0:
        return "差枚マイナス"
    return "差枚トントン"


def _unit_row(unit: dict[str, Any], *, machine_name: str, bars: dict[str, Any]) -> dict[str, Any]:
    games = _as_int(unit.get("total_games") if unit.get("total_games") is not None else unit.get("real_normal_games"))
    bb = _as_int(unit.get("bb_count"))
    rb = _as_int(unit.get("rb_count"))
    hits = None if bb is None and rb is None else (bb or 0) + (rb or 0)
    synthetic = _ratio(games, hits if hits else None)
    rb_probability = _ratio(games, rb)
    stored_syn = _as_float(unit.get("synthetic_probability"))
    stored_rb = _as_float(unit.get("rb_probability"))
    if synthetic is None:
        synthetic = round(stored_syn, 1) if stored_syn is not None else None
    if rb_probability is None:
        rb_probability = round(stored_rb, 1) if stored_rb is not None else None
    history = slump_from_history(unit.get("history") or unit.get("slump"))
    five = bars.get("5") or {}
    reg_hot = _better(rb_probability, _as_float(five.get("rb"))) and (games or 0) >= 1000
    return {
        "dai_no": unit.get("dai_no"),
        "machine_name": machine_name,
        "total_games": games,
        "bb_count": bb,
        "rb_count": rb,
        "synthetic_probability": synthetic,
        "rb_probability": rb_probability,
        "diff_medals": _diff_of(unit),
        "hot": reg_hot,
        "slump": history,
    }


def _pick_bars(members: list[dict[str, Any]], family_id: str) -> dict[str, Any]:
    ranked = sorted(members, key=lambda item: int(item.get("unit_count") or 0), reverse=True)
    for item in ranked:
        parsed = bars_from_criteria(item.get("criteria"))
        if parsed and (parsed.get("6") or {}).get("rb") is not None:
            return parsed
    fallback = _FAMILY_BARS.get(family_id) or _FAMILY_BARS["im-juggler"]
    return _copy_bars(fallback)


def _island_slump(units: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    """履歴の差枚が揃っていれば島の推移、無ければ台番号順の現在差枚。"""
    histories = [item["slump"] for item in units if len(item.get("slump") or []) >= 2]
    if histories and all(len(item) == len(histories[0]) for item in histories):
        points: list[dict[str, Any]] = []
        for index, sample in enumerate(histories[0]):
            diffs = [item[index].get("diff") for item in histories if item[index].get("diff") is not None]
            if not diffs:
                continue
            points.append({
                "label": sample.get("label"),
                "games": sample.get("games"),
                "diff": sum(diffs),
            })
        if len(points) >= 2:
            return points, "history"
    current = [
        {"dai_no": item.get("dai_no"), "games": item.get("total_games"), "diff": item.get("diff_medals")}
        for item in units
        if item.get("diff_medals") is not None
    ]
    return current, ("units" if current else "none")


def _island_from_members(hall: str, family: dict[str, str], members: list[dict[str, Any]]) -> dict[str, Any]:
    bars = _pick_bars(members, family["id"])
    units: list[dict[str, Any]] = []
    names: list[str] = []
    for member in members:
        name = str(member.get("machine_name") or family["label"])
        if name not in names:
            names.append(name)
        source_units = member.get("units")
        if isinstance(source_units, list) and source_units:
            for unit in source_units:
                if isinstance(unit, dict):
                    units.append(_unit_row(unit, machine_name=name, bars=bars))
        else:
            units.append(_unit_row(member, machine_name=name, bars=bars))
    units.sort(key=lambda item: _dai_sort_key(item.get("dai_no")))
    games_values = [item["total_games"] for item in units if item["total_games"] is not None]
    total_games = sum(games_values) if games_values else None
    bb_values = [item["bb_count"] for item in units if item["bb_count"] is not None]
    rb_values = [item["rb_count"] for item in units if item["rb_count"] is not None]
    bb = sum(bb_values) if bb_values else None
    rb = sum(rb_values) if rb_values else None
    hits = None if bb is None and rb is None else (bb or 0) + (rb or 0)
    unit_count = len(units)
    avg_games = round(total_games / unit_count, 1) if total_games is not None and unit_count else None
    synthetic = _ratio(total_games, hits if hits else None)
    rb_probability = _ratio(total_games, rb)
    diffs = [item["diff_medals"] for item in units if item["diff_medals"] is not None]
    diff_total = sum(diffs) if diffs else None
    slump, slump_kind = _island_slump(units)
    judged = score_island(
        unit_count=unit_count,
        avg_games=avg_games,
        synthetic=synthetic,
        rb_probability=rb_probability,
        bars=bars,
    )
    rate = str(family.get("rate") or "")
    return {
        "id": f"{hall}|{family['id']}|{rate}",
        "hall_name": hall,
        "family_id": family["id"],
        "family_label": family["label"],
        "rate": rate,
        "machine_names": names,
        "unit_count": unit_count,
        "total_games": total_games,
        "avg_games": avg_games,
        "bb_count": bb,
        "rb_count": rb,
        "synthetic_probability": synthetic,
        "rb_probability": rb_probability,
        "diff_medals": diff_total,
        "diff_label": _diff_label(diff_total),
        "bars": _copy_bars(bars),
        "slump": slump,
        "slump_kind": slump_kind,
        "units": units,
        **judged,
    }


def _overlay_results(machines: list[dict[str, Any]], results: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """台ごとの差枚と履歴を、集計済み機種の units に重ねる。"""
    if not results:
        return machines
    extras: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in results:
        if not isinstance(row, dict):
            continue
        hall = str(row.get("hall_name") or "")
        name = str(row.get("machine_name") or "")
        if classify_juggler_family(name, series=row.get("series")) is None:
            continue
        dai = str(row.get("dai_no") or "")
        if not dai:
            continue
        extras[(hall, name, dai)] = {
            "diff_medals": _diff_of(row),
            "history": row.get("history"),
        }
    prepared: list[dict[str, Any]] = []
    for machine in machines:
        if not isinstance(machine, dict):
            continue
        hall = str(machine.get("hall_name") or "")
        name = str(machine.get("machine_name") or "")
        if classify_juggler_family(name, series=machine.get("series")) is None:
            prepared.append(machine)
            continue
        units = machine.get("units")
        if not isinstance(units, list):
            prepared.append(machine)
            continue
        merged_units = []
        for unit in units:
            if not isinstance(unit, dict):
                continue
            copied = dict(unit)
            extra = extras.get((hall, name, str(unit.get("dai_no") or "")))
            if extra:
                if copied.get("diff_medals") is None:
                    copied["diff_medals"] = extra.get("diff_medals")
                if extra.get("history"):
                    copied["history"] = extra.get("history")
            merged_units.append(copied)
        copied_machine = dict(machine)
        copied_machine["units"] = merged_units
        prepared.append(copied_machine)
    return prepared


def build_juggler_islands(
    machines: list[dict[str, Any]] | None,
    results: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """ジャグラー島の一覧。スコアの高い全台系候補が先。"""
    source = _overlay_results(list(machines or []), results)
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    families: dict[tuple[str, str], dict[str, str]] = {}
    for machine in source:
        if machine.get("judgment_excluded") or machine.get("status") == "EXCLUDED":
            continue
        family = classify_juggler_family(machine.get("machine_name"), series=machine.get("series"))
        if family is None:
            continue
        hall = str(machine.get("hall_name") or "")
        rate = _rate_marker(machine.get("machine_name"))
        labeled = dict(family)
        labeled["rate"] = rate
        if rate:
            labeled["label"] = f"{family['label']}〔{rate}〕"
        grouped.setdefault((hall, family["id"], rate), []).append(machine)
        families[(family["id"], rate)] = labeled
    islands = [
        _island_from_members(hall, families[(family_id, rate)], members)
        for (hall, family_id, rate), members in grouped.items()
    ]
    islands.sort(
        key=lambda item: (
            _BADGE_RANK.get(str(item.get("badge")), 9),
            -int(item.get("score") or 0),
            -(item.get("avg_games") or 0),
            str(item.get("hall_name") or ""),
            str(item.get("family_label") or ""),
        )
    )
    return islands


def attach_juggler_view(payload: dict[str, Any]) -> dict[str, Any]:
    """判定ペイロードにジャグラー島の分析を足す。"""
    islands = build_juggler_islands(payload.get("machines") or [], payload.get("results") or [])
    all6 = sum(1 for item in islands if item.get("badge") == "all6")
    high56 = sum(1 for item in islands if item.get("badge") == "high56")
    high456 = sum(1 for item in islands if item.get("badge") == "high456")
    payload["juggler"] = {
        "island_count": len(islands),
        "hot_count": sum(1 for item in islands if item.get("hot")),
        "all6_count": all6,
        "high56_count": high56,
        "high456_count": high456,
        "zentai_count": all6,
        "high_count": high56,
        "islands": islands,
    }
    return payload
