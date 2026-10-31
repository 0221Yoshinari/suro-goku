"""その日のイベント店を朝に並べ、あとから全台系・末尾・並びの一番見栄えがいい店を1件出す。"""

from __future__ import annotations

from src.poster.chase import AREAS, ChaseTarget, load_area_catalog
from src.poster.labels import display_name, norm_hall
from src.poster.select import (
    DigitCard,
    HallPick,
    MachineCard,
    _clock_label,
    as_float,
    as_int,
    judgment_date,
)
from src.scraper.halls import PREFECTURE_LABELS

_ALL_PREFECTURES = tuple(PREFECTURE_LABELS)
# X 投稿で数値を探す対象。朝の宣言もこの3エリアに揃える。
POSTER_AREAS = AREAS
_AREA_LABELS = {
    "osaka": "大阪府 全域",
    "tokyo": "東京都 全域",
    "fukuoka": "福岡県 全域",
}

# 強い末尾（従来どおり全56ライン付近）。ゲーム数下限は設けない（analyzer が出した値を使う）。
MIN_DIGIT_AVG_STRONG = 4.75
MIN_DIGIT_UNITS_STRONG = 6
# 弱い末尾は相対比較用。3台未満は末尾合算として薄すぎるので外す。
MIN_DIGIT_AVG_SOFT = 3.0
MIN_DIGIT_UNITS_SOFT = 3
MIN_DIGIT_SPREAD_SOFT = 0.05
SOFT_DIGIT_TAG = "soft_digit"


def area_targets(events: list[dict], date: str) -> list[ChaseTarget]:
    """大阪・東京・福岡の登録店舗をすべて対象にする。イベント名はその店に付いていれば載せる。"""
    catalog = load_area_catalog(POSTER_AREAS)
    titles_by_hall = _event_titles_by_hall(events, date, catalog)
    found: dict[str, ChaseTarget] = {}
    for _key, (prefecture, canonical) in catalog.items():
        canon_key = norm_hall(canonical)
        if not canon_key or canon_key in found:
            continue
        found[canon_key] = ChaseTarget(
            canonical,
            prefecture,
            0,
            tuple(titles_by_hall.get(canon_key, ())),
            0.0,
            0.0,
            None,
        )
    rows = list(found.values())
    rows.sort(key=lambda item: (item.prefecture, item.hall_name))
    return rows


def watch_targets(events: list[dict], date: str) -> list[ChaseTarget]:
    """その日のイベント店を、県の上限なしで全部返す。数値は見ない。"""
    catalog = load_area_catalog(_ALL_PREFECTURES)
    found: dict[str, dict] = {}
    for event in events:
        if str(event.get("date") or date) != date:
            continue
        title = display_name(event.get("event_title"))
        for raw in event.get("hall_names") or []:
            name = display_name(raw)
            key = norm_hall(name)
            if not key:
                continue
            known = catalog.get(key)
            prefecture = known[0] if known else ""
            canonical = known[1] if known else name
            bucket = found.setdefault(
                norm_hall(canonical),
                {"hall_name": canonical, "prefecture": prefecture, "titles": []},
            )
            if title and title not in bucket["titles"]:
                bucket["titles"].append(title)
    rows = [
        ChaseTarget(
            item["hall_name"],
            item["prefecture"],
            0,
            tuple(item["titles"]),
            0.0,
            0.0,
            None,
        )
        for item in found.values()
        if item["titles"] or item["hall_name"]
    ]
    rows.sort(key=lambda item: (item.prefecture, item.hall_name))
    return rows


def _event_titles_by_hall(
    events: list[dict],
    date: str,
    catalog: dict[str, tuple[str, str]],
) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for event in events:
        if str(event.get("date") or date) != date:
            continue
        title = display_name(event.get("event_title"))
        if not title:
            continue
        for raw in event.get("hall_names") or []:
            name = display_name(raw)
            key = norm_hall(name)
            if not key:
                continue
            known = catalog.get(key)
            canonical = known[1] if known else name
            canon_key = norm_hall(canonical)
            if not canon_key:
                continue
            bucket = found.setdefault(canon_key, [])
            if title not in bucket:
                bucket.append(title)
    return found


def poster_area_labels() -> list[tuple[str, str]]:
    """朝の宣言用。prefecture id と表示ラベル。"""
    return [(area, _AREA_LABELS[area]) for area in POSTER_AREAS if area in _AREA_LABELS]


