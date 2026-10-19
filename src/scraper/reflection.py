"""成功して画面へ出した店舗と、失敗した店舗を分けて数える。"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from src.analyzer.judgment import DEFAULT_OUTPUT_DIR

_LOCK = threading.Lock()
_LAST_PUSH = 0.0


def status_path() -> Path:
    override = (os.getenv("REFLECTION_STATUS_PATH") or "").strip()
    if override:
        return Path(override)
    return DEFAULT_OUTPUT_DIR / "reflection_status.json"


def status_line(ok: int, failed: int) -> str:
    text = f"成功{ok}店舗 / 失敗{failed}店舗"
    if ok > 0 and failed > 0:
        return f"{text}（成功分は画面反映済み）"
    if ok > 0:
        return f"{text}（画面反映済み）"
    return text


def begin_cycle() -> None:
    with _LOCK:
        _write({"reflected": [], "failed": [], "note": status_line(0, 0)})


def note_reflected(hall_name: str) -> dict[str, Any]:
    name = str(hall_name or "").strip()
    if not name:
        return status_snapshot()
    with _LOCK:
        state = _read()
        reflected = [item for item in state["reflected"] if item != name]
        reflected.append(name)
        failed = [item for item in state["failed"] if item != name]
        state = _store(reflected, failed)
    _maybe_push(state)
    return state


def note_failed(hall_name: str) -> dict[str, Any]:
    """画面に出せた店舗は失敗件数へ戻さない。"""
    name = str(hall_name or "").strip()
    if not name:
        return status_snapshot()
    with _LOCK:
        state = _read()
        if name in state["reflected"]:
            return _public(state)
        if name in state["failed"]:
            return _public(state)
        state = _store(state["reflected"], [*state["failed"], name])
    _maybe_push(state)
    return state


def status_snapshot() -> dict[str, Any]:
    with _LOCK:
        return _public(_read())


def _store(reflected: list[str], failed: list[str]) -> dict[str, Any]:
    note = status_line(len(reflected), len(failed))
    payload = {"reflected": reflected, "failed": failed, "note": note}
    _write(payload)
    return _public(payload)


def _public(payload: dict[str, Any]) -> dict[str, Any]:
    reflected = [str(item) for item in payload.get("reflected") or [] if str(item).strip()]
    failed = [str(item) for item in payload.get("failed") or [] if str(item).strip() and str(item) not in reflected]
    return {
        "reflected": reflected,
        "failed": failed,
        "ok_count": len(reflected),
        "failed_count": len(failed),
        "note": status_line(len(reflected), len(failed)),
    }


def _read() -> dict[str, Any]:
    path = status_path()
    if not path.is_file():
        return {"reflected": [], "failed": []}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"reflected": [], "failed": []}
    if not isinstance(loaded, dict):
        return {"reflected": [], "failed": []}
    reflected = loaded.get("reflected") if isinstance(loaded.get("reflected"), list) else []
    failed = loaded.get("failed") if isinstance(loaded.get("failed"), list) else []
    return {
        "reflected": [str(item) for item in reflected if str(item).strip()],
        "failed": [str(item) for item in failed if str(item).strip()],
    }


def _write(payload: dict[str, Any]) -> None:
    path = status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    try:
        marker = path.parent / ".sync-mtime"
        marker.write_text(str(time.time()), encoding="utf-8")
    except OSError:
        return


def _maybe_push(state: dict[str, Any], *, force: bool = False) -> None:
    global _LAST_PUSH
    if os.getenv("PYTEST_CURRENT_TEST"):
        return
    now = time.monotonic()
    if not force and now - _LAST_PUSH < 15:
        return
    _LAST_PUSH = now
    threading.Thread(target=_push, args=(dict(state),), name="reflection-status", daemon=True).start()


def push_status_now() -> None:
    if os.getenv("PYTEST_CURRENT_TEST"):
        return
    _push(status_snapshot())


def _push(state: dict[str, Any]) -> None:
    try:
        from src.dashboard.judgment_sync import push_reflection_status

        push_reflection_status(state)
    except Exception:
        return
