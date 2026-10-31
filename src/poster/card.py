"""Xのタイムライン向けまとめ画像。"""

from __future__ import annotations

import os
import unicodedata
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from src.poster.labels import format_date, format_games
from src.poster.select import HallPick, MachineCard

WIDTH = 1200
HEIGHT = 675

_BG = (8, 14, 26)
_CARD = (15, 24, 42)
_CARD_EDGE = (46, 64, 96)
_ROW = (24, 36, 60)
_GOLD = (232, 196, 114)
_TEXT = (244, 247, 252)
_MUTED = (154, 168, 190)
_MINT = (72, 214, 164)
_LINE = (42, 58, 84)
_INK = (28, 20, 6)
_BLUE = (47, 111, 255)

_FONT_PATHS: dict[bool, Path | None] = {}


def render_summary(pick: HallPick, *, date: str) -> Image.Image:
    """選ばれた1ホールのまとめを 1200x675 のRGB画像にする。"""
    image = Image.new("RGB", (WIDTH, HEIGHT), _BG)
    draw = ImageDraw.Draw(image)
    regular = _font(False)
    bold = _font(True)

    draw.rounded_rectangle((28, 24, WIDTH - 28, HEIGHT - 24), radius=28, fill=_CARD, outline=_CARD_EDGE, width=2)
    draw.rectangle((28, 48, 38, HEIGHT - 48), fill=_GOLD)

    left = 64
    right = WIDTH - 64
    brand = bold(30)
    draw.text((left, 48), "スロ極", font=brand, fill=_GOLD)
    draw.text((left + _width(brand, "スロ極") + 18, 56), "本日の注目", font=regular(18), fill=_MUTED)
    _draw_right(draw, right, 54, format_date(date), regular(20), _MUTED)

    label, fill, ink = _badge(pick)
    _pill(draw, left, 104, label, bold(18), fill, ink)

    title = bold(40)
    draw.text((left, 162), _fit(pick.hall_name, title, right - left), font=title, fill=_TEXT)

    subtitle = _subtitle(pick)
    draw.text((left, 222), _fit(subtitle, regular(20), right - left), font=regular(20), fill=_GOLD if pick.event_titles else _MUTED)
    draw.line((left, 264, right, 264), fill=_LINE, width=2)

    machines = list(pick.machines[:3])
    row_height = 92
    top = 284
    for index, machine in enumerate(machines):
        _machine_row(
            draw,
            machine,
            left,
            right,
            top + index * (row_height + 12),
            row_height,
            regular,
            bold,
        )

    footer = regular(16)
    draw.text((left, 612), "スロ極の判定画面に出ている数値だけを掲載", font=footer, fill=_MUTED)
    _draw_right(draw, right, 612, "SURO-GOKU", footer, _GOLD)
    return image


def _badge(pick: HallPick) -> tuple[str, tuple[int, int, int], tuple[int, int, int]]:
    if pick.rank == "S":
        names = {"anniversary": "S   周年", "strong": "S   強イベ", "specific": "S   特定日", "joint": "S   合同"}
        return names.get(pick.event_tier or "", "S"), _GOLD, _INK
    if pick.rank == "A":
        label = "A   ジャグラー" if pick.juggler_hot and not pick.has_all_tai else "A   全台系"
        return label, _BLUE, (255, 255, 255)
    return "注目", (36, 50, 74), (220, 228, 238)


def _subtitle(pick: HallPick) -> str:
    if pick.event_titles:
        return "  /  ".join(pick.event_titles)
    if pick.has_all_tai:
        return "全台系の反応が出ている"
    if pick.juggler_hot:
        return "ジャグラーの全台系が出ている"
    if pick.pattern == "digits" and pick.digits:
        top = pick.digits[0]
        return f"末尾{top.digit}の平均推測設定 {top.avg_setting:.2f}"
    return "全台系の反応が出ている"


def _machine_row(
    draw: ImageDraw.ImageDraw,
    machine: MachineCard,
    left: int,
    right: int,
    top: int,
    height: int,
    regular,
    bold,
) -> None:
    draw.rounded_rectangle((left, top, right, top + height), radius=16, fill=_ROW)
    label = machine.status_label or "全台系"
    value_font = bold(28)
    value_width = _width(value_font, label)
    name_width = right - left - value_width - 48
    name_font = bold(26)
    draw.text((left + 22, top + 16), _fit(machine.name, name_font, name_width), font=name_font, fill=_TEXT)
    _draw_right(draw, right - 22, top + 16, label, value_font, _MINT)

    meta = _meta(machine)
    meta_font = regular(16)
    draw.text((left + 22, top + 56), _fit(meta, meta_font, right - left - 44), font=meta_font, fill=_MUTED)


def _meta(machine: MachineCard) -> str:
    parts: list[str] = []
    if machine.status_label:
        parts.append(machine.status_label)
    games = format_games(machine.avg_games)
    if games:
        parts.append(f"平均 {games}")
    if machine.unit_count:
        parts.append(f"{machine.unit_count}台")
    return "   ·   ".join(parts)


def _pill(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    text: str,
    font: ImageFont.ImageFont,
    fill: tuple[int, int, int],
    ink: tuple[int, int, int],
) -> None:
    width = _width(font, text) + 32
    draw.rounded_rectangle((x, y, x + width, y + 36), radius=18, fill=fill)
    draw.text((x + 16, y + 7), text, font=font, fill=ink)


def _draw_right(
    draw: ImageDraw.ImageDraw,
    right: int,
    y: int,
    text: str,
    font: ImageFont.ImageFont,
    fill: tuple[int, int, int],
) -> None:
    draw.text((right - _width(font, text), y), text, font=font, fill=fill)


def _fit(text: str, font: ImageFont.ImageFont, max_width: float) -> str:
    if _width(font, text) <= max_width:
        return text
    trimmed = text
    while trimmed and _width(font, trimmed + "…") > max_width:
        trimmed = trimmed[:-1]
    return trimmed + "…"


def _width(font: ImageFont.ImageFont, text: str) -> float:
    getlength = getattr(font, "getlength", None)
    if getlength is not None:
        return float(getlength(text))
    return float(len(text) * 16)


def _font(bold: bool):
    def loader(size: int) -> ImageFont.ImageFont:
        path = _font_file(bold)
        if path is None:
            return _default_font(size)
        try:
            return ImageFont.truetype(str(path), size=size)
        except OSError:
            return _default_font(size)

    return loader


def _default_font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def _font_file(bold: bool) -> Path | None:
    if bold in _FONT_PATHS:
        return _FONT_PATHS[bold]
    found = _discover_font(bold) or _discover_font(not bold)
    _FONT_PATHS[bold] = found
    return found


def _discover_font(bold: bool) -> Path | None:
    configured = os.environ.get("SURO_POST_FONT", "").strip()
    if configured and Path(configured).is_file():
        return Path(configured)
    weight = "W6" if bold else "W3"
    root = Path("/System/Library/Fonts")
    if root.is_dir():
        matches = []
        for path in root.iterdir():
            name = unicodedata.normalize("NFC", path.name)
            if "角ゴシック" in name and weight in name and path.suffix.lower() in {".ttc", ".ttf", ".otf"}:
                matches.append(path)
        if matches:
            return sorted(matches)[0]
    regular_cjk = Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")
    candidates = [
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc") if bold else regular_cjk,
        regular_cjk,
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf"),
        Path("/usr/share/fonts/truetype/fonts-japanese-gothic.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
    ]
    for path in candidates:
        if path.is_file():
            return path
    return None
