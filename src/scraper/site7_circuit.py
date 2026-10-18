"""東京マルハン10店舗のサイトセブン巡回。

店舗間隔は 6〜12 分（両端の一様乱数を平均し、中央に寄るゆらぎにする）。
同時に開くブラウザは 1 本。Cookie は既存の storage_state を再利用する。
CAPTCHA・利用停止・ログイン失敗を検知したら、残り店舗へ進まず停止ファイルを書く。
パスワードは .env の SITE7_PASSWORD だけを参照し、このモジュールには置かない。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from src.scraper.halls import PROJECT_ROOT, load_halls
from src.scraper.log_safe import emit, failure_reason
from src.run_gate import require_acquisition

DEFAULT_CONFIG = PROJECT_ROOT / "config" / "site7_tokyo_maruhan.json"
DEFAULT_HALT = PROJECT_ROOT / "data" / "site7" / "circuit_halt.json"
DEFAULT_REPORT = PROJECT_ROOT / "data" / "site7" / "tokyo_circuit_report.json"
DEFAULT_GAP_MIN_SEC = 6 * 60
DEFAULT_GAP_MAX_SEC = 12 * 60


class CircuitHalted(RuntimeError):
    """異常検知済みのため、サイトセブン巡回を始めない。"""


@dataclass(frozen=True)
class CircuitHall:
    id: str
    name: str


@dataclass(frozen=True)
class CircuitPlan:
    halls: tuple[CircuitHall, ...]
    gap_min_sec: float
    gap_max_sec: float


@dataclass
class HallOutcome:
    hall_id: str
    hall_name: str
    status: str
    rows: int = 0
    reason: str = ""


@dataclass
class CircuitResult:
    outcomes: list[HallOutcome] = field(default_factory=list)
    gaps_sec: list[float] = field(default_factory=list)
    stopped: str | None = None
    elapsed_sec: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "stopped": self.stopped,
            "elapsed_sec": round(self.elapsed_sec, 1),
            "gaps_sec": [round(gap, 1) for gap in self.gaps_sec],
            "outcomes": [
                {
                    "hall_id": item.hall_id,
                    "hall_name": item.hall_name,
                    "status": item.status,
                    "rows": item.rows,
                    "reason": item.reason,
                }
                for item in self.outcomes
            ],
        }


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def gap_bounds(payload: dict[str, Any] | None = None) -> tuple[float, float]:
    """店舗間隔（秒）。環境変数が設定ファイルより優先される。"""
    file_cfg = payload or {}
    lo = float(os.getenv("SITE7_CIRCUIT_GAP_MIN_SEC") or file_cfg.get("gap_min_sec") or DEFAULT_GAP_MIN_SEC)
    hi = float(os.getenv("SITE7_CIRCUIT_GAP_MAX_SEC") or file_cfg.get("gap_max_sec") or DEFAULT_GAP_MAX_SEC)
    if hi < lo:
        hi = lo
    return lo, hi


def sample_gap(rng: random.Random, lo: float, hi: float) -> float:
    """6〜12分の範囲で、毎回同じ秒数にならない待ち時間。"""
    if hi < lo:
        hi = lo
    return (rng.uniform(lo, hi) + rng.uniform(lo, hi)) / 2.0


def outcome_reason(exc: BaseException) -> str:
    """レポートに残す短い理由。マイホール未登録を要素欠落にまとめない。"""
    if "マイホール" in str(exc):
        return "マイホール未登録"
    return failure_reason(exc)


def classify_stop(exc: BaseException) -> str | None:
    """巡回全体を止める異常。店舗未登録や 0 件は含めない。"""
    text = str(exc)
    name = type(exc).__name__
    if name == "CaptchaRequired" or "CAPTCHA" in text or "私はロボットではありません" in text:
        return "captcha"
    if any(
        token in text
        for token in (
            "利用を一時的に停止",
            "禁止行為",
            "自動取得ツール",
            "自動取得を検知",
            "プログラムや自動取得",
        )
    ):
        return "restricted"
    if "ログインに失敗" in text:
        return "login_failed"
    return None


def load_circuit_config(path: Path | None = None) -> dict[str, Any]:
    return _read_json(path or DEFAULT_CONFIG)


def build_plan(
    *,
    config_path: Path | None = None,
    hall_ids: list[str] | None = None,
) -> CircuitPlan:
    """設定の店舗 ID を halls.json の正式名へ解決する。未登録 ID は巡回しない。"""
    payload = load_circuit_config(config_path)
    raw_ids = hall_ids
    if raw_ids is None:
        env_ids = (os.getenv("SITE7_CIRCUIT_HALL_IDS") or "").strip()
        if env_ids:
            raw_ids = [part.strip() for part in env_ids.split(",") if part.strip()]
        else:
            raw_ids = [str(item).strip() for item in (payload.get("hall_ids") or []) if str(item).strip()]
    if not raw_ids:
        raise RuntimeError("巡回店舗が config/site7_tokyo_maruhan.json にありません。")
    by_id = {hall.id: hall for hall in load_halls(include_disabled=True)}
    missing = [hall_id for hall_id in raw_ids if hall_id not in by_id]
    if missing:
        raise RuntimeError("halls.json に無い店舗です: " + ", ".join(missing))
    lo, hi = gap_bounds(payload)
    halls = tuple(CircuitHall(id=hall_id, name=by_id[hall_id].name) for hall_id in raw_ids)
    return CircuitPlan(halls=halls, gap_min_sec=lo, gap_max_sec=hi)


def read_halt(path: Path | None = None) -> dict[str, Any] | None:
    payload = _read_json(path or DEFAULT_HALT)
    return payload or None


def write_halt(reason: str, hall_name: str, *, path: Path | None = None) -> Path:
    dest = path or DEFAULT_HALT
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps(
            {
                "reason": reason,
                "hall_name": hall_name,
                "stopped_at": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return dest


def clear_halt(path: Path | None = None) -> bool:
    dest = path or DEFAULT_HALT
    if not dest.is_file():
        return False
    dest.unlink()
    return True


def write_report(result: CircuitResult, path: Path | None = None) -> Path:
    dest = path or DEFAULT_REPORT
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return dest


async def run_circuit(
    plan: CircuitPlan,
    fetch_one: Callable[[str], Awaitable[list]],
    *,
    sleeper: Callable[[float], Awaitable[None]],
    rng: random.Random | None = None,
    halt_path: Path | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> CircuitResult:
    """1店舗ずつ取得し、店舗のあいだだけ長いウェイトを置く。"""
    existing = read_halt(halt_path)
    if existing:
        reason = str(existing.get("reason") or "halted")
        hall = str(existing.get("hall_name") or "")
        raise CircuitHalted(
            f"サイトセブン巡回は停止中です（{reason}{' / ' + hall if hall else ''}）。"
            "確認後に --clear-halt で解除してください。"
        )
    chooser = rng or random.Random()
    started = clock()
    result = CircuitResult()
    for index, hall in enumerate(plan.halls):
        emit(f"[{index + 1}/{len(plan.halls)}] {hall.name}", flush=True)
        try:
            rows = await fetch_one(hall.name)
        except Exception as exc:
            stop = classify_stop(exc)
            if stop:
                result.stopped = stop
                result.outcomes.append(
                    HallOutcome(
                        hall_id=hall.id,
                        hall_name=hall.name,
                        status="stopped",
                        reason=outcome_reason(exc),
                    )
                )
                write_halt(stop, hall.name, path=halt_path)
                emit(f"巡回を停止しました: {stop} / {hall.name}", flush=True)
                break
            result.outcomes.append(
                HallOutcome(
                    hall_id=hall.id,
                    hall_name=hall.name,
                    status="failed",
                    reason=outcome_reason(exc),
                )
            )
            emit(f"  {hall.name} は失敗。次の店舗へ進みます: {outcome_reason(exc)}", flush=True)
        else:
            count = len(rows or [])
            if count:
                result.outcomes.append(
                    HallOutcome(hall_id=hall.id, hall_name=hall.name, status="ok", rows=count)
                )
            else:
                result.outcomes.append(
                    HallOutcome(
                        hall_id=hall.id,
                        hall_name=hall.name,
                        status="failed",
                        reason="0件",
                    )
                )
        if result.stopped or index == len(plan.halls) - 1:
            break
        delay = sample_gap(chooser, plan.gap_min_sec, plan.gap_max_sec)
        result.gaps_sec.append(delay)
        await sleeper(delay)
    result.elapsed_sec = clock() - started
    return result


async def _live_fetch(plan: CircuitPlan, *, halt_path: Path) -> CircuitResult:
    email = (os.getenv("SITE7_EMAIL") or "").strip()
    password = (os.getenv("SITE7_PASSWORD") or "").strip()
    if not email or not password:
        raise RuntimeError("SITE7_EMAIL と SITE7_PASSWORD を .env に設定してください。")

    from src.scraper.base import FetchResult
    from src.scraper.browser import browser_session
    from src.scraper.halls import hall_data_dir, worker_user_agent
    from src.scraper.site7_fetch import _fetch_one_hall, ensure_session, storage_state_path

    storage = storage_state_path()
    storage.parent.mkdir(parents=True, exist_ok=True)

    async def sleeper(delay: float) -> None:
        emit(f"次の店舗まで {delay / 60:.1f} 分待機します", flush=True)
        await asyncio.sleep(delay)

    async with browser_session(storage, user_agent=worker_user_agent(1)) as session:
        page = session.page
        await ensure_session(page, storage, allow_login=True)
        session.save_storage = True

        async def fetch_one(name: str) -> list:
            rows = await _fetch_one_hall(page, name, storage)
            if rows:
                FetchResult(source="site7", hall_name=name, machines=rows).write_incoming(hall_data_dir(name))
            return rows

        return await run_circuit(
            plan,
            fetch_one,
            sleeper=sleeper,
            halt_path=halt_path,
        )


def format_plan(plan: CircuitPlan) -> str:
    names = " → ".join(hall.name for hall in plan.halls)
    return (
        f"対象 {len(plan.halls)} 店舗 / 店舗間隔 {plan.gap_min_sec / 60:.0f}〜{plan.gap_max_sec / 60:.0f} 分 / {names}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="東京マルハン10店舗をサイトセブンから巡回する")
    parser.add_argument("--dry-run", action="store_true", help="ブラウザを起動せず、対象店舗と間隔だけ表示する")
    parser.add_argument("--clear-halt", action="store_true", help="異常停止ファイルを消して、次の巡回を許可する")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args(argv)

    if args.clear_halt:
        removed = clear_halt()
        print("停止ファイルを解除しました" if removed else "停止ファイルはありません")
        if args.dry_run:
            plan = build_plan(config_path=args.config)
            print(format_plan(plan))
        return 0

    require_acquisition("サイトセブン東京巡回")
    plan = build_plan(config_path=args.config)
    print(format_plan(plan), flush=True)
    if args.dry_run:
        return 0

    result = asyncio.run(_live_fetch(plan, halt_path=DEFAULT_HALT))
    path = write_report(result)
    print(f"巡回レポート: {path}", flush=True)
    if result.stopped:
        print(f"異常のため残り店舗は取得していません: {result.stopped}", flush=True)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
