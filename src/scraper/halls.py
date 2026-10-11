"""巡回対象ホール。config/halls.json で追加・削除する。"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from src.scraper.area_map import EXTRA_AREA_PRESETS, infer_area

PROJECT_ROOT = Path(__file__).resolve().parents[2]
HALLS_CONFIG_PATH = PROJECT_ROOT / "config" / "halls.json"

WORKER_USER_AGENTS = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
)


GROUP_LABELS = {
    "tamaya": "玉屋グループ",
    "plaza": "プラザ系列",
    "123": "123グループ",
    "wonderland": "ワンダーランド",
    "dstation": "D'station",
    "maruhan": "マルハン",
    "kicona": "キコーナ",
    "espace": "エスパス",
    "face": "フェイス",
    "gaia": "ガイア",
    "island": "アイランド",
    "other": "その他店舗",
}

# JIS 都道府県コード順。テラモバの prefecture_cd と台データの県名をここへ揃える。
PREFECTURE_CATALOG: tuple[tuple[str, str], ...] = (
    ("hokkaido", "北海道"),
    ("aomori", "青森県"),
    ("iwate", "岩手県"),
    ("miyagi", "宮城県"),
    ("akita", "秋田県"),
    ("yamagata", "山形県"),
    ("fukushima", "福島県"),
    ("ibaraki", "茨城県"),
    ("tochigi", "栃木県"),
    ("gunma", "群馬県"),
    ("saitama", "埼玉県"),
    ("chiba", "千葉県"),
    ("tokyo", "東京都"),
    ("kanagawa", "神奈川県"),
    ("niigata", "新潟県"),
    ("toyama", "富山県"),
    ("ishikawa", "石川県"),
    ("fukui", "福井県"),
    ("yamanashi", "山梨県"),
    ("nagano", "長野県"),
    ("gifu", "岐阜県"),
    ("shizuoka", "静岡県"),
    ("aichi", "愛知県"),
    ("mie", "三重県"),
    ("shiga", "滋賀県"),
    ("kyoto", "京都府"),
    ("osaka", "大阪府"),
    ("hyogo", "兵庫県"),
    ("nara", "奈良県"),
    ("wakayama", "和歌山県"),
    ("tottori", "鳥取県"),
    ("shimane", "島根県"),
    ("okayama", "岡山県"),
    ("hiroshima", "広島県"),
    ("yamaguchi", "山口県"),
    ("tokushima", "徳島県"),
    ("kagawa", "香川県"),
    ("ehime", "愛媛県"),
    ("kochi", "高知県"),
    ("fukuoka", "福岡県"),
    ("saga", "佐賀県"),
    ("nagasaki", "長崎県"),
    ("kumamoto", "熊本県"),
    ("oita", "大分県"),
    ("miyazaki", "宮崎県"),
    ("kagoshima", "鹿児島県"),
    ("okinawa", "沖縄県"),
)
PREFECTURE_LABELS = dict(PREFECTURE_CATALOG)
PREFECTURE_BY_LABEL = {label: key for key, label in PREFECTURE_CATALOG}
PREFECTURE_BY_CODE = {index: key for index, (key, _label) in enumerate(PREFECTURE_CATALOG, start=1)}
_PREFECTURE_PREFERRED = ("fukuoka", "kumamoto", "saga", "tokyo", "osaka")
PREFECTURE_ORDER = _PREFECTURE_PREFERRED + tuple(
    key for key, _label in PREFECTURE_CATALOG if key not in _PREFECTURE_PREFERRED
)
DEFAULT_PREFECTURE = "fukuoka"

# 詳細エリア。areas が空の all は、その都道府県の全店舗。
# 飯塚・宗像は個別タブを持たず、福岡県の「全域」にだけ含める。
AREA_PRESETS: tuple[dict[str, Any], ...] = (
    {
        "id": "fukuoka_central",
        "prefecture": "fukuoka",
        "label": "福岡中央（天神・博多・南区）",
        "areas": ("fukuoka_central",),
        "default": True,
    },
    {
        "id": "fukuoka_west",
        "prefecture": "fukuoka",
        "label": "福岡西・糸島",
        "areas": ("fukuoka_west",),
    },
    {
        "id": "fukuoka_east",
        "prefecture": "fukuoka",
        "label": "福岡東・糟屋",
        "areas": ("fukuoka_east",),
    },
    {
        "id": "chikushi",
        "prefecture": "fukuoka",
        "label": "筑紫（春日・大野城・太宰府・筑紫野）",
        "areas": ("chikushi",),
    },
    {
        "id": "chikugo",
        "prefecture": "fukuoka",
        "label": "筑後・久留米",
        "areas": ("chikugo",),
    },
    {
        "id": "kitakyushu",
        "prefecture": "fukuoka",
        "label": "北九州",
        "areas": ("kitakyushu",),
    },
    {
        "id": "all",
        "prefecture": "fukuoka",
        "label": "全域（福岡県内すべて）",
        "areas": (),
    },
) + EXTRA_AREA_PRESETS

# 総回転数に ART/AT 中のゲームが含まれるホールコンピューターか。
# 玉屋は通常時のみ。プラザと123グループは ART/AT 消化Gを含む。
GROUP_INCLUDES_ART_IN_TOTAL_GAMES = {
    "tamaya": False,
    "plaza": True,
    "123": True,
}


@dataclass(frozen=True)
class Hall:
    id: str
    name: str
    aliases: tuple[str, ...]
    enabled: bool = True
    source: str = "site7"
    source_url: str | None = None
    source_config: dict[str, Any] | None = None
    group: str = ""
    includes_art_in_total_games: bool = False
    area: str = ""
    prefecture: str = ""
    maruhan_hall_id: str = ""


def _load_dotenv() -> None:
    env_path = PROJECT_ROOT / ".env"
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


@lru_cache(maxsize=1)
def load_halls_config(path: str | None = None) -> dict[str, Any]:
    _load_dotenv()
    config_path = Path(path) if path else HALLS_CONFIG_PATH
    return _read_json(config_path)


def hall_from_raw(raw: dict[str, Any]) -> Hall | None:
    """halls.json の1店舗を Hall にする。source_type は source の別名。"""
    name = str(raw.get("name") or "").strip()
    if not name:
        return None
    hall_id = str(raw.get("id") or hall_slug(name)).strip()
    aliases = tuple(
        dict.fromkeys(
            [name, *(str(a).strip() for a in (raw.get("aliases") or []) if str(a).strip())]
        )
    )
    source = str(raw.get("source") or raw.get("source_type") or "site7").strip() or "site7"
    source_url = str(raw.get("source_url") or "").strip() or None
    extra = raw.get(source)
    source_config = dict(extra) if isinstance(extra, dict) else None
    maruhan_hall_id = ""
    if source == "maruhan_app":
        maruhan_hall_id = str(
            raw.get("maruhan_hall_id")
            or (source_config or {}).get("maruhan_hall_id")
            or (source_config or {}).get("hall_id")
            or ""
        ).strip()
        if maruhan_hall_id:
            source_config = {**(source_config or {}), "maruhan_hall_id": maruhan_hall_id}
    group = str(raw.get("group") or "").strip()
    area = str(raw.get("area") or "").strip()
    prefecture = str(raw.get("prefecture") or "").strip()
    if "includes_art_in_total_games" in raw:
        includes_art = bool(raw.get("includes_art_in_total_games"))
    else:
        includes_art = GROUP_INCLUDES_ART_IN_TOTAL_GAMES.get(group, False)
    return Hall(
        id=hall_id,
        name=name,
        aliases=aliases,
        enabled=bool(raw.get("enabled", True)),
        source=source,
        source_url=source_url,
        source_config=source_config,
        group=group,
        includes_art_in_total_games=includes_art,
        area=area,
        prefecture=prefecture,
        maruhan_hall_id=maruhan_hall_id,
    )


def load_halls(*, include_disabled: bool = False) -> list[Hall]:
    payload = load_halls_config()
    items: list[Hall] = []
    for raw in payload.get("halls") or []:
        if not isinstance(raw, dict):
            continue
        if raw.get("archived") is True:
            continue
        hall = hall_from_raw(raw)
        if hall is None:
            continue
        if not include_disabled and not hall.enabled:
            continue
        items.append(hall)
    return items


def includes_art_in_total_games(name: str | None) -> bool:
    """店舗の総回転数が ART/AT 中ゲームを含むか。未登録店は含まない扱い。"""
    hall = hall_by_name(name or "")
    return bool(hall and hall.includes_art_in_total_games)


def hall_by_name(name: str) -> Hall | None:
    target = name.strip()
    if not target:
        return None
    for hall in load_halls(include_disabled=True):
        if hall.name == target or target in hall.aliases:
            return hall
    return None


def resolve_hall_names(values: list[str] | tuple[str, ...] | str) -> list[str]:
    """CLI や環境変数の店舗名を、config の正式名へ揃える。"""
    if isinstance(values, str):
        parts = [part.strip() for part in values.split(",") if part.strip()]
    else:
        parts = [str(part).strip() for part in values if str(part).strip()]
    resolved: list[str] = []
    for part in parts:
        hall = hall_by_name(part)
        name = hall.name if hall else part
        if name not in resolved:
            resolved.append(name)
    return resolved


def hall_patterns(name: str) -> tuple[str, ...]:
    hall = hall_by_name(name)
    if hall:
        return hall.aliases
    return (name,)


def hall_source(name: str) -> str:
    hall = hall_by_name(name)
    return hall.source if hall else "site7"


def halls_for_source(source_id: str, *, names: list[str] | None = None) -> list[Hall]:
    wanted = names if names is not None else configured_halls()
    return [hall for hall in load_halls() if hall.source == source_id and hall.name in wanted]


def group_label(group_id: str) -> str:
    return GROUP_LABELS.get(group_id, group_id or "その他")


def _preset_payload(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item["id"],
        "prefecture": item["prefecture"],
        "label": item["label"],
        "areas": list(item["areas"]),
    }


def area_presets(*, include_disabled: bool = True) -> list[dict[str, Any]]:
    """都道府県ごとの詳細エリア。all は areas が空で、その県の全店舗。"""
    presets = [_preset_payload(item) for item in AREA_PRESETS]
    covered = {item["prefecture"] for item in presets}
    for pref in prefectures(include_disabled=include_disabled):
        if pref["id"] in covered:
            continue
        label = pref["label"]
        presets.append(
            {
                "id": "all",
                "prefecture": pref["id"],
                "label": f"全域（{label}内すべて）",
                "areas": [],
            }
        )
    enabled = [hall for hall in load_halls() if hall.prefecture]
    for preset in presets:
        areas = {str(area) for area in preset.get("areas") or []}
        if areas:
            preset["hall_count"] = sum(
                1 for hall in enabled if hall.prefecture == preset["prefecture"] and hall.area in areas
            )
        else:
            preset["hall_count"] = sum(1 for hall in enabled if hall.prefecture == preset["prefecture"])
    return presets


def prefectures(*, include_disabled: bool = True) -> list[dict[str, Any]]:
    """店舗が存在する都道府県だけを、福岡県を先頭にして返す。"""
    present = {
        hall.prefecture
        for hall in load_halls(include_disabled=include_disabled)
        if hall.prefecture
    }
    ordered = [key for key in PREFECTURE_ORDER if key in present]
    ordered.extend(sorted(present.difference(ordered)))
    enabled_counts: dict[str, int] = {}
    for hall in load_halls():
        if hall.prefecture:
            enabled_counts[hall.prefecture] = enabled_counts.get(hall.prefecture, 0) + 1
    rows: list[dict[str, Any]] = []
    for key in ordered:
        default_area = "all"
        for item in AREA_PRESETS:
            if item["prefecture"] == key and item.get("default"):
                default_area = item["id"]
                break
        rows.append(
            {
                "id": key,
                "label": PREFECTURE_LABELS.get(key, key),
                "default_area": default_area,
                "hall_count": enabled_counts.get(key, 0),
            }
        )
    return rows


def hall_area_index(*, include_disabled: bool = False) -> dict[str, str]:
    """店舗名 → area タグ。未設定の店舗は含めない。"""
    return {
        hall.name: hall.area
        for hall in load_halls(include_disabled=include_disabled)
        if hall.area
    }


def hall_locale_index(*, include_disabled: bool = False) -> dict[str, dict[str, str]]:
    """店舗名 → prefecture / area。県が違う店舗はダッシュボード側で除外する。"""
    return {
        hall.name: {"prefecture": hall.prefecture, "area": hall.area}
        for hall in load_halls(include_disabled=include_disabled)
        if hall.prefecture or hall.area
    }


def hall_groups(*, include_disabled: bool = False) -> list[dict[str, Any]]:
    """有効ホールを系列ごとにまとめる。並びは玉屋 → プラザ → 123 → その他。"""
    order = list(GROUP_LABELS)
    buckets: dict[str, list[str]] = {}
    for hall in load_halls(include_disabled=include_disabled):
        buckets.setdefault(hall.group or "other", []).append(hall.name)
    keys = [key for key in order if key in buckets]
    keys.extend(key for key in buckets if key not in keys)
    rows = [
        {"id": key, "label": group_label(key), "halls": buckets[key]}
        for key in keys
    ]
    if scrape_groups_only():
        scoped: list[dict[str, Any]] = []
        for row in rows:
            names = []
            for name in row["halls"]:
                hall = hall_by_name(name)
                if hall is not None and hall_in_active_scope(hall):
                    names.append(name)
            if names:
                scoped.append({**row, "halls": names})
        return scoped
    return rows


def halls_in_group(group_id: str) -> list[str]:
    for group in hall_groups():
        if group["id"] == group_id:
            return list(group["halls"])
    return []


def scrape_groups_only() -> bool:
    """True のあいだは、玉屋・プラザグループ・ワンダーランド以外を取得しない。"""
    return bool(load_halls_config().get("scrape_groups_only"))


def hall_in_active_scope(hall: Hall) -> bool:
    """本日の取得対象か。

    プラザは店名に「プラザ」が付く他系列を含めず、プラザグループ
    （plaza-grp）の店舗だけ。他都道府県は指示があるまで対象外。
    """
    if not scrape_groups_only():
        return True
    if hall.prefecture != "fukuoka":
        return False
    name = hall.name or ""
    if hall.group == "tamaya" and "玉屋" in name:
        return True
    if hall.group == "wonderland" and "ワンダーランド" in name:
        return True
    if hall.group == "plaza" and _is_plaza_group_hall(hall):
        return True
    return False


def _is_plaza_group_hall(hall: Hall) -> bool:
    url = hall.source_url or ""
    base = ""
    if hall.source_config:
        base = str(hall.source_config.get("base_url") or "")
    return "plaza-grp" in url or "plaza-grp" in base


def configured_halls() -> list[str]:
    raw = (os.getenv("SITE7_HALL_NAMES") or "").strip()
    if raw:
        names = resolve_hall_names(raw)
    else:
        one = (os.getenv("SITE7_HALL_NAME") or "").strip()
        names = [hall.name for hall in load_halls()]
        if one and not names:
            names = resolve_hall_names(one)
    if not scrape_groups_only():
        return names
    allowed = {
        hall.name
        for hall in load_halls()
        if hall_in_active_scope(hall)
    }
    return [name for name in names if name in allowed]


def sleep_hall_gap() -> float:
    """1店舗の取得が終わってから、次の店舗へ進むまでの待機。"""
    import random
    import time

    lo, hi = pause_range()
    delay = random.uniform(lo, hi)
    print(f"  次店舗まで {delay:.1f}s 待機", flush=True)
    time.sleep(delay)
    return delay


def pause_range() -> tuple[float, float]:
    payload = load_halls_config()
    lo = float(
        os.getenv("SCRAPER_PAUSE_MIN_SEC")
        or os.getenv("SITE7_PAUSE_MIN_SEC")
        or payload.get("pause_min_sec")
        or 8.0
    )
    hi = float(
        os.getenv("SCRAPER_PAUSE_MAX_SEC")
        or os.getenv("SITE7_PAUSE_MAX_SEC")
        or payload.get("pause_max_sec")
        or 12.0
    )
    if hi < lo:
        hi = lo
    return lo, hi


def max_workers() -> int:
    """同時に進める店舗数。出口IPを分ける前提で 15〜20。"""
    payload = load_halls_config()
    raw = os.getenv("SCRAPER_MAX_WORKERS")
    if raw and raw.strip():
        value = int(raw)
    else:
        value = int(payload.get("max_workers") or 18)
    return max(1, min(20, value))


def worker_user_agent(worker_id: int) -> str:
    override = (os.getenv(f"SCRAPER_USER_AGENT_{worker_id}") or "").strip()
    if override:
        return override
    return WORKER_USER_AGENTS[(max(1, worker_id) - 1) % len(WORKER_USER_AGENTS)]


def worker_storage_path(base: Path, worker_id: int) -> Path:
    return base.parent / f"{base.stem}.w{worker_id}{base.suffix}"


def hall_slug(name: str) -> str:
    text = re.sub(r"[^\w\-]+", "-", name.strip(), flags=re.UNICODE)
    return text.strip("-") or "hall"


def hall_dir_slug(name: str) -> str:
    hall = hall_by_name(name)
    if hall and hall.id:
        return hall.id
    return hall_slug(name)


def hall_data_dir(name: str, root: Path | None = None) -> Path:
    base = root or (PROJECT_ROOT / "data" / "site7")
    return base / "halls" / hall_dir_slug(name)


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "不明"
    total = max(0, int(round(seconds)))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}時間{minutes}分{secs}秒"
    if minutes:
        return f"{minutes}分{secs}秒"
    return f"{secs}秒"
