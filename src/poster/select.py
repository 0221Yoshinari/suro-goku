"""その日のホールを、スロ極の画面に出ている判定だけで順位を付ける。

投稿に使うのは、サンプルが厚い全6 / 全56 / 全456と、末尾ごとの平均推測設定。
平均ゲームが浅い島と、台数が少なすぎる島は出さない。
周年・特定日・合同・取材は、厚いデータがある店を前に出す。イベントだけでは投稿しない。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from src.analyzer.juggler import classify_juggler_family
from src.poster.labels import display_machine, display_name, norm_hall

JST = ZoneInfo("Asia/Tokyo")
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_HALLS_DIR = PROJECT_ROOT / "data" / "judgments" / "halls"

# 1ファイルがこれより大きい結合JSONは読まない。店舗別ファイルを使う。
MAX_JUDGMENT_BYTES = 48 * 1024 * 1024

# これ未満の設置台数は、全台系としても末尾としても投稿しない。
MIN_UNIT_COUNT = 5
# 機種として載せる最低ライン。朝イチの数回転や、2台だけの島は外す。
MIN_FEATURED_UNITS = 4
MIN_AVG_GAMES = 1000
# 末尾カードの「厚い」ライン。画面の全56数値（平均推測設定 4.75 以上）に揃える。
MIN_DIGIT_AVG = 4.75
MIN_DIGIT_UNITS = 8
MIN_DIGIT_GAMES = 8000

TIER_SCORE = {
    "anniversary": 3000,
    "strong": 2800,
    "specific": 2700,
    "joint": 2600,
    "cover": 200,
}
TIER_RANK = {name: index for index, name in enumerate(TIER_SCORE)}
S_TIERS = {"anniversary", "strong", "specific", "joint"}

REASON_INSUFFICIENT = "insufficient_data"
REASON_OTHER_DAY = "other_day"
REASON_UNREADABLE = "unreadable"

EXCLUSION_LABELS = {
    REASON_INSUFFICIENT: "データ不足",
    REASON_OTHER_DAY: "対象日以外",
    REASON_UNREADABLE: "読み取り失敗",
}


class JudgmentSourceError(RuntimeError):
    """指定した判定ファイルを投稿の入力にできない。"""


@dataclass(frozen=True)
class MachineCard:
    name: str
    dom_name: str
    status_label: str
    unit_count: int
    total_games: int
    avg_games: float | None
    synthetic_probability: float | None
    judgment_type: str
    is_juggler: bool
    is_all_high: bool
    is_high_56: bool
    is_high_base: bool


@dataclass(frozen=True)
class DigitCard:
    digit: int
    avg_setting: float
    unit_count: int
    total_games: int | None
    avg_games: float | None
    status_label: str


@dataclass(frozen=True)
class HallPick:
    hall_name: str
    pattern: str
    rank: str
    score: int
    unit_count: int
    total_games: int
    clock_label: str
    event_tier: str | None
    event_titles: tuple[str, ...]
    has_all_tai: bool
    juggler_hot: bool
    tags: tuple[str, ...]
    machines: tuple[MachineCard, ...]
    digits: tuple[DigitCard, ...]


@dataclass(frozen=True)
class Selection:
    date: str
    pick: HallPick | None
    runners_up: tuple[HallPick, ...]
    excluded_counts: dict[str, int]
    considered: int


def load_judgment_halls(
    path: Path,
    *,
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """店舗別の判定JSONを、機種単位の薄い辞書にして返す。

    戻り値は (ホール一覧, 読めなかったファイル数)。
    台ごとの履歴は末尾の平均推測設定を出すためだけに使い、保持しない。
    """
    files = _judgment_files(path)
    halls: list[dict[str, Any]] = []
    unreadable = 0
    total = len(files)
    for index, file_path in enumerate(files, start=1):
        try:
            if file_path.stat().st_size > MAX_JUDGMENT_BYTES:
                raise JudgmentSourceError(
                    f"{file_path} は大きすぎます。店舗別の {DEFAULT_HALLS_DIR} を指定してください。"
                )
            payload = json.loads(file_path.read_text(encoding="utf-8"))
        except JudgmentSourceError:
            raise
        except (OSError, json.JSONDecodeError, UnicodeError):
            unreadable += 1
        else:
            if isinstance(payload, dict):
                halls.extend(slim_payload(payload))
            else:
                unreadable += 1
        if on_progress is not None:
            on_progress(index, total)
    return _dedupe_halls(halls), unreadable


def slim_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """結合JSONならホールごとに分け、units を落とした辞書にする。"""
    machines = [item for item in (payload.get("machines") or []) if isinstance(item, dict)]
    grouped: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for machine in machines:
        name = str(machine.get("hall_name") or payload.get("hall_name") or "").strip()
        if name not in grouped:
            grouped[name] = []
            order.append(name)
        grouped[name].append(machine)
    if not order:
        name = str(payload.get("hall_name") or "").strip()
        order = [name]
        grouped = {name: []}
    single = len(order) == 1
    slim: list[dict[str, Any]] = []
    for name in order:
        rows = [_slim_machine(machine, name) for machine in grouped[name]]
        digits = _digit_rows(name, grouped[name], payload if single else {})
        unit_count = sum(int(row["unit_count"]) for row in rows)
        if single:
            reported = as_int(payload.get("unit_count"))
            if reported is not None:
                unit_count = reported
        slim.append(
            {
                "hall_name": name,
                "judged_at": payload.get("judged_at"),
                "unit_count": unit_count,
                "machines": rows,
                "digits": digits,
            }
        )
    return slim


def select_top(
    halls: list[dict[str, Any]],
    events: list[dict],
    *,
    date: str,
    unreadable: int = 0,
    theme: str = "auto",
) -> Selection:
    """画面に出る全台系か末尾判定がある店だけを採点し、最上位を1件返す。"""
    counts = {
        REASON_INSUFFICIENT: 0,
        REASON_OTHER_DAY: 0,
        REASON_UNREADABLE: unreadable,
    }
    prepared = _dedupe_halls([row for hall in halls for row in slim_payload(hall)])
    event_index = _event_index(events)
    candidates: list[HallPick] = []
    for hall in prepared:
        reason, pick = _classify_hall(
            hall,
            event_index.get(norm_hall(hall.get("hall_name")), []),
            date,
            theme=theme,
        )
        if pick is None:
            counts[reason] = counts.get(reason, 0) + 1
            continue
        candidates.append(pick)
    candidates.sort(key=lambda item: (-item.score, item.hall_name))
    pick = candidates[0] if candidates else None
    return Selection(
        date=date,
        pick=pick,
        runners_up=tuple(candidates[1:4]),
        excluded_counts=counts,
        considered=len(prepared),
    )


def as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value:
            return None
        return int(value)
    if isinstance(value, str):
        text = value.strip().replace(",", "").replace("+", "").replace("枚", "")
        text = text.replace("−", "-").replace("－", "-")
        if not text:
            return None
        try:
            return int(float(text))
        except ValueError:
            return None
    return None


def as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number != number:
            return None
        return number
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def judgment_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(JST).date().isoformat()


def _classify_hall(
    hall: dict[str, Any],
    events: list[dict],
    date: str,
    *,
    theme: str = "auto",
) -> tuple[str, HallPick | None]:
    name = display_name(hall.get("hall_name"))
    if not name:
        return REASON_INSUFFICIENT, None
    judged_on = judgment_date(str(hall.get("judged_at") or "") or None)
    if judged_on is None:
        return REASON_INSUFFICIENT, None
    if judged_on != date:
        return REASON_OTHER_DAY, None

    machines = [row for row in (hall.get("machines") or []) if isinstance(row, dict) and row.get("machine_name")]
    unit_count = as_int(hall.get("unit_count"))
    if unit_count is None:
        unit_count = sum(as_int(row.get("unit_count")) or 0 for row in machines)
    if unit_count < MIN_UNIT_COUNT:
        return REASON_INSUFFICIENT, None

    featured = [_machine_card(row) for row in machines if _featured(row) and _sample_ready(row)]
    featured.sort(key=_machine_sort_key)
    hot_digits = _hot_digit_cards(hall.get("digits"))
    hall_score = _hall_theme_score(featured)
    digit_score = _digit_theme_score(hot_digits)
    pattern = _choose_pattern(theme, featured, hot_digits, hall_score, digit_score)
    if pattern is None:
        return REASON_INSUFFICIENT, None

    total_games = sum(_machine_games(row) for row in machines)
    tier, titles = _best_event(events)
    all_high = sum(1 for row in featured if row.is_all_high)
    high_56 = sum(1 for row in featured if row.is_high_56)
    high_base = sum(1 for row in featured if row.is_high_base)
    juggler_count = sum(1 for row in featured if row.is_juggler)
    theme_score = hall_score if pattern == "hall" else digit_score
    score = TIER_SCORE.get(tier or "", 0) + theme_score
    return "ok", HallPick(
        hall_name=name,
        pattern=pattern,
        rank="S" if tier in S_TIERS else "A",
        score=score,
        unit_count=unit_count,
        total_games=total_games,
        clock_label=_clock_label(hall.get("judged_at")),
        event_tier=tier,
        event_titles=tuple(titles),
        has_all_tai=bool(featured),
        juggler_hot=juggler_count > 0,
        tags=_tags(tier, all_high, high_56, high_base, juggler_count, hot_digits[:1]),
        machines=tuple(featured[:3]),
        digits=tuple(hot_digits[:3]),
    )


def _choose_pattern(
    theme: str,
    featured: list[MachineCard],
    hot_digits: list[DigitCard],
    hall_score: int,
    digit_score: int,
) -> str | None:
    if theme == "hall":
        return "hall" if featured else None
    if theme == "digits":
        return "digits" if hot_digits else None
    if featured and hall_score >= digit_score:
        return "hall"
    if hot_digits:
        return "digits"
    if featured:
        return "hall"
    return None


def _hall_theme_score(featured: list[MachineCard]) -> int:
    all_high = sum(1 for row in featured if row.is_all_high)
    high_56 = sum(1 for row in featured if row.is_high_56)
    high_base = sum(1 for row in featured if row.is_high_base)
    return min(all_high, 4) * 220 + min(high_56, 4) * 140 + min(high_base, 4) * 80


def _digit_theme_score(digits: list[DigitCard]) -> int:
    if not digits:
        return 0
    top = digits[0]
    score = int(top.avg_setting * 120) + min(top.unit_count, 20) * 4
    if top.avg_setting >= 5.5:
        score += 80
    return score


def _machine_card(machine: dict[str, Any]) -> MachineCard:
    raw_name = str(machine.get("machine_name") or "")
    family = classify_juggler_family(raw_name, series=machine.get("series"))
    return MachineCard(
        name=display_machine(raw_name),
        dom_name=display_name(raw_name),
        status_label=_hot_label(machine),
        unit_count=as_int(machine.get("unit_count")) or 0,
        total_games=_machine_games(machine),
        avg_games=as_float(machine.get("avg_games")),
        synthetic_probability=as_float(machine.get("synthetic_probability")),
        judgment_type=str(machine.get("judgment_type") or ""),
        is_juggler=family is not None,
        is_all_high=bool(machine.get("is_all_high") or machine.get("status") == "ALL_HIGH"),
        is_high_56=bool(machine.get("is_high_56") or machine.get("status") == "HIGH_56"),
        is_high_base=bool(machine.get("is_high_base") or machine.get("status") == "HIGH_BASE"),
    )


def _hot_label(machine: dict[str, Any]) -> str:
    if machine.get("is_all_high") or machine.get("status") == "ALL_HIGH":
        return "全6数値"
    if machine.get("is_high_56") or machine.get("status") == "HIGH_56":
        return "全56数値"
    if machine.get("is_high_base") or machine.get("status") == "HIGH_BASE":
        return "全456数値"
    return str(machine.get("status_label") or "")


def _sample_ready(machine: dict[str, Any]) -> bool:
    """平均が浅いか、台数が少ない島は、判定が強くても載せない。"""
    units = as_int(machine.get("unit_count")) or 0
    if units < MIN_FEATURED_UNITS:
        return False
    average = as_float(machine.get("avg_games"))
    if average is None and units:
        games = _machine_games(machine)
        average = games / units if games else None
    if average is None or average < MIN_AVG_GAMES:
        return False
    return True


def _featured(machine: dict[str, Any]) -> bool:
    if _excluded(machine):
        return False
    return bool(
        machine.get("is_all_high")
        or machine.get("is_high_56")
        or machine.get("is_high_base")
        or machine.get("status") in {"ALL_HIGH", "HIGH_56", "HIGH_BASE"}
    )


def _machine_sort_key(machine: MachineCard) -> tuple:
    heat = 3
    if machine.is_all_high:
        heat = 0
    elif machine.is_high_56:
        heat = 1
    elif machine.is_high_base:
        heat = 2
    return (heat, -machine.unit_count, -(machine.avg_games or 0), machine.name)


def _hot_digit_cards(rows: Any) -> list[DigitCard]:
    found: list[DigitCard] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        digit = as_int(row.get("digit"))
        avg = as_float(row.get("avg_setting"))
        units = as_int(row.get("unit_count")) or 0
        games = as_int(row.get("total_games"))
        average = as_float(row.get("avg_games"))
        if games is None and average is not None and units:
            games = int(average * units)
        if digit is None or avg is None or avg < MIN_DIGIT_AVG or units < MIN_DIGIT_UNITS:
            continue
        if games is None or games < MIN_DIGIT_GAMES:
            continue
        if average is not None and average < MIN_AVG_GAMES:
            continue
        found.append(
            DigitCard(
                digit=digit,
                avg_setting=avg,
                unit_count=units,
                total_games=as_int(row.get("total_games")),
                avg_games=as_float(row.get("avg_games")),
                status_label=str(row.get("status_label") or ""),
            )
        )
    found.sort(key=lambda item: (-item.avg_setting, item.digit))
    return found


def _clock_label(value: Any) -> str:
    hour = _judgment_hour(value)
    if hour is None:
        return ""
    return f"{hour}時台"


def _judgment_hour(value: Any) -> int | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(JST).hour


def _best_event(events: list[dict]) -> tuple[str | None, list[str]]:
    ranked = [(tier, event) for event in events if (tier := _event_tier(event)) is not None]
    if not ranked:
        return None, []
    best = min((tier for tier, _event in ranked), key=lambda item: TIER_RANK[item])
    titles: list[str] = []
    for tier, event in ranked:
        if tier != best:
            continue
        title = display_name(event.get("event_title"))
        if title and title not in titles:
            titles.append(title)
    return best, titles[:2]


def _event_tier(event: dict) -> str | None:
    title = display_name(event.get("event_title"))
    event_type = str(event.get("event_type") or "")
    blob = f"{event_type} {title}"
    if "周年" in blob or event_type == "周年":
        return "anniversary"
    if any(word in blob for word in ("万枚", "グランドオープン", "リニューアル")):
        return "strong"
    if event_type == "特定日" or "特定日" in title or "のつく日" in title or "がつく日" in title:
        return "specific"
    if event_type == "合同" or "合同" in title:
        return "joint"
    if event_type == "取材" and title:
        return "cover"
    return None


def _tags(
    tier: str | None,
    all_high: int,
    high_56: int,
    high_base: int,
    juggler_count: int,
    digits: list[DigitCard],
) -> tuple[str, ...]:
    labels = {
        "anniversary": "周年",
        "strong": "強イベ",
        "specific": "特定日",
        "joint": "合同",
        "cover": "取材",
    }
    tags: list[str] = []
    if tier in labels:
        tags.append(labels[tier])
    if all_high:
        tags.append(f"全6×{all_high}")
    if high_56:
        tags.append(f"全56×{high_56}")
    if high_base:
        tags.append(f"全456×{high_base}")
    if juggler_count:
        tags.append(f"ジャグラー×{juggler_count}")
    if digits:
        tags.append(f"末尾{digits[0].digit}")
    return tuple(tags)


def _event_index(events: list[dict]) -> dict[str, list[dict]]:
    index: dict[str, list[dict]] = {}
    for event in events:
        names = event.get("hall_names") or []
        if event.get("hall_name"):
            names = [*names, event.get("hall_name")]
        for name in names:
            key = norm_hall(str(name))
            if not key:
                continue
            bucket = index.setdefault(key, [])
            if event not in bucket:
                bucket.append(event)
    return index


def _excluded(machine: dict[str, Any]) -> bool:
    return bool(machine.get("judgment_excluded") or machine.get("status") == "EXCLUDED")


def _machine_games(machine: dict[str, Any]) -> int:
    total = as_int(machine.get("total_games"))
    if total is not None and total > 0:
        return total
    average = as_float(machine.get("avg_games"))
    units = as_int(machine.get("unit_count")) or 0
    if average and units:
        return int(average * units)
    return 0


def _slim_machine(machine: dict[str, Any], hall_name: str) -> dict[str, Any]:
    return {
        "hall_name": machine.get("hall_name") or hall_name,
        "machine_name": machine.get("machine_name"),
        "series": machine.get("series"),
        "unit_count": as_int(machine.get("unit_count")) or 0,
        "total_games": machine.get("total_games") if machine.get("total_games") is not None else machine.get("real_normal_games"),
        "avg_games": machine.get("avg_games"),
        "synthetic_probability": machine.get("synthetic_probability"),
        "judgment_type": machine.get("judgment_type"),
        "judgment_type": machine.get("judgment_type"),
        "status": machine.get("status"),
        "status_label": machine.get("status_label"),
        "is_all_high": bool(machine.get("is_all_high")),
        "is_high_56": bool(machine.get("is_high_56")),
        "is_high_base": bool(machine.get("is_high_base")),
        "judgment_excluded": _excluded(machine),
    }


def _digit_rows(hall_name: str, machines: list[dict[str, Any]], payload: dict[str, Any]) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    for machine in machines:
        for unit in machine.get("units") or []:
            if not isinstance(unit, dict):
                continue
            row = unit if unit.get("hall_name") else {**unit, "hall_name": hall_name}
            units.append(row)
    if units:
        try:
            from src.analyzer.extras import hall_digit_summaries

            boards = hall_digit_summaries(units)
        except (TypeError, ValueError, KeyError, ZeroDivisionError):
            boards = []
        if boards and isinstance(boards[0], dict):
            return [_public_digit(row) for row in boards[0].get("digits") or [] if isinstance(row, dict)]
    explicit = payload.get("digits")
    if isinstance(explicit, list):
        return [_public_digit(row) for row in explicit if isinstance(row, dict)]
    return []


def _public_digit(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "digit": row.get("digit"),
        "avg_setting": row.get("avg_setting"),
        "unit_count": row.get("unit_count"),
        "total_games": row.get("total_games"),
        "avg_games": row.get("avg_games"),
        "status": row.get("status"),
        "status_label": row.get("status_label"),
    }


def _judgment_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if not path.is_dir():
        raise JudgmentSourceError(f"判定データが見つかりません: {path}")
    files = sorted(path.glob("*/latest.json"))
    if not files:
        files = sorted(path.glob("latest.json"))
    return files


def _dedupe_halls(halls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    anonymous: list[dict[str, Any]] = []
    for hall in halls:
        key = norm_hall(hall.get("hall_name"))
        if not key:
            anonymous.append(hall)
            continue
        current = best.get(key)
        if current is None:
            best[key] = hall
            order.append(key)
            continue
        if str(hall.get("judged_at") or "") >= str(current.get("judged_at") or ""):
            best[key] = hall
    return [best[key] for key in order] + anonymous