def best_material(halls: list[dict], targets: list[ChaseTarget], date: str) -> HallPick | None:
    """強いシグナルが無ければ、末尾の相対差だけでも1件返す。"""
    strong = best_signal(halls, targets, date)
    if strong is not None:
        return strong
    return best_soft_digit(halls, targets, date)


def best_signal(halls: list[dict], targets: list[ChaseTarget], date: str) -> HallPick | None:
    """朝に並べた店のうち、当日の全台系・末尾・並びで一番見栄えがいい1件。無ければ None。"""
    by_name = {
        norm_hall(display_name(hall.get("hall_name"))): hall
        for hall in halls
        if norm_hall(display_name(hall.get("hall_name")))
    }
    best: HallPick | None = None
    best_score = -1
    for target in targets:
        hall = by_name.get(norm_hall(target.hall_name))
        if hall is None:
            continue
        if judgment_date(str(hall.get("judged_at") or "") or None) != date:
            continue
        pick, score = _score_hall(hall, target)
        if pick is not None and score > best_score:
            best = pick
            best_score = score
    return best


def best_soft_digit(halls: list[dict], targets: list[ChaseTarget], date: str) -> HallPick | None:
    """イベント店のうち、末尾0〜9の平均推測設定の差が一番大きい店を1件。"""
    by_name = {
        norm_hall(display_name(hall.get("hall_name"))): hall
        for hall in halls
        if norm_hall(display_name(hall.get("hall_name")))
    }
    best: HallPick | None = None
    best_score = -1.0
    for target in targets:
        hall = by_name.get(norm_hall(target.hall_name))
        if hall is None:
            continue
        if judgment_date(str(hall.get("judged_at") or "") or None) != date:
            continue
        pick, score = _soft_digit_pick(hall, target)
        if pick is not None and score > best_score:
            best = pick
            best_score = score
    return best


def _score_hall(hall: dict, target: ChaseTarget) -> tuple[HallPick | None, int]:
    clock = _clock_label(hall.get("judged_at"))
    best_pick: HallPick | None = None
    best_score = -1
    for machine in hall.get("machines") or []:
        if not isinstance(machine, dict) or _excluded(machine):
            continue
        card = _machine_card(machine)
        if card is None:
            continue
        score = _machine_score(machine, card)
        if score > best_score:
            best_score = score
            best_pick = _pick(target, clock, "hall", score, machines=(card,))
        block = _lineup(machine)
        if block is None:
            continue
        lineup_score = 160 + int(block["length"]) * 5
        if lineup_score > best_score:
            best_score = lineup_score
            labeled = MachineCard(
                card.name,
                card.dom_name,
                str(block["label"]),
                int(block["length"]),
                card.total_games,
                card.avg_games,
                None,
                card.judgment_type,
                card.is_juggler,
                False,
                False,
                False,
            )
            best_pick = _pick(target, clock, "hall", lineup_score, machines=(labeled,))
    for row in _digit_rows(hall):
        digit = _digit_card_strong(row)
        if digit is None:
            continue
        score = 200 + int(digit.avg_setting * 10) + digit.unit_count
        if score > best_score:
            best_score = score
            best_pick = _pick(target, clock, "digits", score, digits=(digit,), units=digit.unit_count)
    if best_pick is None:
        return None, -1
    return best_pick, best_score


def _pick(
    target: ChaseTarget,
    clock: str,
    pattern: str,
    score: int,
    *,
    tags: tuple[str, ...] = (),
    machines: tuple[MachineCard, ...] = (),
    digits: tuple[DigitCard, ...] = (),
    units: int = 0,
) -> HallPick:
    count = units or (machines[0].unit_count if machines else 0)
    games = machines[0].total_games if machines else (digits[0].total_games or 0 if digits else 0)
    hot = any(item.is_all_high or item.is_high_56 or item.is_high_base for item in machines)
    juggler = any(item.is_juggler or "ジャグラー" in item.name for item in machines)
    return HallPick(
        target.hall_name,
        pattern,
        "S",
        score,
        count,
        games,
        clock,
        None,
        target.event_titles,
        hot,
        juggler,
        tags,
        machines,
        digits,
    )


