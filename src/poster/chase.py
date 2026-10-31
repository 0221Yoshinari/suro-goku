"""福岡、大阪、東京で、イベントの加点が高い店を追いかける。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src.poster.labels import display_name, norm_hall
from src.poster.select import HallPick, _classify_hall, as_float, as_int, judgment_date

AREAS = ("fukuoka", "osaka", "tokyo")
PER_AREA = 2
JST = ZoneInfo("Asia/Tokyo")
# 定時の開始分と、遅れて起動しても受け付ける終了分（終了は含まない）。
_SCHEDULE = (
    ("morning", 10 * 60, 11 * 60 + 15),
    ("pickup", 11 * 60 + 50, 12 * 60 + 40),
    ("midday", 14 * 60, 15 * 60 + 15),
    ("afternoon", 17 * 60, 18 * 60 + 15),
    ("evening", 19 * 60, 20 * 60 + 15),
    ("night", 21 * 60, 22 * 60 + 15),
)

POINT_ANNIVERSARY = 100
POINT_STRONG = 60
POINT_SPECIFIC = 50
POINT_OTHER = 20

_OSAKA_STRONG = (
    "エイムスター",
    "黒バラ超極",
    "黒バラ極意",
    "スロパチステーション青",
    "フラッグコンボ",
)
_FUKUOKA_STRONG = (
    "スロパチ演者",
    "スロパチ取材宴",
    "爆撮",
    "スロパチ取材",
    "アルティメットサザンスター",
)
_TOKYO_STRONG = (
    "スロパチステーション",
    "アツ姫",
    "でちゃう",
    "来店",
    "演者",
    "取材玉",
)
_HOT_STATUS = {"ALL_HIGH", "HIGH_56", "HIGH_BASE"}


@dataclass(frozen=True)
class ChaseTarget:
    hall_name: str
    prefecture: str
    points: int
    event_titles: tuple[str, ...]
    avg_games: float
    stability: float
    same_day: HallPick | None


def chase_slot(hour: int) -> str:
    """テスト用の粗い区分。本番は scheduled_slot の定時だけ通す。"""
    if hour < 12:
        return "morning"
    if hour < 16:
        return "midday"
    return "evening"


def now_jst() -> datetime:
    """日本時間。UTC の壁時計をそのまま朝昼夜の判定に使わない。"""
    current = datetime.now(JST)
    if current.utcoffset() != timedelta(hours=9):
        raise RuntimeError("日本時間を取得できません")
    return current


def scheduled_slot(clock: datetime) -> str | None:
    """定時の中なら morning / midday / evening。夜中や隙間の時間は None。"""
    if clock.tzinfo is None:
        raise ValueError("タイムゾーンのない時刻ではスロットを決められません")
    current = clock.astimezone(JST)
    if current.utcoffset() != timedelta(hours=9):
        raise RuntimeError("日本時間を取得できません")
    minute = current.hour * 60 + current.minute
    for name, start, end in _SCHEDULE:
        if start <= minute < end:
            return name
    return None


def offhours_reason(requested: str, date: str, clock: datetime) -> str:
    """定時の外、または対象日が日本時間の当日でないときは停止理由を返す。"""
    current = clock.astimezone(JST)
    today = current.date().isoformat()
    if date != today:
        return f"対象日 {date} は日本時間の当日 {today} と違います。投稿せず停止しました。"
    opened = scheduled_slot(current)
    label = current.strftime("%Y-%m-%d %H:%M")
    if requested == "auto":
        if opened is None:
            return f"日本時間 {label} は定時の外です。投稿せず停止しました。"
        return ""
    if opened != requested:
        return f"日本時間 {label} は {requested} の定時ではありません。投稿せず停止しました。"
    return ""


def attach_data_image(slot: str, *, same_day: bool) -> bool:
    """朝は数値画像を付けない。それ以外の定時は、当日の反応があるときだけ付ける。"""
    return slot != "morning" and same_day


def event_points(event: dict, prefecture: str) -> int:
    """1件のイベントを、周年・特定日・強い取材の加点にする。"""
    title = display_name(event.get("event_title"))
    event_type = str(event.get("event_type") or "")
    blob = f"{event_type} {title}"
    points = 0
    if "周年" in blob or event_type == "周年":
        points += POINT_ANNIVERSARY
    if _is_strong(blob, prefecture):
        points += POINT_STRONG
    if event_type == "特定日" or "特定日" in title or "つく日" in title:
        points += POINT_SPECIFIC
    if points == 0 and (event_type or title):
        points += POINT_OTHER
    return points


def select_chase(
    halls: list[dict],
    events: list[dict],
    *,
    date: str,
    areas: tuple[str, ...] = AREAS,
    per_area: int = PER_AREA,
    catalog: dict[str, tuple[str, str]] | None = None,
) -> list[ChaseTarget]:
    """エリアごとに加点の上位を返す。同点は稼働と、末尾・全台系・並びの安定度で決める。"""
    if catalog is None:
        catalog = load_area_catalog(areas)
    snapshots = _snapshots(halls, catalog)
    scored: dict[str, dict] = {}
    for event in events:
        names = [str(name) for name in (event.get("hall_names") or []) if str(name).strip()]
        seen: set[str] = set()
        for name in names:
            found = catalog.get(norm_hall(name))
            if found is None or found[0] not in areas:
                continue
            prefecture, canonical = found
            key = norm_hall(canonical)
            if key in seen:
                continue
            seen.add(key)
            bucket = scored.setdefault(
                key,
                {
                    "hall_name": canonical,
                    "prefecture": prefecture,
                    "points": 0,
                    "titles": [],
                },
            )
            bucket["points"] += event_points(event, prefecture)
            title = display_name(event.get("event_title"))
            if title and title not in bucket["titles"]:
                bucket["titles"].append(title)
    ranked: list[ChaseTarget] = []
    for prefecture in areas:
        rows = [row for row in scored.values() if row["prefecture"] == prefecture and row["points"] > 0]
        rows.sort(key=lambda row: _sort_key(row, snapshots))
        for row in rows[:per_area]:
            key = norm_hall(row["hall_name"])
            snapshot = snapshots.get(key)
            average, stability = _track_record(snapshot) if snapshot else (0.0, 0.0)
            ranked.append(
                ChaseTarget(
                    hall_name=row["hall_name"],
                    prefecture=prefecture,
                    points=row["points"],
                    event_titles=tuple(row["titles"][:3]),
                    avg_games=average,
                    stability=stability,
                    same_day=_same_day(snapshot, events, date),
                )
            )
    return ranked


def load_area_catalog(areas: tuple[str, ...]) -> dict[str, tuple[str, str]]:
    """店名と別名を、県と正式名称へ引く。"""
    from src.scraper.halls import load_halls

    catalog: dict[str, tuple[str, str]] = {}
    for hall in load_halls(include_disabled=True):
        if hall.prefecture not in areas:
            continue
        canonical = display_name(hall.name)
        for label in (hall.name, *hall.aliases):
            key = norm_hall(str(label or ""))
            if key:
                catalog[key] = (hall.prefecture, canonical)
    return catalog


def _is_strong(blob: str, prefecture: str) -> bool:
    if "でちゃう" in blob and "虹" in blob:
        return True
    markers = {
        "osaka": _OSAKA_STRONG,
        "fukuoka": _FUKUOKA_STRONG,
        "tokyo": _TOKYO_STRONG,
    }.get(prefecture, ())
    return any(marker in blob for marker in markers)


def _snapshots(halls: list[dict], catalog: dict[str, tuple[str, str]]) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for hall in halls:
        if not isinstance(hall, dict):
            continue
        matched = catalog.get(norm_hall(hall.get("hall_name")))
        if matched is None:
            continue
        key = norm_hall(matched[1])
        previous = found.get(key)
        if previous is None or str(hall.get("judged_at") or "") > str(previous.get("judged_at") or ""):
            found[key] = hall
    return found


def _sort_key(row: dict, snapshots: dict[str, dict]) -> tuple:
    snapshot = snapshots.get(norm_hall(row["hall_name"]))
    average, stability = _track_record(snapshot) if snapshot else (0.0, 0.0)
    return (-row["points"], -average, -stability, row["hall_name"])


def _same_day(snapshot: dict | None, events: list[dict], date: str) -> HallPick | None:
    if snapshot is None or judgment_date(str(snapshot.get("judged_at") or "") or None) != date:
        return None
    names = {norm_hall(snapshot.get("hall_name"))}
    related = [
        event
        for event in events
        if any(norm_hall(str(name)) in names for name in (event.get("hall_names") or []))
    ]
    _reason, pick = _classify_hall(snapshot, related, date, theme="auto")
    return pick


def _track_record(hall: dict) -> tuple[float, float]:
    """直近スナップショットの平均ゲームと、全台系・末尾・並びの出方。"""
    units = 0
    games = 0
    ready = 0
    hot_machines = 0
    for machine in hall.get("machines") or []:
        if not isinstance(machine, dict):
            continue
        count = as_int(machine.get("unit_count")) or 0
        played = as_int(machine.get("total_games")) or 0
        units += count
        games += played
        average = as_float(machine.get("avg_games"))
        if average is None and count and played:
            average = played / count
        if count >= 4 and average is not None and average >= 1000:
            ready += 1
            if machine.get("is_all_high") or machine.get("is_high_56") or machine.get("is_high_base"):
                hot_machines += 1
            elif machine.get("status") in _HOT_STATUS:
                hot_machines += 1
    average_games = games / units if units else 0.0
    machine_rate = hot_machines / ready if ready else 0.0
    hot_digits: set[int] = set()
    known = 0
    for row in hall.get("digits") or []:
        if not isinstance(row, dict):
            continue
        digit = as_int(row.get("digit"))
        if digit is None:
            continue
        total = as_int(row.get("total_games")) or 0
        if total < 8000:
            continue
        known += 1
        setting = as_float(row.get("avg_setting")) or 0.0
        label = str(row.get("status_label") or "")
        if setting >= 4.75 or label.startswith("全"):
            hot_digits.add(digit)
    digit_rate = len(hot_digits) / known if known else 0.0
    run = 0
    best = 0
    for digit in range(10):
        if digit in hot_digits:
            run += 1
            best = max(best, run)
        else:
            run = 0
    lineup = min(best, 4) / 4
    stability = machine_rate * 0.45 + digit_rate * 0.35 + lineup * 0.20
    return average_games, stability
