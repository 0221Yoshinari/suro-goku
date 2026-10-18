"""サイトセブン由来データを判定用レコードへ変換する。"""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.scraper.halls import configured_halls
from src.scraper.targets import match_target

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "site7.json"
DEFAULT_EXAMPLE_CONFIG = PROJECT_ROOT / "config" / "site7.example.json"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "site7"
RATIO_RE = re.compile(r"1\s*/\s*(\d+(?:\.\d+)?)")


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
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


@dataclass(frozen=True)
class Site7Config:
    email: str
    password: str
    hall_name: str
    hall_aliases: tuple[str, ...]
    input_json: Path | None

    @property
    def has_credentials(self) -> bool:
        return bool(self.email and self.password)


def load_config(
    config_path: Path = DEFAULT_CONFIG_PATH,
    env_path: Path = PROJECT_ROOT / ".env",
) -> Site7Config:
    _load_dotenv(env_path)
    file_cfg = _read_json(config_path if config_path.is_file() else DEFAULT_EXAMPLE_CONFIG)
    input_raw = os.environ.get("SITE7_INPUT_JSON") or file_cfg.get("input_json")
    halls = configured_halls()
    hall_name = (os.environ.get("SITE7_HALL_NAME") or file_cfg.get("hall_name") or (halls[0] if halls else "")).strip()
    aliases = tuple(str(a) for a in (file_cfg.get("hall_aliases") or []) if str(a).strip())
    return Site7Config(
        email=os.environ.get("SITE7_EMAIL", "").strip(),
        password=os.environ.get("SITE7_PASSWORD", "").strip(),
        hall_name=hall_name,
        hall_aliases=aliases,
        input_json=Path(input_raw) if input_raw else None,
    )


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    if isinstance(value, int):
        return value
    text = str(value).replace(",", "").replace("，", "")
    if text.isdigit():
        return int(text)
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace(",", "").replace("，", "")
    match = RATIO_RE.search(text)
    if match:
        return float(match.group(1))
    try:
        return float(text)
    except ValueError:
        return None


_MASTER_CACHE: list[Any] | None = None


def clear_master_type_cache() -> None:
    """機種マスター保存後、取得時のA/AT判定を読み直す。"""
    global _MASTER_CACHE
    _MASTER_CACHE = None


def _resolved_type(name: str | None, watched: Any) -> str | None:
    """監視対象か機種マスターから A / AT を決める。AT判定を合成確率へ落とさない。"""
    if watched is not None and getattr(watched, "type", None):
        return str(watched.type)
    if not name:
        return None
    global _MASTER_CACHE
    if _MASTER_CACHE is None:
        from src.analyzer.judgment import load_masters

        _MASTER_CACHE = load_masters()
    from src.analyzer.judgment import match_machine

    master = match_machine(name, _MASTER_CACHE)
    return master.type if master else None


