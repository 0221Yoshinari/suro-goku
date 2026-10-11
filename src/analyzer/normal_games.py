"""総回転数から ART/AT 消化ゲームを実数で除き、通常時G数を求める。

平均値や「1回あたり◯G」は使わない。当り履歴に記録された消化Gだけを足す。
消化Gが履歴に無い店舗では、差し引き不能として通常時Gを確定しない。
"""

from __future__ import annotations

import re
from typing import Any

_ART_LABELS = {
    "art",
    "at",
    "st",
    "st_at",
    "at/art",
    "art/at",
    "アート",
    "ａｒｔ",
    "ａｔ",
}
_NON_ART_LABELS = {
    "bb",
    "rb",
    "big",
    "reg",
    "cz",
    "bonus",
    "sb",
    "cb",
    "ct",
    "mb",
}
_KIND_KEYS = ("signal", "kind", "bonus_kind", "bonus_type", "type", "name", "label", "atari")
_DAILY_ART_GAME_KEYS = (
    "art_games",
    "at_games",
    "art_game",
    "at_game",
    "art_game_count",
    "at_game_count",
    "bonus_3_game",
    "bonus_3_games",
    "bounus_3_game",
    "bounus_3_games",
    "bonus3_game",
    "bonus3_games",
)
_SIGNAL_GAME_KEYS = (
    "games",
    "game",
    "game_count",
    "games_count",
    "consumed_games",
    "digest_games",
    "play_games",
    "bonus_games",
    "bonus_game",
    "bonus_game_count",
    "span_games",
    "span",
    *_DAILY_ART_GAME_KEYS,
)
_START_KEYS = ("start_game", "bonus_start", "begin_game", "from_game", "in_game", "start")
_END_KEYS = ("end_game", "bonus_end", "finish_game", "to_game", "out_game", "end")
_HIT_LIST_KEYS = ("hits", "hit_history", "bonus_history", "atari_history", "bonus_list", "signals", "atari")


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    text = str(value).replace(",", "").replace("，", "").strip()
    if not text or text in {"--", "-", "―"}:
        return None
    match = re.search(r"-?\d+", text)
    return int(match.group(0)) if match else None


