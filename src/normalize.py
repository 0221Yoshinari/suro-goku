"""機種名の表記ゆれ正規化。テラモバの型式サフィックスや半角カナも吸収する。"""

from __future__ import annotations

import re
import unicodedata

_WIDTH_TABLE = str.maketrans(
    "０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ"
    "ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ"
    "ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ",
    "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "123456789X",
)

_PREFIX_RE = re.compile(r"^[lsp](?=[\u3040-\u30ff\u4e00-\u9fff]|u[\u3040-\u30ff])")
_NOISE_RE = re.compile(r"パチスロ|スマスロ")
_TYPE_SUFFIX_RE = re.compile(r"(?:[a-z]{2}\d{0,2}|(?<=\d)[a-z]|[a-z]\d{1,2})$")
_SERIES_V_RE = re.compile(r"(?<=[\u3040-\u30ff\u4e00-\u9fff])v$")
_NON_NAME_RE = re.compile(r"[!！?？'’\"“”\[\]【】]")


def normalize_machine_name(value: str | None) -> str:
    """判定・監視照合用の機種名キー。

    全角英数・半角カナ・L/S接頭辞・テラモバ型式（CX, PA1, 5C 等）を落とし、
    既存マスターの標準名と同じキーになるようにする。
    """
    if not value:
        return ""
    text = str(value).replace("ＶⅢ", "V3").replace("VⅢ", "V3")
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_WIDTH_TABLE)
    text = (
        text.replace(" ", "")
        .replace("　", "")
        .replace("・", "")
        .replace("･", "")
        .replace("！", "!")
        .replace("（", "")
        .replace("）", "")
        .replace("(", "")
        .replace(")", "")
        .replace("：", "")
        .replace(":", "")
        .replace("－", "")
        .replace("—", "")
        .replace("−", "")
        .replace("-", "")
    )
    text = _NON_NAME_RE.sub("", text)
    text = text.casefold()
    text = text.replace("iii", "3").replace("ii", "2")
    text = _NOISE_RE.sub("", text)
    text = _PREFIX_RE.sub("", text)
    text = _PREFIX_RE.sub("", text)
    if text.startswith("uミラクル"):
        text = "ウルトラ" + text[1:]
    prev = None
    while prev != text:
        prev = text
        nxt = _TYPE_SUFFIX_RE.sub("", text)
        if len(nxt) < 3:
            break
        text = nxt
    text = _SERIES_V_RE.sub("5", text)
    return text


def norm_name(value: str | None) -> str:
    return normalize_machine_name(value)
