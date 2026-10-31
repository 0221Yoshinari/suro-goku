"""ジャグラー島トップ4を、1枚のまとめ画像にする。"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from src.poster.card import (
    _CARD,
    _CARD_EDGE,
    _GOLD,
    _LINE,
    _MINT,
    _MUTED,
    _TEXT,
    _draw_right,
    _fit,
    _font,
    _pill,
    _width,
)
from src.poster.labels import format_date

WIDTH = 1200
HEIGHT = 1350
_PAD = 56
_ROW_H = 268


def _odds(value: Any) -> str:
    if value is None:
        return "-"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    if number <= 0:
        return "-"
    return f"1/{number:.1f}"


def render_juggler_pickup_card(islands: list[dict[str, Any]], *, date: str) -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), (8, 14, 26))
    draw = ImageDraw.Draw(image)
    regular = _font(False)
    bold = _font(True)
    left = _PAD
    right = WIDTH - _PAD

    draw.rounded_rectangle((_PAD - 8, 32, WIDTH - _PAD + 8, HEIGHT - 32), radius=28, fill=_CARD, outline=_CARD_EDGE, width=2)
    draw.rectangle((_PAD - 8, 56, _PAD + 2, HEIGHT - 56), fill=_GOLD)

    draw.text((left, 52), "スロ極", font=bold(32), fill=_GOLD)
    draw.text((left + 120, 62), "ジャグラーモード TOP4", font=regular(22), fill=_MUTED)
    _draw_right(draw, right, 58, format_date(date), regular(22), _MUTED)

    for index, island in enumerate(islands[:4], start=1):
        top = 128 + (index - 1) * _ROW_H
        _row(draw, index, island, left, right, top, regular, bold)
        if index < min(4, len(islands)):
            draw.line((left, top + _ROW_H - 18, right, top + _ROW_H - 18), fill=_LINE, width=2)

    footer = regular(17)
    draw.text((left, HEIGHT - 72), "店名・数値は判定データから。他店舗の情報は載せていません。", font=footer, fill=_MUTED)
    return image


def _row(
    draw: ImageDraw.ImageDraw,
    rank: int,
    island: dict[str, Any],
    left: int,
    right: int,
    top: int,
    regular,
    bold,
) -> None:
    hall = str(island.get("hall_name") or "-")
    family = str(island.get("family_label") or "ジャグラー")
    badge = str(island.get("badge_label") or "").strip() or "数値"
    units = int(island.get("unit_count") or 0)
    score = int(island.get("score") or 0)
    syn = _odds(island.get("synthetic_probability"))
    reg = _odds(island.get("rb_probability"))
    games = island.get("total_games")
    games_text = f"{int(games):,}G" if isinstance(games, (int, float)) and games else "-"

    rank_font = bold(36)
    draw.text((left, top + 4), f"{rank}", font=rank_font, fill=_GOLD)

    name_font = bold(38)
    name_x = left + 52
    draw.text((name_x, top), _fit(hall, name_font, right - name_x), font=name_font, fill=_TEXT)

    family_font = regular(24)
    draw.text((name_x, top + 50), _fit(family, family_font, right - name_x - 140), font=family_font, fill=_MUTED)

    badge_fill = (255, 214, 96) if badge == "全6" else (255, 160, 72)
    _pill(draw, right - 120, top + 6, badge, bold(20), badge_fill, (40, 24, 0))

    metrics_y = top + 98
    metrics = [
        ("台数", f"{units}台"),
        ("総回転", games_text),
        ("合算", syn),
        ("REG", reg),
        ("島スコア", str(score)),
    ]
    gap = (right - left) // len(metrics)
    for idx, (label, value) in enumerate(metrics):
        box_left = left + idx * gap
        draw.text((box_left, metrics_y), label, font=regular(18), fill=_MUTED)
        value_font = bold(30) if label in {"合算", "REG"} else bold(26)
        draw.text((box_left, metrics_y + 28), value, font=value_font, fill=_MINT if label in {"合算", "REG"} else _TEXT)


def save_juggler_pickup_card(islands: list[dict[str, Any]], dest: Path, *, date: str) -> None:
    image = render_juggler_pickup_card(islands, date=date)
    dest.parent.mkdir(parents=True, exist_ok=True)
    temporary = dest.with_suffix(dest.suffix + ".tmp")
    image.save(temporary, format="PNG", optimize=True)
    temporary.replace(dest)


def card_png_bytes(islands: list[dict[str, Any]], *, date: str) -> bytes:
    buffer = BytesIO()
    render_juggler_pickup_card(islands, date=date).save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
