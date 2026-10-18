"""OCR抽出結果と機種マスターを突合し、全6 / 全56 / 全456を判定する。"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from src.analyzer.normal_games import resolve_real_normal_games
from src.normalize import norm_name
from src.scraper.halls import configured_halls, hall_dir_slug, includes_art_in_total_games
from src.scraper.targets import is_judgment_excluded, match_target

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OCR_DIR = PROJECT_ROOT / "data" / "ocr_results"
DEFAULT_MASTERS_PATH = PROJECT_ROOT / "config" / "machines.json"
LEGACY_MASTERS_PATH = PROJECT_ROOT / "data" / "masters" / "machines.json"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "judgments"

SETTING_ESTIMATE_LABELS = {
    6: "設定6相当",
    5: "設定5相当",
    4: "設定4相当",
    2: "設定2相当",
    1: "設定1相当",
    0: "基準未満",
}

STATUS_ALL_HIGH = "ALL_HIGH"
STATUS_HIGH_56 = "HIGH_56"
STATUS_HIGH_BASE = "HIGH_BASE"
STATUS_EARLY_ALERT = "EARLY_ALERT"
STATUS_NOT_HIGH = "NOT_HIGH"
STATUS_INSUFFICIENT_GAMES = "INSUFFICIENT_GAMES"
STATUS_UNMATCHED = "UNMATCHED"
STATUS_INCOMPLETE = "INCOMPLETE"
STATUS_EXCLUDED = "EXCLUDED"

STATUS_LABELS = {
    STATUS_ALL_HIGH: "全6数値",
    STATUS_HIGH_56: "全56数値",
    STATUS_HIGH_BASE: "全456数値",
    STATUS_EARLY_ALERT: "全456数値",
    STATUS_NOT_HIGH: "その他（通常）",
    STATUS_INSUFFICIENT_GAMES: "通常",
    STATUS_UNMATCHED: "マスター未登録",
    STATUS_INCOMPLETE: "データ不足",
    STATUS_EXCLUDED: "判定除外",
}

STATUS_RANK = {
    STATUS_ALL_HIGH: 0,
    STATUS_HIGH_56: 1,
    STATUS_HIGH_BASE: 2,
    STATUS_EARLY_ALERT: 3,
    STATUS_NOT_HIGH: 4,
    STATUS_INSUFFICIENT_GAMES: 5,
    STATUS_INCOMPLETE: 6,
    STATUS_UNMATCHED: 7,
    STATUS_EXCLUDED: 8,
}

# 設定6・設定5理論値に対する悪い方向の許容。良い方向（理論値以下）はその設定を満たす。
# 3〜5%帯の中央。一つ下の設定の公表値以上へは広げない。
SETTING6_RELATIVE_TOLERANCE = 0.04
SETTING6_MARGIN_MIN_GAMES = 3000
# 設定5・設定4がマスターに無いとき、設定6から埋める倍率。
DERIVE_SETTING5_RATIO = 1.07
DERIVE_SETTING4_SYNTHETIC_RATIO = 1.16
DERIVE_SETTING4_RB_RATIO = 1.26


def _norm_name(value: str | None) -> str:
    return norm_name(value)


def _dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


@dataclass(frozen=True)
class SettingBar:
    synthetic: float
    rb: float | None = None

    def to_dict(self) -> dict[str, float | None]:
        payload: dict[str, float | None] = {
            "synthetic": self.synthetic,
            "first_hit": self.synthetic,
        }
        if self.rb is not None:
            payload["rb"] = self.rb
        return payload


@dataclass(frozen=True)
class MachineMaster:
    id: str
    name: str
    aliases: tuple[str, ...]
    type: str
    hit_source: str
    target_count: str
    hit_target: str
    min_games: int
    early_min_games: int
    synthetic_probability_threshold: float
    rb_probability_threshold: float | None
    settings: dict[int, SettingBar]
    enabled: bool
    probability_source: str | None
    raw: dict[str, Any]

    def names(self) -> tuple[str, ...]:
        return (self.name, self.id, *self.aliases)

    def to_summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "series": self.raw.get("series"),
            "type": self.type,
            "hit_source": self.hit_source,
            "target_count": self.target_count,
            "hit_target": self.hit_target,
            "override": self.raw.get("override") if isinstance(self.raw.get("override"), dict) else None,
            "enabled": self.enabled,
            "is_disabled": not self.enabled,
            "probability_source": self.probability_source,
            "setting_1": self.settings[1].synthetic if 1 in self.settings else None,
            "setting_2": self.settings[2].synthetic if 2 in self.settings else None,
            "setting_3": self.settings[3].synthetic if 3 in self.settings else None,
            "setting_4": self.settings[4].synthetic if 4 in self.settings else None,
            "setting_5": self.settings[5].synthetic if 5 in self.settings else None,
            "setting_6": self.settings[6].synthetic if 6 in self.settings else None,
            "min_games": self.min_games,
            "early_min_games": self.early_min_games,
            "synthetic_probability_threshold": self.synthetic_probability_threshold,
            "rb_probability_threshold": self.rb_probability_threshold,
            "settings": {str(level): bar.to_dict() for level, bar in sorted(self.settings.items())},
        }


TARGET_COUNT_BONUS = "bonus"
TARGET_COUNT_ST_AT = "st_at"
TARGET_COUNT_CZ = "cz"
TARGET_COUNT_ALIASES = {
    "bonus": TARGET_COUNT_BONUS,
    "bb": TARGET_COUNT_BONUS,
    "st_at": TARGET_COUNT_ST_AT,
    "st": TARGET_COUNT_ST_AT,
    "at": TARGET_COUNT_ST_AT,
    "art": TARGET_COUNT_ST_AT,
    "cz": TARGET_COUNT_CZ,
    "rb": TARGET_COUNT_CZ,
}


HIT_TARGET_BB = "bb"
HIT_TARGET_RB = "rb"
HIT_TARGET_COMBINED = "combined"
HIT_TARGET_ALIASES = {
    "bb": HIT_TARGET_BB,
    "big": HIT_TARGET_BB,
    "bonus": HIT_TARGET_BB,
    "rb": HIT_TARGET_RB,
    "reg": HIT_TARGET_RB,
    "cz": HIT_TARGET_RB,
    "combined": HIT_TARGET_COMBINED,
    "synthetic": HIT_TARGET_COMBINED,
    "合算": HIT_TARGET_COMBINED,
}


def default_hit_target(machine_type: str | None) -> str:
    """未指定時の初当り対象。Aタイプは合算、AT機はRB。"""
    if str(machine_type or "").strip().upper() == "A":
        return HIT_TARGET_COMBINED
    return HIT_TARGET_RB


def normalize_hit_target(value: str | None, *, machine_type: str = "AT") -> str:
    if str(machine_type or "").strip().upper() != "A":
        return HIT_TARGET_RB
    key = str(value or "").strip().lower()
    if not key:
        return default_hit_target(machine_type)
    return HIT_TARGET_ALIASES.get(key, default_hit_target(machine_type))


def forced_hit_target(judgment_type: str | None, hit_target: str | None) -> str:
    """判定に使う初当り対象。AT機はマスターや上書きが BB でも RB に固定する。"""
    if judgment_type == "AT":
        return HIT_TARGET_RB
    if hit_target in (HIT_TARGET_BB, HIT_TARGET_RB, HIT_TARGET_COMBINED):
        return hit_target
    return default_hit_target(judgment_type)


def uses_legacy_observation(judgment_type: str | None, hit_target: str | None) -> bool:
    """既存の集計式を使うか。Aは合算+REG、ATはRBだけ。"""
    if judgment_type == "A":
        return (hit_target or HIT_TARGET_COMBINED) == HIT_TARGET_COMBINED
    if judgment_type == "AT":
        return (hit_target or HIT_TARGET_RB) == HIT_TARGET_RB
    return True


def compare_type_for(judgment_type: str | None, hit_target: str | None) -> str | None:
    """設定比較の型。Aタイプの合算だけ REG も見る。それ以外は初当り1本。"""
    if judgment_type == "A" and (hit_target or HIT_TARGET_COMBINED) == HIT_TARGET_COMBINED:
        return "A"
    if judgment_type in ("A", "AT"):
        return "AT"
    return judgment_type


def observe_hit_target(
    hit_target: str,
    bb_count: int | None,
    rb_count: int | None,
    games: int | None,
) -> tuple[int | None, float | None, float | None]:
    """指定した初当り対象の回数と確率。比較用のRB確率は同じ値にする。"""
    if hit_target == HIT_TARGET_BB:
        hits = bb_count if bb_count and bb_count > 0 else None
    elif hit_target == HIT_TARGET_COMBINED:
        if bb_count is None and rb_count is None:
            hits = None
        else:
            hits = (bb_count or 0) + (rb_count or 0)
            if hits <= 0:
                hits = None
    else:
        hits = rb_count if rb_count and rb_count > 0 else None
    probability = _ratio_from_counts(games, hits)
    return hits, probability, probability


def normalize_target_count(value: str | None, *, machine_type: str = "AT") -> str:
    if str(machine_type or "").strip().upper() == "A":
        return TARGET_COUNT_BONUS
    key = str(value or "").strip().lower()
    return TARGET_COUNT_ALIASES.get(key, TARGET_COUNT_CZ)


def hit_source_from_target_count(target_count: str) -> str:
    if target_count == TARGET_COUNT_ST_AT:
        return "art"
    if target_count == TARGET_COUNT_BONUS:
        return "bb"
    return "rb"


def _top_setting(item: dict[str, Any], level: int) -> float | None:
    return _as_float(item.get(f"setting_{level}"))


def _derive_bar(setting6: SettingBar, level: int) -> SettingBar:
    if level == 6:
        return setting6
    if level == 5:
        return SettingBar(
            synthetic=round(setting6.synthetic * DERIVE_SETTING5_RATIO, 1),
            rb=round(setting6.rb * DERIVE_SETTING5_RATIO, 1) if setting6.rb is not None else None,
        )
    return SettingBar(
        synthetic=round(setting6.synthetic * DERIVE_SETTING4_SYNTHETIC_RATIO, 1),
        rb=round(setting6.rb * DERIVE_SETTING4_RB_RATIO, 1) if setting6.rb is not None else None,
    )


def _setting_primary(entry: dict[str, Any] | None) -> float | None:
    if not isinstance(entry, dict):
        return None
    for key in ("first_hit", "synthetic", "at_first_hit"):
        if entry.get(key) is not None:
            return float(entry[key])
    return None


def _load_settings(item: dict[str, Any], syn6: float, rb6: float | None) -> dict[int, SettingBar]:
    raw = (item.get("criteria") or {}).get("settings") or {}
    bars: dict[int, SettingBar] = {}
    for key, entry in raw.items():
        try:
            level = int(key)
        except (TypeError, ValueError):
            continue
        primary = _setting_primary(entry)
        if primary is None:
            continue
        rb = entry.get("rb") if isinstance(entry, dict) else None
        bars[level] = SettingBar(
            synthetic=primary,
            rb=float(rb) if rb is not None else None,
        )
    for level in (6, 5, 4, 3, 2, 1):
        top = _top_setting(item, level)
        if top is None:
            continue
        existing = bars.get(level)
        bars[level] = SettingBar(top, existing.rb if existing else None)
    if 6 not in bars:
        bars[6] = SettingBar(float(syn6), float(rb6) if rb6 is not None else None)
    if 5 not in bars:
        bars[5] = _derive_bar(bars[6], 5)
    if 4 not in bars:
        bars[4] = _derive_bar(bars[6], 4)
    return bars


def _published_setting6(item: dict[str, Any]) -> tuple[float, float | None] | None:
    criteria = item.get("criteria") or {}
    published = item.get("published") or {}
    settings = criteria.get("settings") or {}
    if not isinstance(settings, dict):
        settings = {}
    entry6 = settings.get("6") or settings.get(6) or {}
    syn6 = _top_setting(item, 6)
    if syn6 is None:
        syn6 = _setting_primary(entry6)
    if syn6 is None:
        syn6 = _as_float(criteria.get("synthetic_probability_threshold"))
    if syn6 is None:
        first_map = published.get("first_hit")
        if isinstance(first_map, dict) and first_map.get("6") is not None:
            syn6 = float(first_map["6"])
        else:
            syn6 = _as_float(published.get("first_hit_denominator"))
    if syn6 is None:
        return None
    rb6 = criteria.get("rb_probability_threshold")
    if rb6 is None and isinstance(entry6, dict):
        rb6 = entry6.get("rb")
    if rb6 is None:
        rb_map = published.get("rb")
        if isinstance(rb_map, dict) and rb_map.get("6") is not None:
            rb6 = rb_map["6"]
        else:
            rb6 = published.get("rb_probability")
    return float(syn6), (float(rb6) if rb6 is not None else None)


def master_enabled(item: dict[str, Any] | None) -> bool:
    """判定対象か。enabled が false、または is_disabled が true なら除外。"""
    if not isinstance(item, dict):
        return True
    if item.get("is_disabled") is True:
        return False
    if item.get("enabled") is False:
        return False
    return True


def probability_source_of(item: dict[str, Any] | None) -> str | None:
    if not isinstance(item, dict):
        return None
    raw = str(item.get("probability_source") or "").strip().lower()
    return raw or None


def resolve_masters_path(path: Path | None = None) -> Path:
    if path is not None:
        return path
    if DEFAULT_MASTERS_PATH.is_file():
        return DEFAULT_MASTERS_PATH
    return LEGACY_MASTERS_PATH


def load_masters(
    path: Path | None = None,
    *,
    apply_override: bool = True,
    overrides_path: Path | None = None,
) -> list[MachineMaster]:
    masters_path = resolve_masters_path(path)
    try:
        payload = json.loads(masters_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"機種マスターを読めませんでした: {type(exc).__name__}", flush=True)
        return []
    if not isinstance(payload, dict):
        print("機種マスターの形式が不正です", flush=True)
        return []
    if apply_override:
        from src.analyzer.overrides import apply_overrides

        payload = apply_overrides(payload, overrides_path)
    machines: list[MachineMaster] = []
    for item in payload.get("machines") or []:
        if not isinstance(item, dict):
            continue
        try:
            published = _published_setting6(item)
            if published is None:
                print(
                    f"機種マスターをスキップ（設定6基準なし）: {item.get('id') or item.get('name')}",
                    flush=True,
                )
                continue
            syn6, rb6 = published
            machine_type = str(item.get("type") or "").strip().upper() or "AT"
            target_count = normalize_target_count(
                item.get("target_count") or item.get("hit_source"),
                machine_type=machine_type,
            )
            hit_target = normalize_hit_target(item.get("hit_target"), machine_type=machine_type)
            hit_source = str(
                item.get("hit_source") or hit_source_from_target_count(target_count)
            ).strip().lower()
            if machine_type != "A":
                # AT機の初当りはデータサイトのRB回数だけ。BB・ART・合算は使わない。
                hit_target = HIT_TARGET_RB
                hit_source = "rb"
                target_count = TARGET_COUNT_CZ
            machine_id = str(item.get("id") or item.get("name") or "").strip()
            machine_name = str(item.get("name") or item.get("id") or "").strip()
            if not machine_id or not machine_name:
                print(f"機種マスターをスキップ（id/nameなし）: {item}", flush=True)
                continue
            machines.append(
                MachineMaster(
                    id=machine_id,
                    name=machine_name,
                    aliases=tuple(str(a) for a in item.get("aliases") or []),
                    type="A" if machine_type == "A" else "AT",
                    hit_source=hit_source,
                    target_count=target_count,
                    hit_target=hit_target,
                    min_games=int(criteria_min_games(item)),
                    early_min_games=int(criteria_early_min_games(item)),
                    synthetic_probability_threshold=syn6,
                    rb_probability_threshold=rb6,
                    settings=_load_settings(item, syn6, rb6),
                    enabled=master_enabled(item),
                    probability_source=probability_source_of(item),
                    raw=item,
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            print(f"機種マスターをスキップ: {item.get('id') or item.get('name')}: {type(exc).__name__}", flush=True)
            continue
    return machines


def criteria_min_games(item: dict[str, Any]) -> int:
    try:
        return int((item.get("criteria") or {}).get("min_games") or 3000)
    except (TypeError, ValueError):
        return 3000


def criteria_early_min_games(item: dict[str, Any]) -> int:
    try:
        return int((item.get("criteria") or {}).get("early_min_games") or 300)
    except (TypeError, ValueError):
        return 300


def match_machine(machine_name: str | None, masters: list[MachineMaster]) -> MachineMaster | None:
    target = _norm_name(machine_name)
    if not target:
        return None
    scored: list[tuple[int, MachineMaster]] = []
    for machine in masters:
        for candidate in machine.names():
            key = _norm_name(candidate)
            if len(key) < 3:
                continue
            if target == key:
                scored.append((10000 + len(key), machine))
            elif key in target:
                scored.append((len(key), machine))
    if not scored:
        return None
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1]


def load_ocr_records(ocr_dir: Path) -> list[dict[str, Any]]:
    """個別JSONを優先し、summary.json は欠けている分だけ補完する。"""
    ocr_dir = ocr_dir.resolve()
    by_key: dict[str, dict[str, Any]] = {}

    for path in sorted(ocr_dir.rglob("*.json")):
        if path.name == "summary.json":
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or "fields" not in payload:
            continue
        key = str(payload.get("source_file") or path.name)
        by_key[key] = payload

    summary_path = ocr_dir / "summary.json"
    if summary_path.is_file():
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
        for record in payload.get("results") or []:
            if not isinstance(record, dict):
                continue
            key = str(record.get("source_file") or "")
            if key and key not in by_key:
                by_key[key] = record

    return [by_key[key] for key in sorted(by_key)]


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _synthetic_probability(fields: dict[str, Any]) -> float | None:
    extracted = _as_float(fields.get("synthetic_probability"))
    if extracted is not None:
        return extracted
    total_games = _as_int(fields.get("total_games"))
    first_hit = _as_int(fields.get("first_hit_count"))
    if total_games is None or first_hit is None or first_hit <= 0:
        return None
    return round(total_games / first_hit, 4)


def _ratio_from_counts(games: int | None, hits: int | None) -> float | None:
    if games is None or hits is None or hits <= 0:
        return None
    return round(games / hits, 4)


def _meets_setting(
    judgment_type: str | None,
    synthetic: float | None,
    rb_probability: float | None,
    bar: SettingBar | None,
) -> bool | None:
    """設定N基準を満たすか。Aタイプは合算とREGの両方。ATは初当たりのみ。"""
    if bar is None or synthetic is None:
        return None
    if synthetic > bar.synthetic:
        return False
    if judgment_type == "AT" or bar.rb is None:
        return True
    if rb_probability is None:
        return None
    return rb_probability <= bar.rb


def _within_theory_band(actual: float, theory: float, looser: float | None) -> bool:
    """実数値が理論値の誤差範囲内か。

    分母が小さいほど高設定。理論値以下は範囲内。
    悪い方向は理論値の +4% まで許容し、一つ下の設定理論値以上にはしない。
    """
    if actual <= theory:
        return True
    ceiling = theory * (1.0 + SETTING6_RELATIVE_TOLERANCE)
    if looser is not None and looser > theory:
        if actual >= looser:
            return False
        ceiling = min(ceiling, looser)
    return actual <= ceiling


def _meets_setting_with_margin(
    judgment_type: str | None,
    synthetic: float | None,
    rb_probability: float | None,
    bar: SettingBar | None,
    looser: SettingBar | None,
    *,
    sample_games: float | None,
) -> bool | None:
    """設定基準。サンプルが足りるときは確率ブレの許容を足す。

    Aタイプは合算とREGの両方、ATは初当りだけ。許容で上位を満たしたら下位へ落とさない。
    """
    strict = _meets_setting(judgment_type, synthetic, rb_probability, bar)
    if strict is not False:
        return strict
    if sample_games is None or sample_games < SETTING6_MARGIN_MIN_GAMES:
        return False
    if bar is None or synthetic is None:
        return None
    looser_syn = looser.synthetic if looser is not None else None
    if not _within_theory_band(synthetic, bar.synthetic, looser_syn):
        return False
    if judgment_type == "AT" or bar.rb is None:
        return True
    if rb_probability is None:
        return None
    looser_rb = looser.rb if looser is not None else None
    return _within_theory_band(rb_probability, bar.rb, looser_rb)


def _meets_setting6(
    judgment_type: str | None,
    synthetic: float | None,
    rb_probability: float | None,
    bar6: SettingBar | None,
    bar5: SettingBar | None,
    *,
    sample_games: float | None,
) -> bool | None:
    """設定6判定。クリアしたら全56・全456へは降格しない。"""
    return _meets_setting_with_margin(
        judgment_type,
        synthetic,
        rb_probability,
        bar6,
        bar5,
        sample_games=sample_games,
    )


def _meets_setting5(
    judgment_type: str | None,
    synthetic: float | None,
    rb_probability: float | None,
    bar5: SettingBar | None,
    bar4: SettingBar | None,
    *,
    sample_games: float | None,
) -> bool | None:
    """設定5判定。全6に届かないときの全56基準。設定4理論値以上には広げない。"""
    return _meets_setting_with_margin(
        judgment_type,
        synthetic,
        rb_probability,
        bar5,
        bar4,
        sample_games=sample_games,
    )


def at_hit_count(
    bb_count: int | None,
    rb_count: int | None,
    art_count: int | None,
    target_count: str = TARGET_COUNT_CZ,
) -> int | None:
    """AT機の初当り回数。RB回数だけを使い、BB・ARTへはフォールバックしない。"""
    del bb_count, art_count, target_count
    return rb_count if rb_count and rb_count > 0 else None


def at_first_hit_count(
    rb_count: int | None,
    art_count: int | None,
    hit_source: str = "rb",
    *,
    bb_count: int | None = None,
    target_count: str | None = None,
    probability_source: str | None = None,
) -> int | None:
    """AT機の初当り回数。機種設定に関わらず RB 回数だけを使う。"""
    del art_count, hit_source, probability_source
    return at_hit_count(bb_count, rb_count, None, target_count or TARGET_COUNT_CZ)


def at_observation(
    bb_count: int | None,
    rb_count: int | None,
    art_count: int | None,
    games: int | None,
    *,
    target_count: str,
    probability_source: str | None = None,
) -> tuple[int | None, float | None, float | None]:
    """初当り回数、初当り確率、RB確率を返す。

    AT機は通常時G ÷ RB回数だけを判定に使う。
    BB（ST中・連チャン中のボーナスを含む）と ART、合成ボーナス確率は集計しない。
    """
    del bb_count, art_count, target_count, probability_source
    first_hit = rb_count if rb_count and rb_count > 0 else None
    probability = _ratio_from_counts(games, first_hit)
    return first_hit, probability, probability


def estimate_setting_level(
    judgment_type: str | None,
    synthetic: float | None,
    rb_probability: float | None,
    settings: dict[int, SettingBar] | None,
    *,
    sample_games: float | None = None,
) -> int | None:
    if synthetic is None or not settings:
        return None
    for level in sorted(settings.keys(), reverse=True):
        if level == 6:
            met = _meets_setting6(
                judgment_type,
                synthetic,
                rb_probability,
                settings.get(6),
                settings.get(5),
                sample_games=sample_games,
            )
        elif level == 5:
            met = _meets_setting5(
                judgment_type,
                synthetic,
                rb_probability,
                settings.get(5),
                settings.get(4),
                sample_games=sample_games,
            )
        else:
            met = _meets_setting(judgment_type, synthetic, rb_probability, settings.get(level))
        if met is True:
            return level
    return 0


def _classify(
    total_games: int,
    master: MachineMaster,
    meets6: bool,
    meets5: bool,
    meets4: bool,
) -> str:
    """高いランクを優先する。全6、次に全56、その次に全456。下位で上書きしない。

    朝一の最低ゲーム数未満は、確率だけ良くても全6・全56・全456にしない。
    """
    early_min = int(getattr(master, "early_min_games", 0) or 0)
    if early_min and total_games < early_min:
        return STATUS_INSUFFICIENT_GAMES
    if meets6:
        return STATUS_ALL_HIGH
    if meets5:
        return STATUS_HIGH_56
    if meets4:
        return STATUS_HIGH_BASE
    if total_games >= master.min_games:
        return STATUS_NOT_HIGH
    return STATUS_INSUFFICIENT_GAMES


def _incomplete_judgment(record: dict[str, Any], *, machine_name: Any = None, error: str | None = None) -> dict[str, Any]:
    fields = record.get("fields") if isinstance(record.get("fields"), dict) else {}
    name = machine_name if machine_name is not None else fields.get("machine_name")
    if error:
        from src.scraper.log_safe import failure_reason

        print(f"判定をスキップ ({name or '機種'}): {failure_reason(error)}", flush=True)
    return {
        "source_file": record.get("source_file"),
        "hall_name": record.get("hall_name") or fields.get("hall_name"),
        "machine_name": name,
        "series": None,
        "judgment_type": None,
        "matched_machine": None,
        "total_games": _as_int(fields.get("total_games")),
        "first_hit_count": _as_int(fields.get("first_hit_count")),
        "bb_count": fields.get("bb_count"),
        "rb_count": fields.get("rb_count"),
        "art_count": _as_int(fields.get("art_count") or fields.get("art")),
        "dai_no": fields.get("dai_no") or record.get("dai_no"),
        "judgment_excluded": False,
        "bb_probability": _as_float(fields.get("bb_probability")),
        "rb_probability": _as_float(fields.get("rb_probability")),
        "synthetic_probability": _as_float(fields.get("synthetic_probability")),
        "criteria": None,
        "checks": {
            "games_enough": None,
            "early_enough": None,
            "meets_setting_6": None,
            "meets_setting_5": None,
            "meets_setting_4": None,
            "probability_lte_setting6": None,
            "rb_lte_setting6": None,
        },
        "status": STATUS_INCOMPLETE,
        "status_label": STATUS_LABELS[STATUS_INCOMPLETE],
        "is_all_high": False,
        "is_high_56": False,
        "is_high_base": False,
        "is_early_alert": False,
        "estimated_setting": None,
        "estimated_setting_label": "データ不足",
    }


def judge_record(record: dict[str, Any], masters: list[MachineMaster]) -> dict[str, Any]:
    try:
        return _judge_record_body(record, masters)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        return _incomplete_judgment(record, error=str(exc))


def _judge_record_body(record: dict[str, Any], masters: list[MachineMaster]) -> dict[str, Any]:
    fields = record.get("fields") or {}
    if not isinstance(fields, dict):
        fields = {}
    machine_name = fields.get("machine_name")
    hall_name = record.get("hall_name") or fields.get("hall_name")
    reported_games = _as_int(fields.get("reported_total_games") or fields.get("total_games"))
    bb_count = _as_int(fields.get("bb_count"))
    rb_count = _as_int(fields.get("rb_count"))
    art_count = _as_int(fields.get("art_count") or fields.get("art"))
    if "includes_art_in_total_games" in fields:
        includes_art = bool(fields.get("includes_art_in_total_games"))
    else:
        includes_art = includes_art_in_total_games(str(hall_name) if hall_name else None)
    normal = resolve_real_normal_games(
        reported_games,
        includes_art_in_total_games=includes_art,
        art_count=art_count,
        history=fields.get("history"),
        hits=fields.get("hits"),
        art_games=_as_int(fields.get("art_games") or fields.get("at_games")),
    )
    total_games = _as_int(normal["real_normal_games"])
    watched = match_target(machine_name)
    master = match_machine(machine_name, masters)
    judgment_type = (master.type if master else None) or (watched.type if watched else None)
    target_count = (
        master.target_count
        if master
        else normalize_target_count(None, machine_type=judgment_type or "AT")
    )
    hit_target = forced_hit_target(
        judgment_type,
        master.hit_target if master else default_hit_target(judgment_type),
    )
    compare_as = compare_type_for(judgment_type, hit_target)

    first_hit_count: int | None = None
    synthetic: float | None = None
    rb_probability: float | None = None
    bb_probability: float | None = None

    if judgment_type == "AT":
        first_hit_count, synthetic, rb_probability = at_observation(
            bb_count,
            rb_count,
            art_count,
            total_games,
            target_count=target_count,
            probability_source=master.probability_source if master else None,
        )
        bb_probability = _ratio_from_counts(total_games, bb_count)
    elif judgment_type == "A" and not uses_legacy_observation(judgment_type, hit_target):
        first_hit_count, synthetic, rb_probability = observe_hit_target(
            hit_target,
            bb_count,
            rb_count,
            total_games,
        )
        bb_probability = _ratio_from_counts(total_games, bb_count)
    elif judgment_type == "A":
        if bb_count is not None and rb_count is not None:
            first_hit_count = bb_count + rb_count
        else:
            first_hit_count = _as_int(fields.get("first_hit_count"))
        synthetic = _ratio_from_counts(total_games, first_hit_count)
        rb_probability = _ratio_from_counts(total_games, rb_count)
        bb_probability = _ratio_from_counts(total_games, bb_count)
    else:
        first_hit_count = _as_int(fields.get("first_hit_count"))
        synthetic = _ratio_from_counts(total_games, first_hit_count)
        rb_probability = _ratio_from_counts(total_games, rb_count)
        bb_probability = _ratio_from_counts(total_games, bb_count)

    games_enough: bool | None = None
    early_enough: bool | None = None
    meets6: bool | None = None
    meets5: bool | None = None
    meets4: bool | None = None
    excluded = is_judgment_excluded(machine_name) or (master is not None and not master.enabled)
    status = STATUS_UNMATCHED

    if excluded:
        status = STATUS_EXCLUDED
    elif master is None:
        status = STATUS_UNMATCHED
    elif judgment_type == "AT" and first_hit_count is None:
        status = STATUS_INCOMPLETE
    elif total_games is None or synthetic is None:
        status = STATUS_INCOMPLETE
    else:
        games_enough = total_games >= master.min_games
        early_enough = total_games >= master.early_min_games
        meets6 = _meets_setting6(
            compare_as,
            synthetic,
            rb_probability,
            master.settings.get(6),
            master.settings.get(5),
            sample_games=total_games,
        )
        meets5 = _meets_setting5(
            compare_as,
            synthetic,
            rb_probability,
            master.settings.get(5),
            master.settings.get(4),
            sample_games=total_games,
        )
        meets4 = _meets_setting(compare_as, synthetic, rb_probability, master.settings.get(4))
        if not early_enough:
            meets6 = False
            meets5 = False
            meets4 = False
        if None in (meets6, meets5, meets4):
            status = STATUS_INCOMPLETE
        else:
            status = _classify(total_games, master, bool(meets6), bool(meets5), bool(meets4))

    estimated = estimate_setting_level(
        compare_as,
        synthetic,
        rb_probability,
        master.settings if master else None,
        sample_games=total_games,
    )

    judged = {
        "source_file": record.get("source_file"),
        "hall_name": record.get("hall_name") or fields.get("hall_name"),
        "machine_name": machine_name,
        "series": (master.raw.get("series") if master else None),
        "judgment_type": judgment_type,
        "matched_machine": master.to_summary() if master else None,
        "reported_total_games": normal["reported_total_games"],
        "art_consumed_games": normal["art_consumed_games"],
        "includes_art_in_total_games": normal["includes_art_in_total_games"],
        "normal_games_exact": normal["normal_games_exact"],
        "real_normal_games": total_games,
        "total_games": total_games,
        "first_hit_count": first_hit_count,
        "bb_count": fields.get("bb_count"),
        "rb_count": fields.get("rb_count"),
        "art_count": art_count,
        "dai_no": fields.get("dai_no") or record.get("dai_no"),
        "judgment_excluded": excluded,
        "bb_probability": bb_probability,
        "rb_probability": rb_probability,
        "synthetic_probability": synthetic,
        "criteria": master.to_summary() if master else None,
        "checks": {
            "games_enough": games_enough,
            "early_enough": early_enough,
            "meets_setting_6": meets6,
            "meets_setting_5": meets5,
            "meets_setting_4": meets4,
            "probability_lte_setting6": meets6,
            "rb_lte_setting6": (
                None
                if master is None or master.settings.get(6) is None or master.settings[6].rb is None
                else (
                    None
                    if rb_probability is None
                    else rb_probability <= master.settings[6].rb
                )
            ),
        },
        "status": status,
        "status_label": STATUS_LABELS[status],
        "is_all_high": (not excluded) and status == STATUS_ALL_HIGH,
        "is_high_56": (not excluded) and status == STATUS_HIGH_56,
        "is_high_base": (not excluded) and status == STATUS_HIGH_BASE,
        "is_early_alert": (not excluded) and status == STATUS_EARLY_ALERT,
        "estimated_setting": estimated,
        "estimated_setting_label": SETTING_ESTIMATE_LABELS.get(estimated, "データ不足") if estimated is not None else "データ不足",
    }
    diff_medals = _as_int(fields.get("差玉") or fields.get("diff_medals"))
    if diff_medals is not None:
        judged["diff_medals"] = diff_medals
    return judged


def _status_rank(item: dict[str, Any]) -> int:
    return STATUS_RANK.get(item.get("status") or "", 99)


def _sum_optional(values: list[int | None]) -> int | None:
    nums = [int(v) for v in values if v is not None]
    if not nums:
        return None
    return sum(nums)


def _sum_exact(values: list[int | None]) -> int | None:
    """1台でも不明なら合算しない。欠けた台を除いた部分合計は使わない。"""
    if not values or any(value is None for value in values):
        return None
    return sum(int(value) for value in values)


def aggregate_machine_groups(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一店舗・同一機種の台を合算し、全台系判定を機種単位で付け直す。"""
    from src.analyzer.extras import digit_judgments

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in results:
        key = (str(item.get("hall_name") or ""), str(item.get("machine_name") or ""))
        grouped.setdefault(key, []).append(item)

    machines: list[dict[str, Any]] = []
    for (hall, name), units in grouped.items():
        units_sorted = sorted(
            units,
            key=lambda item: (
                str(item.get("dai_no") or "99999").zfill(6),
                item.get("source_file") or "",
            ),
        )
        sample = units_sorted[0]
        judgment_type = sample.get("judgment_type")
        target_count = str(
            (sample.get("criteria") or {}).get("target_count")
            or (sample.get("criteria") or {}).get("hit_source")
            or ("cz" if judgment_type == "AT" else "bonus")
        )
        criteria = sample.get("criteria") if isinstance(sample.get("criteria"), dict) else None
        hit_target = forced_hit_target(
            judgment_type,
            str((criteria or {}).get("hit_target") or default_hit_target(judgment_type)),
        )
        compare_as = compare_type_for(judgment_type, hit_target)
        master: SimpleNamespace | None = None
        if criteria:
            raw_settings = criteria.get("settings") or {}
            bars: dict[int, SettingBar] = {}
            for key, entry in raw_settings.items():
                try:
                    level = int(key)
                except (TypeError, ValueError):
                    continue
                primary = _setting_primary(entry)
                if primary is None:
                    continue
                rb = entry.get("rb") if isinstance(entry, dict) else None
                bars[level] = SettingBar(
                    synthetic=primary,
                    rb=float(rb) if rb is not None else None,
                )
            syn6 = criteria.get("synthetic_probability_threshold")
            if 6 not in bars and syn6 is not None:
                rb6 = criteria.get("rb_probability_threshold")
                bars[6] = SettingBar(float(syn6), float(rb6) if rb6 is not None else None)
            for level in (1, 2, 3, 4, 5, 6):
                top = criteria.get(f"setting_{level}")
                if top is None:
                    continue
                existing = bars.get(level)
                bars[level] = SettingBar(float(top), existing.rb if existing else None)
            master = SimpleNamespace(
                min_games=int(criteria.get("min_games") or 3000),
                early_min_games=int(criteria.get("early_min_games") or 300),
                settings=bars,
            )

        games = _sum_exact([_as_int(u.get("real_normal_games")) for u in units_sorted])
        reported_games = _sum_exact([_as_int(u.get("reported_total_games")) for u in units_sorted])
        art_consumed = _sum_exact([_as_int(u.get("art_consumed_games")) for u in units_sorted])
        bb = _sum_optional([_as_int(u.get("bb_count")) for u in units_sorted])
        rb = _sum_optional([_as_int(u.get("rb_count")) for u in units_sorted])
        art = _sum_optional([_as_int(u.get("art_count")) for u in units_sorted])
        unit_count = len(units_sorted)
        avg_games = round(games / unit_count, 1) if games is not None and unit_count else None
        games_for_class = int(avg_games) if avg_games is not None else None
        known_diffs = [value for value in (_as_int(unit.get("diff_medals")) for unit in units_sorted) if value is not None]
        diff_medals = sum(known_diffs) if known_diffs else None

        first_hit: int | None
        synthetic: float | None
        rb_probability: float | None
        bb_probability: float | None
        if judgment_type == "AT":
            first_hit, synthetic, rb_probability = at_observation(
                bb,
                rb,
                art,
                games,
                target_count=target_count,
                probability_source=probability_source_of(criteria if isinstance(criteria, dict) else None),
            )
            bb_probability = _ratio_from_counts(games, bb)
        elif judgment_type == "A" and not uses_legacy_observation(judgment_type, hit_target):
            first_hit, synthetic, rb_probability = observe_hit_target(hit_target, bb, rb, games)
            bb_probability = _ratio_from_counts(games, bb)
        elif judgment_type == "A":
            if bb is not None or rb is not None:
                first_hit = (bb or 0) + (rb or 0)
                if first_hit <= 0:
                    first_hit = None
            else:
                first_hit = _sum_optional([_as_int(u.get("first_hit_count")) for u in units_sorted])
            synthetic = _ratio_from_counts(games, first_hit)
            rb_probability = _ratio_from_counts(games, rb)
            bb_probability = _ratio_from_counts(games, bb)
        else:
            first_hit = _sum_optional([_as_int(u.get("first_hit_count")) for u in units_sorted])
            synthetic = _ratio_from_counts(games, first_hit)
            rb_probability = _ratio_from_counts(games, rb)
            bb_probability = _ratio_from_counts(games, bb)

        games_enough: bool | None = None
        early_enough: bool | None = None
        meets6: bool | None = None
        meets5: bool | None = None
        meets4: bool | None = None
        excluded = (
            bool(sample.get("judgment_excluded"))
            or is_judgment_excluded(name)
            or not master_enabled(criteria if isinstance(criteria, dict) else None)
        )
        if excluded:
            status = STATUS_EXCLUDED
        elif master is None:
            status = STATUS_UNMATCHED
        elif judgment_type == "AT" and first_hit is None:
            status = STATUS_INCOMPLETE
        elif games_for_class is None or synthetic is None:
            status = STATUS_INCOMPLETE
        else:
            games_enough = games_for_class >= master.min_games
            early_enough = games_for_class >= master.early_min_games
            meets6 = _meets_setting6(
                compare_as,
                synthetic,
                rb_probability,
                master.settings.get(6),
                master.settings.get(5),
                sample_games=avg_games,
            )
            meets5 = _meets_setting5(
                compare_as,
                synthetic,
                rb_probability,
                master.settings.get(5),
                master.settings.get(4),
                sample_games=avg_games,
            )
            meets4 = _meets_setting(compare_as, synthetic, rb_probability, master.settings.get(4))
            if not early_enough:
                meets6 = False
                meets5 = False
                meets4 = False
            if None in (meets6, meets5, meets4):
                status = STATUS_INCOMPLETE
            else:
                status = _classify(games_for_class, master, bool(meets6), bool(meets5), bool(meets4))

        machines.append(
            {
                "hall_name": hall or None,
                "machine_name": name or None,
                "series": sample.get("series"),
                "judgment_type": judgment_type,
                "criteria": criteria,
                "unit_count": unit_count,
                "reported_total_games": reported_games,
                "art_consumed_games": art_consumed,
                "includes_art_in_total_games": bool(sample.get("includes_art_in_total_games")),
                "normal_games_exact": games is not None,
                "real_normal_games": games,
                "total_games": games,
                "avg_games": avg_games,
                "first_hit_count": first_hit,
                "bb_count": bb,
                "rb_count": rb,
                "art_count": art,
                "diff_medals": diff_medals,
                "bb_probability": bb_probability,
                "rb_probability": rb_probability,
                "synthetic_probability": synthetic,
                "checks": {
                    "games_enough": games_enough,
                    "early_enough": early_enough,
                    "meets_setting_6": meets6,
                    "meets_setting_5": meets5,
                    "meets_setting_4": meets4,
                },
                "status": status,
                "status_label": STATUS_LABELS[status],
                "is_all_high": (not excluded) and status == STATUS_ALL_HIGH,
                "is_high_56": (not excluded) and status == STATUS_HIGH_56,
                "is_high_base": (not excluded) and status == STATUS_HIGH_BASE,
                "is_early_alert": (not excluded) and status == STATUS_EARLY_ALERT,
                "judgment_excluded": excluded,
                "digit_judgments": [],
                "units": [
                    {
                        "dai_no": unit.get("dai_no"),
                        "reported_total_games": unit.get("reported_total_games"),
                        "art_consumed_games": unit.get("art_consumed_games"),
                        "real_normal_games": unit.get("real_normal_games"),
                        "total_games": unit.get("real_normal_games"),
                        "bb_count": unit.get("bb_count"),
                        "rb_count": unit.get("rb_count"),
                        "art_count": unit.get("art_count"),
                        "first_hit_count": unit.get("first_hit_count"),
                        "synthetic_probability": unit.get("synthetic_probability"),
                        "bb_probability": unit.get("bb_probability")
                        if unit.get("bb_probability") is not None
                        else _ratio_from_counts(_as_int(unit.get("total_games")), _as_int(unit.get("bb_count"))),
                        "rb_probability": unit.get("rb_probability"),
                        "diff_medals": unit.get("diff_medals"),
                        "status": unit.get("status"),
                        "status_label": unit.get("status_label"),
                        "estimated_setting": unit.get("estimated_setting"),
                        "estimated_setting_label": unit.get("estimated_setting_label"),
                    }
                    for unit in units_sorted
                ],
            }
        )
        machine = machines[-1]
        if not excluded:
            try:
                machine["digit_judgments"] = digit_judgments(
                    machine["units"],
                    judgment_type=judgment_type,
                    criteria=criteria,
                )
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                print(f"末尾判定をスキップ ({hall} {name}): {type(exc).__name__}", flush=True)
                machine["digit_judgments"] = []
    machines.sort(
        key=lambda item: (
            _status_rank(item),
            item.get("hall_name") or "",
            item.get("machine_name") or "",
        )
    )
    return machines


