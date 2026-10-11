"""OCRテキスト列から、BB / RB / 合成確率などの数値フィールドを拾う。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

# EasyOCR の1件: (bbox, text, confidence)
OcrItem = tuple[list[list[float]], str, float]

NUMBER_RE = re.compile(r"(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)")
RATIO_RE = re.compile(r"1\s*/\s*(\d+(?:\.\d+)?)")
FW_TRANS = str.maketrans("ＢＲ０１２３４５６７８９", "BR0123456789")

# より具体的なラベルを先に判定する
LABEL_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("bb_probability", ("BB確率", "B確率", "昭確率", "88確率")),
    ("rb_probability", ("RB確率",)),
    ("synthetic_probability", ("合成確率", "合成確子", "合成確")),
    ("bb_count", ("BB",)),
    ("rb_count", ("RB",)),
    ("total_games", ("総ゲーム", "回転数", "ゲーム数", "G数", "総回転")),
    ("first_hit_count", ("初当たり", "初当り", "初当", "初当たり回数", "初当回数")),
)

PROBABILITY_FIELDS = {"bb_probability", "rb_probability", "synthetic_probability"}

ValueKind = Literal["int", "ratio"]


@dataclass
class ParsedFields:
    machine_name: str | None
    bb_count: int | None
    rb_count: int | None
    bb_probability: float | None
    rb_probability: float | None
    synthetic_probability: float | None
    total_games: int | None
    first_hit_count: int | None
    extra_numbers: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "machine_name": self.machine_name,
            "bb_count": self.bb_count,
            "rb_count": self.rb_count,
            "bb_probability": self.bb_probability,
            "rb_probability": self.rb_probability,
            "synthetic_probability": self.synthetic_probability,
            "total_games": self.total_games,
            "first_hit_count": self.first_hit_count,
            "extra_numbers": self.extra_numbers,
        }


def _to_int(raw: str) -> int | None:
    cleaned = raw.replace(",", "").replace("，", "")
    if not cleaned.isdigit():
        return None
    return int(cleaned)


def _to_float(raw: str) -> float | None:
    cleaned = raw.replace(",", "").replace("，", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _center_y(bbox: list[list[float]]) -> float:
    ys = [float(p[1]) for p in bbox]
    return (min(ys) + max(ys)) / 2


def _center_x(bbox: list[list[float]]) -> float:
    xs = [float(p[0]) for p in bbox]
    return (min(xs) + max(xs)) / 2


def _norm(text: str) -> str:
    return text.replace(" ", "").replace("　", "").translate(FW_TRANS)


def _match_label(text: str) -> str | None:
    n = _norm(text)
    if not n:
        return None
    if "合成" in n and "確" in n:
        return "synthetic_probability"
    if "RB" in n and "確" in n:
        return "rb_probability"
    if "確" in n and any(token in n for token in ("BB", "B確", "昭", "88")):
        return "bb_probability"
    if n in {"RB"}:
        return "rb_count"
    if n in {"BB", "B"}:
        return "bb_count"
    for field, keywords in LABEL_RULES:
        for kw in keywords:
            if kw in n:
                return field
    return None


def _parse_ratio(text: str) -> float | None:
    match = RATIO_RE.search(_norm(text))
    if not match:
        return None
    return _to_float(match.group(1))


def _parse_count(text: str) -> int | None:
    if _parse_ratio(text) is not None:
        return None
    nums = NUMBER_RE.findall(text)
    if not nums:
        return None
    return _to_int(nums[0])


def _extract_value(text: str, kind: ValueKind) -> int | float | None:
    if kind == "ratio":
        ratio = _parse_ratio(text)
        if ratio is not None:
            return ratio
        return None
    return _parse_count(text)


def _in_column(label: dict[str, Any], other: dict[str, Any], column_width: float) -> bool:
    dx = other["cx"] - label["cx"]
    dy = other["cy"] - label["cy"]
    if dy < -8:
        return False
    if dx < -50:
        return False
    if dx > column_width:
        return False
    if dy > 140:
        return False
    return True


def _pick_value(
    label: dict[str, Any],
    rows: list[dict[str, Any]],
    kind: ValueKind,
    column_width: float = 430.0,
) -> int | float | None:
    inline = _extract_value(label["text"], kind)
    if inline is not None:
        return inline

    candidates: list[tuple[float, int | float]] = []
    for other in rows:
        if other is label:
            continue
        if not _in_column(label, other, column_width):
            continue
        value = _extract_value(other["text"], kind)
        if value is None:
            continue
        dy = other["cy"] - label["cy"]
        dx = abs(other["cx"] - label["cx"])
        candidates.append((dy * 2 + dx * 0.25, value))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def parse_ocr_items(items: list[OcrItem], row_tolerance_px: float = 28.0) -> ParsedFields:
    """ラベルと同じ列・直下にある数字 / 1/xxx.x をフィールドに割り当てる。"""
    del row_tolerance_px  # 列方向の探索に置き換え
    labeled: dict[str, int | float] = {}
    extra: list[dict[str, Any]] = []

    rows = [
        {
            "text": text.strip(),
            "conf": float(conf),
            "cy": _center_y(bbox),
            "cx": _center_x(bbox),
        }
        for bbox, text, conf in items
        if text and text.strip()
    ]

    for row in rows:
        field = _match_label(row["text"])
        if not field or field in labeled:
            continue
        kind: ValueKind = "ratio" if field in PROBABILITY_FIELDS else "int"
        value = _pick_value(row, rows, kind)
        if value is not None:
            labeled[field] = value

    bb_count = labeled.get("bb_count")
    rb_count = labeled.get("rb_count")
    first_hit = labeled.get("first_hit_count")
    if first_hit is None and isinstance(bb_count, int) and isinstance(rb_count, int):
        first_hit = bb_count + rb_count

    synthetic = labeled.get("synthetic_probability")
    total_games = labeled.get("total_games")
    if total_games is None and isinstance(synthetic, (int, float)) and isinstance(first_hit, int) and first_hit > 0:
        total_games = int(round(float(synthetic) * first_hit))

    for row in rows:
        ratio = _parse_ratio(row["text"])
        if ratio is not None:
            extra.append(
                {
                    "raw": row["text"],
                    "value": ratio,
                    "kind": "ratio",
                    "confidence": round(float(row["conf"]), 4),
                }
            )
            continue
        parsed = _parse_count(row["text"])
        if parsed is None:
            continue
        extra.append(
            {
                "raw": row["text"],
                "value": parsed,
                "kind": "int",
                "confidence": round(float(row["conf"]), 4),
            }
        )

    return ParsedFields(
        machine_name=None,
        bb_count=int(bb_count) if isinstance(bb_count, int) else None,
        rb_count=int(rb_count) if isinstance(rb_count, int) else None,
        bb_probability=_as_float(labeled.get("bb_probability")),
        rb_probability=_as_float(labeled.get("rb_probability")),
        synthetic_probability=_as_float(synthetic),
        total_games=int(total_games) if isinstance(total_games, int) else None,
        first_hit_count=int(first_hit) if isinstance(first_hit, int) else None,
        extra_numbers=extra,
    )


def _as_float(value: int | float | None) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def machine_name_from_filename(filename: str) -> str | None:
    """画像ファイル名から機種名を拾う（例: ネオアイム　数値.png → ネオアイム）。"""
    stem = filename.rsplit(".", 1)[0]
    stem = stem.replace("　", " ").strip()
    stem = re.sub(r"\s*(数値|データ|画面|キャプチャ)\s*$", "", stem)
    stem = stem.strip()
    return stem or None
