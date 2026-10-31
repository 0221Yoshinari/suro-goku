"""投稿文面と画像で共通する表示用の整形。"""

from __future__ import annotations

import re
import unicodedata

# サイト側の型式記号。L東京喰種 や 46L東京喰種 の先頭だけ外し、機種名は残す。
_MODEL_PREFIX = re.compile(r"^(?:\d{1,3})?[LSPlsp](?=[\u3040-\u30ff\u4e00-\u9fff])")
# 末尾の型式コード。モンキーターン5CE の CE、東京喰種CT の CT。バージョン番号は残す。
_TRAILING_CODE = re.compile(r"(?:[A-Za-z]{2,}\d{0,2}|(?<=\d)[A-Za-z]|[A-Za-z]\d{1,2})$")


def display_name(value: str | None) -> str:
    """半角カナなどを、タイムラインで読める表記に揃える。"""
    return unicodedata.normalize("NFKC", str(value or "")).strip()


def display_machine(value: str | None) -> str:
    text = display_name(value)
    stripped = _MODEL_PREFIX.sub("", text, count=1)
    while True:
        shorter = _TRAILING_CODE.sub("", stripped)
        if len(shorter) < 2 or shorter == stripped:
            break
        stripped = shorter
    return stripped.strip() or text


def norm_hall(value: str | None) -> str:
    """ホール名の照合用。空白と大文字小文字の差は無視する。"""
    return "".join(display_name(value).split()).casefold()


def format_medals(value: int) -> str:
    sign = "+" if value > 0 else ""
    return f"{sign}{value:,}枚"


def format_games(value: float | None) -> str:
    if value is None:
        return ""
    return f"{round(value):,}G"


def format_date(iso_date: str) -> str:
    year, month, day = iso_date.split("-")
    return f"{int(year)}.{int(month):02d}.{int(day):02d}"


def format_date_long(iso_date: str) -> str:
    month, day = iso_date.split("-")[1:]
    return f"{int(month)}月{int(day)}日"
