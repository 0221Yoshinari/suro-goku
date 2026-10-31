"""ジャグラーモードと同じ基準で、エリア横断の島ランキングを作る。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")

from src.analyzer.juggler import _island_sort_key, build_juggler_islands
from src.poster.chase import AREAS
from src.poster.select import PROJECT_ROOT, judgment_date
from src.scraper.halls import load_halls

DEFAULT_JUDGMENTS = PROJECT_ROOT / "data" / "judgments" / "halls"
MIN_SCORE_WEAK = 45


def _allowed_halls() -> set[str]:
    return {hall.name for hall in load_halls(include_disabled=True) if hall.prefecture in AREAS}


def rank_juggler_islands(
    judgments_dir: Path | None = None,
    *,
    limit: int = 4,
    min_score: int = MIN_SCORE_WEAK,
) -> list[dict[str, Any]]:
    """大阪・東京・福岡の latest 判定から、スコア順に島を返す。"""
    root = judgments_dir or DEFAULT_JUDGMENTS
    allowed = _allowed_halls()
    islands: list[dict[str, Any]] = []
    for folder in sorted(root.iterdir()):
        if not folder.is_dir():
            continue
        path = folder / "latest.json"
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            continue
        hall = str(payload.get("hall_name") or "")
        if hall not in allowed:
            continue
        judged = judgment_date(str(payload.get("judged_at") or "") or None)
        for island in build_juggler_islands(payload.get("machines") or [], payload.get("results")):
            badge = str(island.get("badge") or "none")
            score = int(island.get("score") or 0)
            if badge == "none" and score < min_score:
                continue
            row = dict(island)
            row["judged_on"] = judged
            islands.append(row)
    islands.sort(key=_island_sort_key)
    return islands[: max(limit, 0)]


def _odds_line(value: Any, prefix: str) -> str:
    if value is None:
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number <= 0:
        return ""
    return f"{prefix}1/{number:.1f}"


def compose_juggler_pickup_post(
    islands: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    slot: str = "",
) -> str:
    """X向け。4島を1投稿に。数値は合算とREGだけ、締めは時刻に合わせた1文。"""
    lines = [
        "ジャグラーモードで、ツール上反応しているところをピックアップしてます↓",
        "",
    ]
    for index, island in enumerate(islands[:4], start=1):
        hall = str(island.get("hall_name") or "")
        family = str(island.get("family_label") or "ジャグラー")
        badge = str(island.get("badge_label") or "").strip()
        syn = _odds_display(island.get("synthetic_probability"))
        reg = _odds_display(island.get("rb_probability"))
        lines.append(f"{index}. {hall}")
        lines.append(_juggler_facts(family, badge, syn, reg))
        lines.append("")
    if islands:
        lines.append(pick_juggler_closing(now=now, slot=slot))
    return "\n".join(lines).strip()


def pick_juggler_closing(*, now: datetime | None = None, slot: str = "") -> str:
    """運用の宣言。読者への命令口調や内部用語は使わない。"""
    clock = (now or datetime.now(tz=JST)).astimezone(JST)
    hour = clock.hour
    base = "良さそうなところ見つかれば、随時ピックアップしてポストしていきます。"
    if slot in {"evening", "night"} or hour >= 17 or hour < 2:
        pool = (
            base,
            "良さそうなところ見つかれば、随時ピックアップしてポストしていきます。夜ジャグラー前の参考にも。",
        )
    elif slot in {"pickup", "morning"}:
        pool = (
            base,
            "良さそうなところ見つかれば、随時ピックアップしてポストしていきます。店選びの参考にも。",
        )
    else:
        pool = (base,)
    seed = f"{clock.date().isoformat()}-{hour:02d}-{slot or 'auto'}"
    return _soft_choice(seed, pool)


def _odds_display(value: Any) -> str:
    if value is None:
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number <= 0:
        return ""
    return f"1/{number:.1f}"


def _juggler_facts(family: str, badge: str, syn: str, reg: str) -> str:
    if badge:
        lead = f"{family}　{badge}。"
    else:
        lead = f"{family}。"
    nums: list[str] = []
    if syn:
        nums.append(f"合算{syn}")
    if reg:
        nums.append(f"REG{reg}")
    if nums:
        lead += "　".join(nums) + "。"
    return lead


def _soft_choice(name: str, pool: tuple[str, ...]) -> str:
    total = 0
    for index, char in enumerate(name):
        total = (total * 33 + ord(char) + index) % 997
    return pool[total % len(pool)]


def compose_juggler_pick(island: dict[str, Any], *, index: int, total: int) -> str:
    hall = str(island.get("hall_name") or "")
    family = str(island.get("family_label") or "ジャグラー")
    badge = str(island.get("badge_label") or "").strip()
    syn = _odds_display(island.get("synthetic_probability"))
    reg = _odds_display(island.get("rb_probability"))
    lines = [f"ジャグラーモード　ピックアップ（{index}/{total}）", "", f"{index}. {hall}"]
    lines.append(_juggler_facts(family, badge, syn, reg))
    return "\n".join(lines)