def to_judgment_record(row: dict[str, Any], hall_name: str) -> dict[str, Any]:
    """サイトセブンの1行を analyzer が読む fields 形式へ揃える。"""
    name = row.get("machine_name") or row.get("kishu") or row.get("name")
    total_games = _as_int(row.get("total_games") or row.get("games") or row.get("回転数") or row.get("総ゲーム"))
    bb_count = _as_int(row.get("bb_count") or row.get("bb") or row.get("BB"))
    rb_count = _as_int(row.get("rb_count") or row.get("rb") or row.get("RB"))
    art_count = _as_int(row.get("art_count") or row.get("art") or row.get("ART") or row.get("AT"))
    watched = match_target(str(name) if name else None)
    machine_type = _resolved_type(str(name) if name else None, watched)
    first_hit = _as_int(row.get("first_hit_count") or row.get("hits"))
    if machine_type == "A":
        if first_hit is None and bb_count is not None and rb_count is not None:
            first_hit = bb_count + rb_count
        synthetic = None
        if total_games is not None and first_hit:
            synthetic = round(total_games / first_hit, 4)
        if synthetic is None:
            synthetic = _as_float(
                row.get("synthetic_probability") or row.get("合成確率") or row.get("gosei")
            )
    elif machine_type == "AT":
        # AT機の初当りはサイトのRB回数だけ。BB回数・合成確率・ARTへはフォールバックしない。
        first_hit = rb_count if rb_count and rb_count > 0 else None
        synthetic = round(total_games / first_hit, 4) if total_games is not None and first_hit else None
    else:
        if first_hit is None and bb_count is not None and rb_count is not None:
            first_hit = bb_count + rb_count
        synthetic = _as_float(
            row.get("synthetic_probability")
            or row.get("合成確率")
            or row.get("gosei")
        )
    return {
        "source": "site7",
        "source_file": row.get("source_file") or f"{hall_name}:{name}:{row.get('dai_no') or ''}".rstrip(":"),
        "hall_name": hall_name,
        "fields": {
            "machine_name": name,
            "hall_name": hall_name,
            "bb_count": bb_count,
            "rb_count": rb_count,
            "art_count": art_count,
            "dai_no": row.get("dai_no") or row.get("dai") or row.get("台番号"),
            "bb_probability": _as_float(row.get("bb_probability") or row.get("BB確率")),
            "rb_probability": _as_float(row.get("rb_probability") or row.get("RB確率")),
            "synthetic_probability": synthetic,
            "total_games": total_games,
            "first_hit_count": first_hit,
            "history": row.get("history") if isinstance(row.get("history"), list) else None,
            "hits": row.get("hits") if isinstance(row.get("hits"), list) else None,
            "art_games": row.get("art_games") if row.get("art_games") is not None else row.get("at_games"),
            "差玉": _as_int(row.get("差玉") or row.get("substraction") or row.get("diff")),
        },
    }


def load_rows(path: Path) -> tuple[list[dict[str, Any]], str | None]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)], None
    if isinstance(payload, dict):
        fetched_at = payload.get("fetched_at")
        if fetched_at is not None:
            fetched_at = str(fetched_at)
        for key in ("machines", "rows", "results", "data"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)], fetched_at
    raise ValueError(f"機種行の配列が見つかりません: {path}")


def save_snapshot(
    records: list[dict[str, Any]],
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    fetched_at: str | None = None,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "fetched_at": fetched_at or datetime.now(timezone.utc).isoformat(),
        "count": len(records),
        "results": records,
    }
    latest = output_dir / "latest.json"
    latest.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    return latest


def ingest_file(path: Path, hall_name: str, output_dir: Path = DEFAULT_OUTPUT_DIR) -> list[dict[str, Any]]:
    raw_rows, fetched_at = load_rows(path)
    rows = [
        row
        for row in raw_rows
        if match_target(str(row.get("machine_name") or row.get("kishu") or row.get("name") or ""))
        and str(row.get("dai_no") or "").strip() not in ("", "平均", "合計", "計")
    ]
    records = [
        to_judgment_record(row, str(row.get("hall_name") or hall_name))
        for row in rows
    ]
    if fetched_at:
        for record in records:
            record["fetched_at"] = fetched_at
    save_snapshot(records, output_dir, fetched_at=fetched_at)
    return records


def fetch_latest(config: Site7Config | None = None) -> list[dict[str, Any]]:
    """会員サイトへの自動ログインは行わない。JSON入力があれば判定用に正規化する。"""
    cfg = config or load_config()
    source = cfg.input_json
    if source is None:
        fallback = DEFAULT_OUTPUT_DIR / "incoming.json"
        source = fallback if fallback.is_file() else None
    if source is None or not Path(source).is_file():
        raise FileNotFoundError(
            "サイトセブンの入力JSONがありません。"
            "SITE7_INPUT_JSON か data/site7/incoming.json に台データJSONを指定してください。"
        )
    return ingest_file(Path(source), cfg.hall_name)


def main() -> None:
    parser = argparse.ArgumentParser(description="サイトセブン由来JSONを判定用レコードへ変換する")
    parser.add_argument("--input", type=Path, help="入力JSON（機種行の配列、または machines/rows キー）")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    cfg = load_config()
    if args.input:
        records = ingest_file(args.input, cfg.hall_name, args.output_dir)
    else:
        records = fetch_latest(cfg)
        save_snapshot(records, args.output_dir)
    print(f"変換件数: {len(records)}")
    print(f"出力: {(args.output_dir / 'latest.json').resolve()}")


if __name__ == "__main__":
    main()