def _machine_hall_counts(machines: list[dict[str, Any]], halls: list[str]) -> dict[str, dict[str, int]]:
    return {
        hall: {
            "count": sum(item["unit_count"] for item in machines if item.get("hall_name") == hall),
            "machine_count": sum(1 for item in machines if item.get("hall_name") == hall),
            "all_high_count": sum(1 for item in machines if item.get("hall_name") == hall and item["is_all_high"]),
            "high_56_count": sum(1 for item in machines if item.get("hall_name") == hall and item["is_high_56"]),
            "high_base_count": sum(1 for item in machines if item.get("hall_name") == hall and item["is_high_base"]),
            "early_alert_count": sum(1 for item in machines if item.get("hall_name") == hall and item["is_early_alert"]),
        }
        for hall in halls
    }


def judge_records(
    records: list[dict[str, Any]],
    masters_path: Path = DEFAULT_MASTERS_PATH,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    source: str | None = None,
    *,
    incremental: bool = False,
) -> dict[str, Any]:
    try:
        masters = load_masters(resolve_masters_path(masters_path))
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"機種マスター読み込みに失敗したため空のマスターで継続します: {type(exc).__name__}", flush=True)
        masters = []
    watched = [
        record
        for record in records
        if match_target((record.get("fields") or {}).get("machine_name") or record.get("machine_name"))
    ]
    results = [judge_record(record, masters) for record in watched]
    try:
        machines = aggregate_machine_groups(results)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        print(f"機種集計に失敗したため台単位の結果のみ残します: {type(exc).__name__}", flush=True)
        machines = []
    from src.analyzer.extras import hall_digit_summaries
    machines.sort(
        key=lambda item: (
            _status_rank(item),
            item.get("hall_name") or "",
            item.get("machine_name") or "",
        )
    )
    results.sort(
        key=lambda item: (
            _status_rank(item),
            item.get("hall_name") or "",
            item.get("machine_name") or "",
            str(item.get("dai_no") or ""),
        )
    )
    all_high = [item for item in machines if item["is_all_high"]]
    high_56 = [item for item in machines if item["is_high_56"]]
    high_base = [item for item in machines if item["is_high_base"]]
    early_alert = [item for item in machines if item["is_early_alert"]]
    excluded_machines = [item for item in machines if item.get("judgment_excluded") or item.get("status") == STATUS_EXCLUDED]
    digit_hits = []
    for item in machines:
        if item.get("judgment_excluded"):
            continue
        for row in item.get("digit_judgments") or []:
            if row.get("status") in (STATUS_ALL_HIGH, "HIGH_56", STATUS_HIGH_BASE):
                digit_hits.append(
                    {
                        "hall_name": item.get("hall_name"),
                        "machine_name": item.get("machine_name"),
                        "judgment_type": item.get("judgment_type"),
                        **{k: row[k] for k in row},
                    }
                )
    hall_digits = hall_digit_summaries(results)
    status_counts: dict[str, int] = {}
    for item in machines:
        status_counts[item["status"]] = status_counts.get(item["status"], 0) + 1
    found = {str(item.get("hall_name")) for item in machines if item.get("hall_name")}
    halls = list(dict.fromkeys([*configured_halls(), *sorted(found)]))
    hall_counts = _machine_hall_counts(machines, halls)
    unit_count = len(results)
    fetched_at = next((str(item["fetched_at"]) for item in watched if item.get("fetched_at")), None)

    summary = {
        "judged_at": datetime.now(timezone.utc).isoformat(),
        "fetched_at": fetched_at,
        "source": source,
        "masters_path": str(masters_path.resolve()),
        "count": len(machines),
        "machine_count": len(machines),
        "unit_count": unit_count,
        "all_high_count": len(all_high),
        "high_56_count": len(high_56),
        "high_base_count": len(high_base),
        "early_alert_count": len(early_alert),
        "excluded_count": len(excluded_machines),
        "digit_hit_count": len(digit_hits),
        "digit_hits": digit_hits,
        "hall_digit_summaries": hall_digits,
        "status_counts": status_counts,
        "halls": halls,
        "target_halls": configured_halls(),
        "hall_counts": hall_counts,
        "machines": machines,
        "results": results,
    }

    published = publish_merged_judgment(summary, output_dir) if not incremental else summary
    _write_per_hall_judgments(summary, output_dir)
    return published