def _day_text(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    match = re.search(r"\d{4}-\d{2}-\d{2}", text)
    return match.group(0) if match else None


def _label_is_art(value: Any) -> bool | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    if value in (3, "3"):
        return True
    if value in (1, 2, "1", "2"):
        return False
    text = str(value).strip().lower()
    if not text:
        return None
    if text in _ART_LABELS or "art" in text or text in {"at", "st"}:
        return True
    if text in _NON_ART_LABELS:
        return False
    return None


def _is_art_signal(record: dict[str, Any]) -> bool | None:
    seen = False
    for key in _KIND_KEYS:
        if key not in record:
            continue
        seen = True
        flag = _label_is_art(record.get(key))
        if flag is True:
            return True
        if flag is False:
            return False
    if not seen:
        return None
    return None


def _game_point(record: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        if key not in record:
            continue
        value = record.get(key)
        if isinstance(value, str) and ":" in value:
            continue
        number = _as_int(value)
        if number is not None and number >= 0:
            return number
    return None


def _signal_games(record: dict[str, Any]) -> int | None:
    for key in _SIGNAL_GAME_KEYS:
        if key not in record:
            continue
        games = _as_int(record.get(key))
        if games is not None and games >= 0:
            return games
    start = _game_point(record, _START_KEYS)
    end = _game_point(record, _END_KEYS)
    if start is None or end is None or end < start:
        return None
    return end - start


def _looks_like_hit(record: dict[str, Any]) -> bool:
    if _is_art_signal(record) is not None:
        return True
    if any(key in record for key in _START_KEYS) and any(key in record for key in _END_KEYS):
        return True
    if any(key in record for key in ("games", "game", "game_count", "consumed_games", "bonus_games")):
        return True
    return False


def _snapshot_day(rows: list[dict[str, Any]]) -> str | None:
    days = [day for row in rows if (day := _day_text(row.get("day") or row.get("date")))]
    return max(days) if days else None


def _on_snapshot_day(record: dict[str, Any], snapshot_day: str | None) -> bool:
    day = _day_text(record.get("day") or record.get("date"))
    if snapshot_day is None or day is None:
        return True
    return day == snapshot_day


def _child_hits(record: dict[str, Any]) -> list[dict[str, Any]]:
    parent_day = record.get("day") or record.get("date")
    found: list[dict[str, Any]] = []
    for key in _HIT_LIST_KEYS:
        rows = record.get(key)
        if not isinstance(rows, list):
            continue
        for item in rows:
            if not isinstance(item, dict):
                continue
            child = dict(item)
            if parent_day and not (child.get("day") or child.get("date")):
                child["day"] = parent_day
            found.append(child)
    return found


def _flatten(history: Any, hits: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for source in (history, hits):
        if not isinstance(source, list):
            continue
        for item in source:
            if isinstance(item, dict):
                rows.append(item)
    return rows


def art_consumed_games(
    history: Any = None,
    hits: Any = None,
    *,
    art_count: int | None = None,
    art_games: int | None = None,
) -> int | None:
    """当日分の ART/AT 消化Gを実数合算する。不明なら None。ART が無ければ 0。"""
    rows = _flatten(history, hits)
    snapshot_day = _snapshot_day(rows)
    today = [row for row in rows if _on_snapshot_day(row, snapshot_day)]

    signals: list[dict[str, Any]] = []
    for row in today:
        signals.extend(hit for hit in _child_hits(row) if _on_snapshot_day(hit, snapshot_day))
        if _looks_like_hit(row):
            signals.append(row)

    art_signals = [row for row in signals if _is_art_signal(row) is True]
    if art_signals:
        games: list[int] = []
        for row in art_signals:
            consumed = _signal_games(row)
            if consumed is None:
                break
            games.append(consumed)
        else:
            if art_count is None or len(games) == art_count:
                return sum(games)

    daily_games: list[int] = []
    if art_games is not None and art_games >= 0:
        daily_games.append(art_games)
    for row in today:
        if _looks_like_hit(row) and _is_art_signal(row) is not False:
            continue
        for key in _DAILY_ART_GAME_KEYS:
            if key not in row:
                continue
            consumed = _as_int(row.get(key))
            if consumed is not None and consumed >= 0:
                daily_games.append(consumed)
                break
    if daily_games:
        return sum(daily_games)

    if art_count is None or art_count <= 0:
        return 0
    return None


def resolve_real_normal_games(
    total_games: int | None,
    *,
    includes_art_in_total_games: bool,
    art_count: int | None = None,
    history: Any = None,
    hits: Any = None,
    art_games: int | None = None,
) -> dict[str, Any]:
    """通常時G。総回転に ART が含まれない店はそのまま。含まれる店は実消化Gを引く。"""
    reported = total_games if total_games is None or total_games >= 0 else None
    if not includes_art_in_total_games:
        return {
            "includes_art_in_total_games": False,
            "reported_total_games": reported,
            "art_consumed_games": 0,
            "real_normal_games": reported,
            "normal_games_exact": reported is not None,
        }
    consumed = art_consumed_games(history, hits, art_count=art_count, art_games=art_games)
    if reported is None or consumed is None or consumed > reported:
        return {
            "includes_art_in_total_games": True,
            "reported_total_games": reported,
            "art_consumed_games": consumed,
            "real_normal_games": None,
            "normal_games_exact": False,
        }
    return {
        "includes_art_in_total_games": True,
        "reported_total_games": reported,
        "art_consumed_games": consumed,
        "real_normal_games": reported - consumed,
        "normal_games_exact": True,
    }
