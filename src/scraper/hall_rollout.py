"""無効の新規ホールを、1店ずつ取得してから有効にする。

台データが公開されていない店は叩かない。失敗した店は無効に戻す。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.analyzer.judgment import DEFAULT_OUTPUT_DIR, judge_records
from src.scraper.log_safe import emit, failure_reason
from src.scraper.hall_catalog import save_halls_payload
from src.scraper.halls import HALLS_CONFIG_PATH, PROJECT_ROOT, load_halls_config
from src.scraper.registry import get_source

STATE_PATH = PROJECT_ROOT / "data" / "hall_rollout_state.json"
DEFAULT_PAUSE_SEC = 60.0
DEFAULT_FAIL_PAUSE_SEC = 120.0


def rollout_candidates(halls_payload: dict[str, Any], *, include_unpublished: bool = False) -> list[dict[str, Any]]:
    """まだ有効でない、取得対象のホール。未公開は既定では含めない。"""
    rows: list[dict[str, Any]] = []
    for raw in halls_payload.get("halls") or []:
        if not isinstance(raw, dict) or raw.get("enabled") or raw.get("archived") is True:
            continue
        source = str(raw.get("source") or raw.get("source_type") or "")
        if source not in {"goraggio", "teramoba"}:
            continue
        if raw.get("data_public") is False and not include_unpublished:
            continue
        if not str(raw.get("name") or "").strip():
            continue
        rows.append(raw)
    rows.sort(key=lambda item: (str(item.get("prefecture") or ""), str(item.get("name") or "")))
    return rows


def load_state(path: Path | None = None) -> dict[str, Any]:
    target = path or STATE_PATH
    if not target.is_file():
        return {"completed": [], "failed": {}, "skipped": []}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"completed": [], "failed": {}, "skipped": []}
    if not isinstance(payload, dict):
        return {"completed": [], "failed": {}, "skipped": []}
    payload.setdefault("completed", [])
    payload.setdefault("failed", {})
    payload.setdefault("skipped", [])
    return payload


def save_state(state: dict[str, Any], path: Path | None = None) -> None:
    target = path or STATE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    text = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(target)


def _set_enabled(hall_id: str, enabled: bool, halls_path: Path) -> None:
    payload = json.loads(halls_path.read_text(encoding="utf-8"))
    found = False
    for raw in payload.get("halls") or []:
        if isinstance(raw, dict) and str(raw.get("id") or "") == hall_id:
            raw["enabled"] = enabled
            found = True
            break
    if not found:
        raise RuntimeError(f"ホールが見つかりません: {hall_id}")
    save_halls_payload(payload, halls_path)


def _wait_for_scheduler() -> None:
    from src.scheduler import lock_is_held

    while lock_is_held():
        emit("定期巡回が動いているため、終わるまで待ちます", flush=True)
        time.sleep(20)


async def fetch_one(name: str) -> tuple[bool, list[dict[str, Any]], str]:
    """1店だけ取得する。成功なら台データの配列を返す。"""
    os.environ["SITE7_HALL_NAMES"] = name
    load_halls_config.cache_clear()
    from src.scraper.halls import hall_by_name

    hall = hall_by_name(name)
    if hall is None or not hall.enabled:
        return False, [], "有効な店舗として読めません"
    try:
        result = await get_source(hall.source).fetch()
    except Exception as exc:
        return False, [], str(exc)
    failed = set((result.extra or {}).get("failed_halls") or [])
    machines = [row for row in result.machines if isinstance(row, dict)]
    if name in failed or not machines:
        detail = "台データが0件" if not machines else "取得失敗"
        return False, [], detail
    return True, machines, ""


def merge_hall_judgment(hall_name: str, machines: list[dict[str, Any]], source: str) -> None:
    """1店分の判定を、既存の全店 latest.json に足す。"""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        summary = judge_records(machines, output_dir=Path(tmp), source=source)
    from src.analyzer.judgment import _write_per_hall_judgments
    from src.analyzer.judgment import publish_merged_judgment

    publish_merged_judgment(summary, DEFAULT_OUTPUT_DIR)

    per_hall = {
        "judged_at": summary.get("judged_at"),
        "fetched_at": summary.get("fetched_at"),
        "source": source,
        "halls": [hall_name],
        "target_halls": [hall_name],
        "machines": summary.get("machines") or [],
        "results": summary.get("results") or [],
        "hall_counts": summary.get("hall_counts") or {},
    }
    _write_per_hall_judgments(per_hall, DEFAULT_OUTPUT_DIR)


def run_rollout(
    *,
    limit: int | None = None,
    pause_sec: float = DEFAULT_PAUSE_SEC,
    fail_pause_sec: float = DEFAULT_FAIL_PAUSE_SEC,
    halls_path: Path | None = None,
    state_path: Path | None = None,
    include_unpublished: bool = False,
    retry_failed: bool = False,
) -> dict[str, Any]:
    halls_path = halls_path or HALLS_CONFIG_PATH
    state = load_state(state_path)
    done = set(state.get("completed") or [])
    failed = state.setdefault("failed", {})
    if retry_failed:
        failed.clear()
    payload = json.loads(halls_path.read_text(encoding="utf-8"))
    pending = [
        hall
        for hall in rollout_candidates(payload, include_unpublished=include_unpublished)
        if str(hall.get("id")) not in done and str(hall.get("id")) not in failed
    ]
    if limit is not None:
        pending = pending[: max(0, limit)]
    emit(f"順次有効化: 残り {len(pending)} 店 / 間隔 {pause_sec:.0f} 秒", flush=True)
    enabled = 0
    errors = 0
    for index, hall in enumerate(pending, start=1):
        hall_id = str(hall.get("id"))
        name = str(hall.get("name"))
        source = str(hall.get("source") or "")
        _wait_for_scheduler()
        emit(f"[{index}/{len(pending)}] {name} を有効にして取得します", flush=True)
        _set_enabled(hall_id, True, halls_path)
        ok, machines, reason = asyncio.run(fetch_one(name))
        if not ok:
            _set_enabled(hall_id, False, halls_path)
            failed[hall_id] = reason
            errors += 1
            emit(f"  失敗したため無効に戻しました: {reason}", flush=True)
            save_state(state, state_path)
            time.sleep(fail_pause_sec)
            continue
        try:
            merge_hall_judgment(name, machines, source)
        except Exception as exc:
            emit(f"  判定の結合に失敗しました: {failure_reason(exc)}", flush=True)
        if hall_id not in done:
            state.setdefault("completed", []).append(hall_id)
        enabled += 1
        emit(f"  取得 {len(machines)} 件。有効のままにします", flush=True)
        save_state(state, state_path)
        if index < len(pending):
            time.sleep(pause_sec)
    os.environ.pop("SITE7_HALL_NAMES", None)
    load_halls_config.cache_clear()
    summary = {"enabled": enabled, "failed": errors, "remaining_checked": len(pending)}
    emit(f"完了 有効化={enabled} 失敗={errors}", flush=True)
    return summary


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="無効ホールを1店ずつ取得して有効化する")
    parser.add_argument("--limit", type=int, default=0, help="この回数で止める。0 は残り全部")
    parser.add_argument("--pause", type=float, default=DEFAULT_PAUSE_SEC)
    parser.add_argument("--fail-pause", type=float, default=DEFAULT_FAIL_PAUSE_SEC)
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--include-unpublished", action="store_true", help="台データ未公開の店も試す")
    args = parser.parse_args(argv)
    from src.run_gate import exit_if_halted

    exit_if_halted("ホール展開取得")
    run_rollout(
        limit=args.limit or None,
        pause_sec=args.pause,
        fail_pause_sec=args.fail_pause,
        include_unpublished=args.include_unpublished,
        retry_failed=args.retry_failed,
    )


if __name__ == "__main__":
    main()