def _hall_names_in(rows: list[dict[str, Any]] | None) -> set[str]:
    return {str(row.get("hall_name")) for row in (rows or []) if row.get("hall_name")}


def combine_judgments(base: dict[str, Any] | None, update: dict[str, Any]) -> dict[str, Any]:
    """update に含まれる店舗だけ差し替え、それ以外の末尾合算は残す。"""
    base_payload = base if isinstance(base, dict) else {}
    update_halls = _hall_names_in(update.get("machines")) | _hall_names_in(update.get("results"))
    base_halls = _hall_names_in(base_payload.get("machines")) | _hall_names_in(base_payload.get("results"))
    if not update_halls:
        return dict(base_payload) if base_halls else dict(update)
    if not base_halls:
        machines = list(update.get("machines") or [])
        results = list(update.get("results") or [])
    else:
        machines = [row for row in (base_payload.get("machines") or []) if row.get("hall_name") not in update_halls]
        results = [row for row in (base_payload.get("results") or []) if row.get("hall_name") not in update_halls]
        machines.extend(update.get("machines") or [])
        results.extend(update.get("results") or [])
    return _summarize_combined(machines, results, update, base_payload)


def _summarize_combined(
    machines: list[dict[str, Any]],
    results: list[dict[str, Any]],
    update: dict[str, Any],
    base: dict[str, Any],
) -> dict[str, Any]:
    from src.analyzer.extras import hall_digit_summaries

    present = _hall_names_in(machines) | _hall_names_in(results)
    halls: list[str] = []
    for name in [*(base.get("halls") or []), *(update.get("halls") or []), *sorted(present)]:
        if name in present and name not in halls:
            halls.append(str(name))
    digit_hits = []
    for item in machines:
        if item.get("judgment_excluded"):
            continue
        for row in item.get("digit_judgments") or []:
            if row.get("status") in (STATUS_ALL_HIGH, "HIGH_56", STATUS_HIGH_BASE):
                digit_hits.append(
                    {
                        "hall_name": item.get("hall_name"),
                        "machine_name": item.get("machine_name"),
                        "judgment_type": item.get("judgment_type"),
                        **{key: row[key] for key in row},
                    }
                )
    status_counts: dict[str, int] = {}
    for item in machines:
        status = str(item.get("status") or "")
        if status:
            status_counts[status] = status_counts.get(status, 0) + 1
    return {
        "judged_at": update.get("judged_at") or base.get("judged_at"),
        "fetched_at": update.get("fetched_at") or base.get("fetched_at"),
        "source": update.get("source") or base.get("source"),
        "masters_path": update.get("masters_path") or base.get("masters_path"),
        "count": len(machines),
        "machine_count": len(machines),
        "unit_count": len(results),
        "all_high_count": sum(1 for row in machines if row.get("is_all_high")),
        "high_56_count": sum(1 for row in machines if row.get("is_high_56")),
        "high_base_count": sum(1 for row in machines if row.get("is_high_base")),
        "early_alert_count": sum(1 for row in machines if row.get("is_early_alert")),
        "excluded_count": sum(1 for row in machines if row.get("judgment_excluded") or row.get("status") == STATUS_EXCLUDED),
        "digit_hit_count": len(digit_hits),
        "digit_hits": digit_hits,
        "hall_digit_summaries": hall_digit_summaries(results),
        "status_counts": status_counts,
        "halls": halls,
        "target_halls": update.get("target_halls") or base.get("target_halls") or halls,
        "hall_counts": _machine_hall_counts(machines, halls),
        "machines": machines,
        "results": results,
    }


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(_dumps(payload) + "\n", encoding="utf-8")
    tmp.replace(path)


