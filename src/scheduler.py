"""営業時間内の定期取得・判定。"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta, time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

from src.analyzer.judgment import PROJECT_ROOT
from src.notify.discord import notify_error, notify_success
from src.pipeline import run_source
from src.scraper.halls import configured_halls

TOKYO = ZoneInfo("Asia/Tokyo")
DEFAULT_INTERVAL_MIN = 40
DEFAULT_START = "10:50"
DEFAULT_END = "22:30"
HARD_STOP = "23:00"
LOCK_PATH = PROJECT_ROOT / "data" / "logs" / "scheduler.lock"
STATE_PATH = PROJECT_ROOT / "data" / "logs" / "scheduler_state.json"
LOG_DIR = PROJECT_ROOT / "data" / "logs"
DEFAULT_CYCLE_BUDGET_SEC = 5 * 60 * 60
DEFAULT_STALL_SEC = 20 * 60


def _parse_hhmm(value: str) -> dt_time:
    hour, minute = value.strip().split(":", 1)
    return dt_time(int(hour), int(minute))


def interval_minutes() -> int:
    return max(1, int(os.getenv("SCHEDULE_INTERVAL_MIN") or DEFAULT_INTERVAL_MIN))


def cycle_budget_sec() -> float:
    raw = (os.getenv("SCHEDULE_CYCLE_BUDGET_SEC") or "").strip()
    if not raw:
        return float(DEFAULT_CYCLE_BUDGET_SEC)
    try:
        return max(60.0, float(raw))
    except ValueError:
        return float(DEFAULT_CYCLE_BUDGET_SEC)


def stall_sec() -> float:
    raw = (os.getenv("SCHEDULE_STALL_SEC") or "").strip()
    if not raw:
        return float(DEFAULT_STALL_SEC)
    try:
        return max(60.0, float(raw))
    except ValueError:
        return float(DEFAULT_STALL_SEC)


def cycle_should_abort(*, elapsed_sec: float, idle_sec: float, budget_sec: float, stall_limit: float) -> str | None:
    """進みがない、または制限時間を超えた巡回は打ち切る。"""
    if idle_sec >= stall_limit:
        return "idle"
    if elapsed_sec >= budget_sec:
        return "budget"
    return None


def _interval_seconds() -> int:
    return max(60, interval_minutes() * 60)


def _hours() -> tuple[dt_time, dt_time]:
    start = _parse_hhmm(os.getenv("SCHEDULE_START") or DEFAULT_START)
    end = _parse_hhmm(os.getenv("SCHEDULE_END") or DEFAULT_END)
    return start, end


def schedule_window() -> dict:
    start, end = _hours()
    return {
        "start": start.strftime("%H:%M"),
        "end": end.strftime("%H:%M"),
        "hard_stop": HARD_STOP,
        "interval_min": interval_minutes(),
        "timezone": "Asia/Tokyo",
        "last_slot_hint": "22:10",
    }


def lock_is_held() -> bool:
    if not LOCK_PATH.is_file():
        return False
    handle = LOCK_PATH.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    finally:
        handle.close()


def _aware(stamp: datetime) -> datetime:
    if stamp.tzinfo is None:
        return stamp.replace(tzinfo=TOKYO)
    return stamp.astimezone(TOKYO)


def _parse_log_line(line: str) -> tuple[datetime, str] | None:
    text = line.strip()
    if len(text) < 20 or text[4] != "-" or text[10] != " ":
        return None
    try:
        stamp = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TOKYO)
    except ValueError:
        return None
    return stamp, text[20:].strip()


def parse_scheduler_log(path: Path | None = None) -> dict:
    log_path = path or (LOG_DIR / "scheduler.log")
    last_started = None
    last_completed = None
    last_failed = None
    last_skipped = None
    last_event = None
    if log_path.is_file():
        for raw in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
            parsed = _parse_log_line(raw)
            if not parsed:
                continue
            stamp, message = parsed
            last_event = {"at": stamp.isoformat(), "message": message}
            if message.startswith("取得開始"):
                last_started = stamp
            elif message.startswith("判定完了"):
                last_completed = stamp
            elif message.startswith("失敗"):
                last_failed = stamp
            elif "スキップ" in message:
                last_skipped = stamp
    return {
        "last_started_at": last_started.isoformat() if last_started else None,
        "last_completed_at": last_completed.isoformat() if last_completed else None,
        "last_failed_at": last_failed.isoformat() if last_failed else None,
        "last_skipped_at": last_skipped.isoformat() if last_skipped else None,
        "last_event": last_event,
    }


def next_open_at(now: datetime | None = None) -> datetime:
    current = _aware(now or datetime.now(TOKYO))
    start, _end = _hours()
    today_start = datetime.combine(current.date(), start, tzinfo=TOKYO)
    stamp = current.timetz().replace(tzinfo=None)
    if stamp < start:
        return today_start
    return datetime.combine((current + timedelta(days=1)).date(), start, tzinfo=TOKYO)


def next_run_at(now: datetime | None = None, last_attempt: datetime | None = None) -> datetime:
    current = _aware(now or datetime.now(TOKYO))
    interval = timedelta(minutes=interval_minutes())
    if not is_business_hours(current):
        return next_open_at(current)
    if last_attempt is not None:
        due = _aware(last_attempt) + interval
        if due > current and is_business_hours(due):
            return due
        if due > current:
            return next_open_at(due)
    return current


def is_business_hours(now: datetime | None = None) -> bool:
    current = now or datetime.now(TOKYO)
    if current.tzinfo is None:
        current = current.replace(tzinfo=TOKYO)
    else:
        current = current.astimezone(TOKYO)
    start, end = _hours()
    stamp = current.timetz().replace(tzinfo=None)
    if stamp >= _parse_hhmm(HARD_STOP):
        return False
    if start <= end:
        return start <= stamp < end
    return stamp >= start or stamp < end


def write_schedule_state(*, next_run_at: datetime, running: bool) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": os.getpid(),
        "running": running,
        "next_run_at": _aware(next_run_at).isoformat(),
        "updated_at": datetime.now(TOKYO).isoformat(),
        "interval_min": interval_minutes(),
    }
    STATE_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def read_schedule_state() -> dict | None:
    if not STATE_PATH.is_file():
        return None
    try:
        payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def scheduler_process_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _log(message: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = f"{datetime.now(TOKYO).strftime('%Y-%m-%d %H:%M:%S')} {message}"
    print(line, flush=True)
    with (LOG_DIR / "scheduler.log").open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _acquire_lock():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handle = LOCK_PATH.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def _ensure_public_dashboard() -> None:
    from src.scraper.site7 import _load_dotenv
    from src.tunnel import announce_public_url, ensure_public_url, tunnel_enabled

    _load_dotenv(PROJECT_ROOT / ".env")
    if not tunnel_enabled():
        return
    try:
        port = int(os.getenv("DASHBOARD_PORT") or "8000")
        url, created = ensure_public_url(port)
        _log(f"外部公開URL: {url}")
        if created:
            announce_public_url(url)
    except Exception as exc:
        _log(f"外部公開トンネルを開始できませんでした: {exc}")


def run_cycle() -> dict:
    from src.scraper.halls import load_halls_config

    load_halls_config.cache_clear()
    write_schedule_state(next_run_at=datetime.now(TOKYO), running=True)
    _ensure_public_dashboard()
    halls = configured_halls()
    _log(f"取得開始（{len(halls)}店舗）")
    started = time.monotonic()
    from src.scraper.fetch_all import fetch_configured

    fetched = asyncio.run(fetch_configured())
    source_id = fetched.source if fetched.source in {"site7", "teramoba"} else "site7"
    incoming = PROJECT_ROOT / "data" / source_id / "incoming.json"
    if not incoming.is_file():
        incoming = PROJECT_ROOT / "data" / "site7" / "incoming.json"
        source_id = "site7"
    result = run_source(source_id, input_path=incoming)
    judgment = result["judgment"]
    extra = getattr(fetched, "extra", None) or {}
    if isinstance(extra, dict) and extra.get("failed_halls"):
        judgment["failed_halls"] = extra["failed_halls"]
    if isinstance(extra, dict) and extra.get("failure_details"):
        judgment["failure_details"] = extra["failure_details"]
    elapsed = extra.get("elapsed_sec") if isinstance(extra, dict) else None
    if elapsed is None:
        elapsed = time.monotonic() - started
    _log(
        "判定完了 "
        f"件数={judgment.get('count', 0)} "
        f"全6={judgment.get('all_high_count', 0)} "
        f"全56={judgment.get('high_56_count', 0)} "
        f"全456={judgment.get('high_base_count', 0)} "
        f"時間={elapsed:.0f}s"
    )
    if notify_success(judgment, elapsed_sec=elapsed, target_halls=halls):
        _log("Discord通知を送信しました")
    else:
        _log("Discord通知に失敗しました")
    finished = datetime.now(TOKYO)
    upcoming = next_run_at(finished, finished)
    write_schedule_state(next_run_at=upcoming, running=False)
    _log(f"次回予定 {upcoming.strftime('%Y-%m-%d %H:%M')}")
    return result


def _watch_cycle(started: float, stop: threading.Event) -> None:
    """取得本体が固まっても、別スレッドから巡回を終わらせて常駐を再起動する。"""
    from src.scraper.deadline import idle_seconds
    from src.scraper.deadline import note_progress

    note_progress()
    while not stop.wait(30):
        elapsed = time.monotonic() - started
        idle = idle_seconds()
        reason = cycle_should_abort(
            elapsed_sec=elapsed,
            idle_sec=idle,
            budget_sec=cycle_budget_sec(),
            stall_limit=stall_sec(),
        )
        try:
            write_schedule_state(next_run_at=datetime.now(TOKYO), running=True)
        except OSError:
            pass
        if reason == "idle":
            _log(f"巡回が {idle / 60:.0f}分進んでいないため打ち切り、常駐を再起動します")
            os._exit(75)
        if reason == "budget":
            _log(f"巡回が {elapsed / 60:.0f}分を超えたため打ち切り、常駐を再起動します")
            os._exit(75)


def run_once(*, require_hours: bool) -> int:
    from src.run_gate import exit_if_halted

    exit_if_halted("定期取得")
    if require_hours and not is_business_hours():
        start, end = _hours()
        _log(f"営業時間外のためスキップ（{start.strftime('%H:%M')}–{end.strftime('%H:%M')} JST）")
        return 0
    lock = _acquire_lock()
    if lock is None:
        _log("前回の取得が継続中のためスキップ")
        return 0
    stop_watch = threading.Event()
    watcher = threading.Thread(
        target=_watch_cycle,
        args=(time.monotonic(), stop_watch),
        name="cycle-watch",
        daemon=True,
    )
    watcher.start()
    restart_after = False
    try:
        run_cycle()
        return 0
    except Exception as exc:
        _log(f"失敗: {exc}")
        _log(traceback.format_exc().rstrip())
        notify_error(str(exc))
        return 1
    finally:
        stop_watch.set()
        try:
            from src.scraper.x_monitor import sync_x_events

            watched = sync_x_events()
            if not watched.get("skipped"):
                _log(
                    "Xイベント監視 "
                    f"added={watched.get('added', 0)} updated={watched.get('updated', 0)}"
                )
        except Exception as exc:
            _log(f"Xイベント監視に失敗: {exc}")
        from src.scraper.deadline import abandoned_threads

        if abandoned_threads():
            restart_after = True
            _log(
                f"打ち切った通信が {abandoned_threads()} 件残っているため、"
                "次の巡回の前に常駐を再起動します"
            )
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()
        if restart_after:
            os._exit(75)


def _sleep_until(when: datetime) -> None:
    target = _aware(when)
    while True:
        now = datetime.now(TOKYO)
        remaining = (target - now).total_seconds()
        if remaining <= 1:
            return
        write_schedule_state(next_run_at=target, running=False)
        time.sleep(min(15, remaining))


def run_loop() -> int:
    from src.run_gate import exit_if_halted

    exit_if_halted("定期取得")
    interval = _interval_seconds()
    _log(f"定期巡回を常駐開始（間隔 {interval // 60} 分 / 営業時間内のみ）")
    while True:
        try:
            now = datetime.now(TOKYO)
            log = parse_scheduler_log()
            last_raw = log.get("last_completed_at") or log.get("last_started_at")
            last = None
            if last_raw:
                last = datetime.fromisoformat(last_raw)
            nxt = next_run_at(now, last)
            if nxt > now + timedelta(seconds=5):
                _log(f"次回まで待機（{nxt.strftime('%Y-%m-%d %H:%M')}）")
                _sleep_until(nxt)
                continue
            if not is_business_hours(now):
                opening = next_open_at(now)
                _log(f"営業時間外。次回は {opening.strftime('%Y-%m-%d %H:%M')}")
                _sleep_until(opening)
                continue
            write_schedule_state(next_run_at=now, running=True)
            code = run_once(require_hours=True)
            if code != 0:
                _log("今回の巡回は失敗。次の間隔まで待機します")
        except Exception as exc:
            _log(f"巡回ループで例外を捕捉しました。次の間隔まで継続します: {exc}")
            _log(traceback.format_exc().rstrip())
            time.sleep(60)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="設定ホールの取得と判定を定期実行する")
    parser.add_argument("--once", action="store_true", help="1回だけ実行して終了")
    parser.add_argument(
        "--business-hours",
        action="store_true",
        help="営業時間外は実行しない（launchd 向け）",
    )
    parser.add_argument("--loop", action="store_true", help="間隔をおいて繰り返し実行")
    args = parser.parse_args()
    if args.loop:
        raise SystemExit(run_loop())
    raise SystemExit(run_once(require_hours=args.business_hours))


if __name__ == "__main__":
    main()
