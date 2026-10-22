"""県の画面用JSONを、開くリクエストの外で作る。

店舗の保存が一段落したあと、その県の完成品を1行にする。
画面を開く処理は、その行を返すだけにする。
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

_LOCK = threading.Lock()
_DIRTY: dict[str, float] = {}
_BUILDING: set[str] = set()
_ERRORS: dict[str, str] = {}
_RETRY_AFTER: dict[str, float] = {}
_SERVED: dict[str, tuple[str, float, str]] = {}
_STARTED = False
_EVENT = threading.Event()
_PRIORITY = ("fukuoka", "tokyo", "osaka", "aichi", "kanagawa", "saitama", "chiba", "hyogo")


def schedule(prefecture: str, *, delay: float = 5.0) -> None:
    """完成品の作り直しを予約する。連続保存のあいだは最後の保存から delay 秒待つ。"""
    wanted = (prefecture or "").strip().lower()
    if not wanted:
        return
    from src.dashboard.judgment_store import durable_enabled

    if not durable_enabled():
        return
    with _LOCK:
        due = time.monotonic() + max(0.0, delay)
        retry_at = _RETRY_AFTER.get(wanted, 0.0)
        if due < retry_at:
            due = retry_at
        current = _DIRTY.get(wanted)
        if delay > 0 or current is None or due < current:
            _DIRTY[wanted] = due
        _ensure_thread()
    _EVENT.set()


def warm() -> None:
    """起動時に、原本より古い県の完成品だけを作り直す。福岡を先にする。"""
    from src.dashboard.judgment_store import durable_enabled, hall_source_stamps, snapshot_source_stamps

    if not durable_enabled():
        return
    current = hall_source_stamps()
    saved = snapshot_source_stamps()
    for prefecture in _ordered(list(current)):
        if saved.get(prefecture) != current[prefecture]:
            schedule(prefecture, delay=0)


def served_snapshot(prefecture: str) -> str | None:
    """開くAPIが返す完成品。未作成なら None。県の組み立てはしない。"""
    wanted = (prefecture or "").strip().lower()
    if not wanted:
        return None
    from src.dashboard.judgment_store import durable_enabled, load_prefecture_snapshot

    if not durable_enabled():
        return None
    with _LOCK:
        cached = _SERVED.get(wanted)
    if cached is not None:
        _refresh_if_events_moved(wanted, cached[1])
        return cached[2]
    row = load_prefecture_snapshot(wanted)
    if row is None:
        return None
    payload = str(row["payload"])
    events_mtime = float(row["events_mtime"])
    with _LOCK:
        _SERVED[wanted] = (str(row["source_stamp"]), events_mtime, payload)
    _refresh_if_events_moved(wanted, events_mtime)
    return payload


def health_snapshot() -> dict[str, Any]:
    """完成品の有無と大きさ。本文は含めない。"""
    from src.dashboard.judgment_store import durable_enabled, list_prefecture_snapshot_meta

    if not durable_enabled():
        return {"enabled": False, "building": [], "ready": []}
    with _LOCK:
        building = _ordered(list(_BUILDING))
        waiting = _ordered([name for name in _DIRTY if name not in _BUILDING])
        errors = dict(_ERRORS)
    ready = list_prefecture_snapshot_meta()
    for row in ready:
        error = errors.get(str(row.get("prefecture") or ""))
        if error:
            row["last_error"] = error
    return {"enabled": True, "building": building, "waiting": waiting, "ready": ready}


def _refresh_if_events_moved(prefecture: str, stored_mtime: float) -> None:
    """イベントファイルが変わっていたら、今の完成品は返したまま裏で作り直す。"""
    try:
        from src.dashboard.app import _events_mtime

        if abs(float(stored_mtime) - float(_events_mtime())) <= 0.001:
            return
    except Exception:
        return
    with _LOCK:
        if prefecture in _DIRTY or prefecture in _BUILDING:
            return
    schedule(prefecture, delay=1)


def _ensure_thread() -> None:
    global _STARTED
    if _STARTED:
        return
    _STARTED = True
    threading.Thread(target=_loop, name="prefecture-snapshot", daemon=True).start()


def _ordered(names: list[str]) -> list[str]:
    rank = {name: index for index, name in enumerate(_PRIORITY)}
    return sorted(names, key=lambda name: (rank.get(name, len(_PRIORITY)), name))


def _seconds_until_due() -> float | None:
    with _LOCK:
        if not _DIRTY:
            return None
        now = time.monotonic()
        return max(0.0, min(_DIRTY.values()) - now)


def _pop_due() -> str | None:
    with _LOCK:
        now = time.monotonic()
        ready = [name for name, due in _DIRTY.items() if due <= now]
        if not ready:
            return None
        picked = _ordered(ready)[0]
        _DIRTY.pop(picked, None)
        _BUILDING.add(picked)
        return picked


def _loop() -> None:
    while True:
        delay = _seconds_until_due()
        if delay is None:
            _EVENT.wait()
            _EVENT.clear()
            continue
        if delay > 0:
            _EVENT.wait(delay)
            _EVENT.clear()
            continue
        prefecture = _pop_due()
        if prefecture is None:
            continue
        try:
            _build(prefecture)
            with _LOCK:
                _ERRORS.pop(prefecture, None)
                _RETRY_AFTER.pop(prefecture, None)
        except Exception as exc:
            with _LOCK:
                _ERRORS[prefecture] = type(exc).__name__
                _RETRY_AFTER[prefecture] = time.monotonic() + 15
            print(f"県の完成品を作れませんでした: {prefecture} {type(exc).__name__}", flush=True)
        finally:
            with _LOCK:
                _BUILDING.discard(prefecture)


def _build(prefecture: str) -> None:
    from src.dashboard.app import _empty_payload, _events_mtime, _finish_synced, public_judgment_view
    from src.dashboard.judgment_store import (
        drop_durable_prefecture,
        hall_source_stamps,
        save_prefecture_snapshot,
    )
    from src.dashboard.judgment_sync import assemble_prefecture

    started = time.perf_counter()
    stamp = hall_source_stamps().get(prefecture, "0:0")
    synced = assemble_prefecture(prefecture)
    if synced:
        view = public_judgment_view(_finish_synced(synced, lineup="now"))
    else:
        view = _empty_payload()
    view["snapshot_status"] = "ready"
    view["prefecture"] = prefecture
    payload = json.dumps(view, ensure_ascii=False, separators=(",", ":"))
    build_ms = int((time.perf_counter() - started) * 1000)
    saved = save_prefecture_snapshot(
        prefecture,
        source_stamp=stamp,
        events_mtime=_events_mtime(),
        judged_at=str(view.get("judged_at") or ""),
        fetched_at=str(view.get("fetched_at") or ""),
        events_updated_at=str(view.get("events_updated_at") or ""),
        machine_count=int(view.get("machine_count") or 0),
        unit_count=int(view.get("unit_count") or 0),
        lineup_ready=bool(view.get("lineup_ready")),
        payload=payload,
        build_ms=build_ms,
    )
    drop_durable_prefecture(prefecture)
    if not saved:
        raise RuntimeError("snapshot was not stored")
    with _LOCK:
        _SERVED[prefecture] = (stamp, float(_events_mtime()), payload)
    size_kb = max(1, len(payload.encode("utf-8")) // 1024)
    print(
        f"県の完成品を保存しました: {prefecture} {int(view.get('machine_count') or 0)}機種 {size_kb}KB {build_ms}ms",
        flush=True,
    )