def publish_merged_judgment(summary: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    """latest.json へ、今回判定した店舗分だけを結合して末尾合算を作り直す。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    main = output_dir / "latest.json"
    base: dict[str, Any] = {}
    if main.is_file():
        try:
            loaded = json.loads(main.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded = {}
        if isinstance(loaded, dict):
            base = loaded
    merged = combine_judgments(base, summary)
    _atomic_write_json(main, merged)
    return merged


def _write_per_hall_judgments(summary: dict[str, Any], output_dir: Path) -> None:
    seen: set[str] = set()
    halls: list[str] = []
    for row in [*(summary.get("machines") or []), *(summary.get("results") or [])]:
        name = str(row.get("hall_name") or "")
        if name and name not in seen:
            seen.add(name)
            halls.append(name)
    if not halls:
        return
    machines = summary.get("machines") or []
    results = summary.get("results") or []
    for hall_name in halls:
        dest = output_dir / "halls" / hall_dir_slug(hall_name)
        dest.mkdir(parents=True, exist_ok=True)
        hall_machines = [item for item in machines if item.get("hall_name") == hall_name]
        hall_results = [item for item in results if item.get("hall_name") == hall_name]
        counts = (summary.get("hall_counts") or {}).get(hall_name) or {}
        payload = {
            "judged_at": summary.get("judged_at"),
            "fetched_at": summary.get("fetched_at"),
            "source": summary.get("source"),
            "hall_name": hall_name,
            "halls": [hall_name],
            "target_halls": [hall_name],
            "count": len(hall_machines),
            "machine_count": counts.get("machine_count", len(hall_machines)),
            "unit_count": counts.get("count", len(hall_results)),
            "all_high_count": counts.get("all_high_count", 0),
            "high_56_count": counts.get("high_56_count", 0),
            "high_base_count": counts.get("high_base_count", 0),
            "early_alert_count": counts.get("early_alert_count", 0),
            "status_counts": {},
            "hall_counts": {hall_name: counts},
            "machines": hall_machines,
            "results": hall_results,
        }
        (dest / "latest.json").write_text(_dumps(payload), encoding="utf-8")


def judge_all(
    ocr_dir: Path = DEFAULT_OCR_DIR,
    masters_path: Path = DEFAULT_MASTERS_PATH,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> dict[str, Any]:
    records = load_ocr_records(ocr_dir.resolve())
    summary = judge_records(
        records,
        masters_path=masters_path,
        output_dir=output_dir,
        source=str(ocr_dir.resolve()),
    )
    summary["ocr_dir"] = str(ocr_dir.resolve())
    (output_dir.resolve() / "latest.json").write_text(_dumps(summary), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="OCR結果と機種マスターを突合し 3段階判定する")
    parser.add_argument("--ocr-dir", type=Path, default=DEFAULT_OCR_DIR, help="OCR JSON フォルダ")
    parser.add_argument("--masters", type=Path, default=DEFAULT_MASTERS_PATH, help="機種マスターJSON")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="判定結果の出力先")
    args = parser.parse_args()

    summary = judge_all(ocr_dir=args.ocr_dir, masters_path=args.masters, output_dir=args.output_dir)
    print(f"判定機種: {summary.get('machine_count', summary['count'])} 機種（全 {summary.get('unit_count', summary['count'])} 台）")
    print(f"全6数値: {summary['all_high_count']} 機種")
    print(f"全56数値: {summary.get('high_56_count', 0)} 機種")
    print(f"全456数値: {summary['high_base_count']} 機種")
    print("出力: 判定JSON")
    for item in summary["results"]:
        flag = "★" if item["is_all_high"] else ("●" if item.get("is_high_56") else ("◆" if item["is_high_base"] else ("▲" if item["is_early_alert"] else "-")))
        print(
            f"{flag} {item.get('machine_name') or '(機種不明)'} "
            f"[{item['status']}/{item['status_label']}] "
            f"総ゲーム={item.get('total_games')} "
            f"合成確率={item.get('synthetic_probability')} "
            f"基準G={((item.get('criteria') or {}).get('min_games'))} "
            f"設定6={((item.get('criteria') or {}).get('synthetic_probability_threshold'))}"
        )


if __name__ == "__main__":
    main()
