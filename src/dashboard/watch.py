"""監視対象機種と取得スケジュールのダッシュボード用データ。"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from src.analyzer.judgment import PROJECT_ROOT
from src.scraper.halls import configured_halls
from src.scraper.site7 import DEFAULT_OUTPUT_DIR, _load_dotenv
from src.scraper.targets import load_targets, match_target
from src.scheduler import (
    TOKYO,
    is_business_hours,
    lock_is_held,
    next_run_at,
    parse_scheduler_log,
    read_schedule_state,
    schedule_window,
    scheduler_process_alive,
)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _hall_tab_order(halls: list[str]) -> list[str]:
    return list(dict.fromkeys(halls))


def _iso_to_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        return stamp.replace(tzinfo=TOKYO)
    return stamp.astimezone(TOKYO)


def build_watch_payload() -> dict[str, Any]:
    _load_dotenv(PROJECT_ROOT / ".env")
    targets = load_targets()
    halls = _hall_tab_order(configured_halls())
    incoming = _read_json(DEFAULT_OUTPUT_DIR / "incoming.json")
    seen: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in incoming.get("machines") or []:
        hall = str(row.get("hall_name") or "").strip()
        name = str(row.get("machine_name") or row.get("kishu") or "").strip()
        if not hall or not name:
            continue
        target = match_target(name, targets)
        if target is None:
            continue
        rec = seen[hall].setdefault(
            target.id,
            {"site7_names": set(), "unit_count": 0},
        )
        rec["site7_names"].add(name)
        rec["unit_count"] += 1

    hall_lists = []
    for hall in halls:
        machines = []
        for target in targets:
            hit = seen.get(hall, {}).get(target.id) or {"site7_names": set(), "unit_count": 0}
            names = sorted(hit["site7_names"])
            machines.append(
                {
                    "id": target.id,
                    "display_name": target.display_name,
                    "type": target.type,
                    "type_label": "ジャグラー" if target.type == "A" else "AT/スマスロ",
                    "patterns": list(target.patterns),
                    "marked": True,
                    "found_in_last_fetch": bool(names),
                    "matched_names": names,
                    "unit_count": hit["unit_count"],
                }
            )
        hall_lists.append(
            {
                "hall_name": hall,
                "marked_count": len(machines),
                "found_count": sum(1 for item in machines if item["found_in_last_fetch"]),
                "machines": machines,
            }
        )

    log = parse_scheduler_log()
    last_event = log.get("last_event")
    if isinstance(last_event, dict):
        from src.scraper.log_safe import redact

        log = {**log, "last_event": {**last_event, "message": redact(str(last_event.get("message") or ""))}}
    window = schedule_window()
    now = datetime.now(TOKYO)
    state = read_schedule_state()
    try:
        state_pid = int(state.get("pid") or 0) if state else 0
    except (TypeError, ValueError):
        state_pid = 0
    scheduler_alive = scheduler_process_alive(state_pid)
    running = lock_is_held() or bool(state and state.get("running") and scheduler_alive)
    last_attempt = _iso_to_dt(log.get("last_completed_at") or log.get("last_started_at"))
    nxt = next_run_at(now=now, last_attempt=last_attempt)
    if scheduler_alive and state:
        scheduled = _iso_to_dt(state.get("next_run_at"))
        if scheduled and scheduled > now - timedelta(minutes=1):
            nxt = scheduled
    due_now = (not running) and is_business_hours(now) and nxt <= now and not scheduler_alive

    return {
        "halls": hall_lists,
        "watch_count": len(targets),
        "fetched_at": incoming.get("fetched_at"),
        "schedule": {
            **window,
            **log,
            "running": running,
            "scheduler_alive": scheduler_alive,
            "in_business_hours": is_business_hours(now),
            "next_run_at": nxt.isoformat(),
            "due_now": due_now,
            "note": (
                f"常駐タイマーが{window['interval_min']}分間隔で巡回します。"
                f"{window['start']}〜{window['end']} のみ実行し、当日の最終スロットは{window['last_slot_hint']}頃、"
                f"{window['hard_stop']}以降は実行しません。前回未完了時はスキップします。"
            ),
        },
    }