def _soft_digit_pick(hall: dict, target: ChaseTarget) -> tuple[HallPick | None, float]:
    clock = _clock_label(hall.get("judged_at"))
    candidates: list[DigitCard] = []
    for row in _digit_rows(hall):
        card = _digit_card_soft(row)
        if card is not None:
            candidates.append(card)
    if len(candidates) < 2:
        return None, -1.0
    ranked = sorted(candidates, key=lambda item: (-item.avg_setting, -item.unit_count, item.digit))
    top, second = ranked[0], ranked[1]
    spread = top.avg_setting - second.avg_setting
    if top.avg_setting < MIN_DIGIT_AVG_SOFT and spread < MIN_DIGIT_SPREAD_SOFT:
        return None, -1.0
    if spread < MIN_DIGIT_SPREAD_SOFT and top.avg_setting < MIN_DIGIT_AVG_SOFT + 0.2:
        return None, -1.0
    score = spread * 120.0 + top.avg_setting * 15.0 + top.unit_count
    pick_score = int(score)
    pick = _pick(
        target,
        clock,
        "digits",
        pick_score,
        tags=(SOFT_DIGIT_TAG,),
        digits=(top, second),
        units=top.unit_count,
    )
    return pick, score


def _machine_card(machine: dict) -> MachineCard | None:
    units = as_int(machine.get("unit_count")) or 0
    if units < 4:
        return None
    total = as_int(machine.get("total_games")) or 0
    average = as_float(machine.get("avg_games"))
    if average is None and units and total:
        average = total / units
    hot = bool(
        machine.get("is_all_high")
        or machine.get("is_high_56")
        or machine.get("is_high_base")
        or machine.get("status") in {"ALL_HIGH", "HIGH_56", "HIGH_BASE"}
    )
    if not hot:
        return None
    if (average or 0) < 200 and total < 800:
        return None
    name = display_name(machine.get("name"))
    return MachineCard(
        name,
        str(machine.get("dom_name") or machine.get("name") or ""),
        str(machine.get("status_label") or ""),
        units,
        total,
        average,
        as_float(machine.get("synthetic_probability")),
        str(machine.get("judgment_type") or ""),
        bool(machine.get("is_juggler") or "ジャグラー" in name),
        bool(machine.get("is_all_high") or machine.get("status") == "ALL_HIGH"),
        bool(machine.get("is_high_56") or machine.get("status") == "HIGH_56"),
        bool(machine.get("is_high_base") or machine.get("status") == "HIGH_BASE"),
    )


def _machine_score(machine: dict, card: MachineCard) -> int:
    if card.is_all_high or machine.get("status") == "ALL_HIGH":
        base = 300
    elif card.is_high_56 or machine.get("status") == "HIGH_56":
        base = 240
    else:
        base = 180
    return base + card.unit_count


def _lineup(machine: dict) -> dict | None:
    blocks = machine.get("lineup_blocks")
    if not isinstance(blocks, list):
        return None
    best: dict | None = None
    for block in blocks:
        if not isinstance(block, dict):
            continue
        length = as_int(block.get("length")) or 0
        if length < 3:
            continue
        if best is None or length > int(best["length"]):
            start = block.get("start_dai")
            best = {"length": length, "label": f"{start}号機から{length}台の並び"}
    return best


def _digit_rows(hall: dict) -> list[dict]:
    rows: list[dict] = []
    own = hall.get("digits")
    if isinstance(own, list):
        rows.extend(item for item in own if isinstance(item, dict))
    for machine in hall.get("machines") or []:
        if not isinstance(machine, dict):
            continue
        nested = machine.get("digit_judgments")
        if isinstance(nested, list):
            rows.extend(item for item in nested if isinstance(item, dict))
    return rows


def _digit_card_strong(row: dict) -> DigitCard | None:
    card = _digit_card_observed(row)
    if card is None:
        return None
    if card.avg_setting < MIN_DIGIT_AVG_STRONG or card.unit_count < MIN_DIGIT_UNITS_STRONG:
        return None
    return card


def _digit_card_soft(row: dict) -> DigitCard | None:
    card = _digit_card_observed(row)
    if card is None:
        return None
    if card.unit_count < MIN_DIGIT_UNITS_SOFT:
        return None
    return card


def _digit_card_observed(row: dict) -> DigitCard | None:
    """analyzer が付けた平均推測設定があれば、ゲーム数下限なしで使う。"""
    digit = as_int(row.get("digit"))
    average = as_float(row.get("avg_setting"))
    units = as_int(row.get("unit_count")) or 0
    if digit is None or average is None or units <= 0:
        return None
    rated = as_int(row.get("rated_count"))
    if rated is not None and rated <= 0:
        return None
    games = as_int(row.get("total_games"))
    played = as_float(row.get("avg_games"))
    if games is None and played is not None and units:
        games = int(played * units)
    return DigitCard(
        digit,
        average,
        units,
        games,
        played,
        str(row.get("status_label") or ""),
    )


def _excluded(machine: dict) -> bool:
    return bool(machine.get("judgment_excluded") or machine.get("status") == "EXCLUDED")
