"""Macで書いた店舗判定を、本番の画面が読める場所へ送る。

本番（Render）は data/judgments を持たない。Macが1店舗分のJSONを
POSTし、本番はそのファイルを県ごとに組み立てて返す。
"""

from __future__ import annotations

import hmac
import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from src.analyzer.judgment import DEFAULT_OUTPUT_DIR
from src.scraper.halls import configured_halls, hall_by_name, hall_dir_slug


def _note(message: str) -> None:
    try:
        from src.scraper.log_safe import emit

        emit(message, flush=True)
    except Exception:
        print(message, flush=True)

SYNC_PATH = "/api/internal/hall-judgments"
PRIORITY = ("fukuoka", "tokyo", "osaka", "aichi", "kanagawa", "saitama", "chiba", "hyogo")
_STATE_PATH = DEFAULT_OUTPUT_DIR.parent / "logs" / "judgment_sync_state.json"
_LOCK = threading.Lock()
_STARTED = False


def sync_url() -> str:
    return (os.getenv("JUDGMENT_SYNC_URL") or "").strip().rstrip("/")


def sync_token() -> str:
    return (os.getenv("JUDGMENT_SYNC_TOKEN") or os.getenv("MASTER_PANEL_TOKEN") or "").strip()


def token_matches(presented: str | None) -> bool:
    expected = sync_token()
    candidate = (presented or "").strip()
    if candidate.lower().startswith("bearer "):
        candidate = candidate[7:].strip()
    if not expected or not candidate:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), candidate.encode("utf-8"))


def store_hall_payload(payload: dict[str, Any], output_dir: Path | None = None) -> Path:
    """1店舗分の判定を latest.json として保存する。"""
    name = str(payload.get("hall_name") or "").strip()
    if not name:
        raise ValueError("hall_name がありません")
    body = dict(payload)
    body.pop("results", None)
    dest = (output_dir or DEFAULT_OUTPUT_DIR) / "halls" / hall_dir_slug(name) / "latest.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(body, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    marker = dest.parents[2] / ".sync-mtime"
    marker.write_text(str(time.time()), encoding="utf-8")
    return dest


def read_prefecture_payloads(prefecture: str, output_dir: Path | None = None) -> list[dict[str, Any]]:
    wanted = (prefecture or "").strip().lower()
    root = (output_dir or DEFAULT_OUTPUT_DIR) / "halls"
    payloads: list[dict[str, Any]] = []
    for name in configured_halls():
        hall = hall_by_name(name)
        if wanted and (hall is None or (hall.prefecture or "").lower() != wanted):
            continue
        path = root / hall_dir_slug(name) / "latest.json"
        if not path.is_file():
            continue
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(loaded, dict):
            payloads.append(loaded)
    return payloads


def assemble_prefecture(prefecture: str, output_dir: Path | None = None) -> dict[str, Any]:
    """県内の店舗ファイルを、画面向けの1レスポンスにまとめる。"""
    payloads = read_prefecture_payloads(prefecture, output_dir)
    if not payloads:
        return {}
    machines: list[dict[str, Any]] = []
    units: list[dict[str, Any]] = []
    halls: list[str] = []
    judged_at = ""
    fetched_at = ""
    for payload in payloads:
        name = str(payload.get("hall_name") or "")
        if name:
            halls.append(name)
        judged_at = max(judged_at, str(payload.get("judged_at") or ""))
        fetched_at = max(fetched_at, str(payload.get("fetched_at") or ""))
        for row in payload.get("machines") or []:
            if not isinstance(row, dict):
                continue
            item = row if row.get("hall_name") or not name else dict(row, hall_name=name)
            machines.append(item)
            for unit in item.get("units") or []:
                if isinstance(unit, dict):
                    copied = dict(unit)
                    copied.setdefault("hall_name", item.get("hall_name") or name)
                    copied.setdefault("judgment_type", item.get("judgment_type"))
                    units.append(copied)
    from src.analyzer.extras import hall_digit_summaries

    return {
        "judged_at": judged_at or None,
        "fetched_at": fetched_at or None,
        "machines": machines,
        "halls": halls,
        "hall_digit_summaries": hall_digit_summaries(units) if units else [],
        "machine_count": len(machines),
        "unit_count": len(units),
        "count": len(machines),
    }


def push_hall_payload(payload: dict[str, Any]) -> bool:
    """本番へ1店舗分を送る。送信先が無いときは何もしない。"""
    url = sync_url()
    token = sync_token()
    if not url or not token:
        return False
    body = dict(payload)
    body.pop("results", None)
    request = urllib.request.Request(
        url + SYNC_PATH,
        data=json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        _note(f"本番への判定送信に失敗しました: {type(exc).__name__}")
        return False


def push_hall_file(path: Path) -> bool:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(loaded, dict):
        return False
    return push_hall_payload(loaded)


def _state() -> dict[str, int]:
    if not _STATE_PATH.is_file():
        return {}
    try:
        loaded = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(loaded, dict):
        return {}
    return {str(key): int(value) for key, value in loaded.items() if isinstance(value, int)}


def _write_state(state: dict[str, int]) -> None:
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _STATE_PATH.write_text(json.dumps(state), encoding="utf-8")


def remote_is_empty(prefecture: str = "fukuoka") -> bool:
    """本番のその県が空なら、再送が必要。"""
    url = sync_url()
    if not url:
        return False
    request = urllib.request.Request(f"{url}/api/judgments?prefecture={prefecture}", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            loaded = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return False
    if not isinstance(loaded, dict):
        return False
    return int(loaded.get("machine_count") or 0) == 0


def push_saved_halls() -> int:
    """未送信の判定ファイルを、主要県から順に本番へ送る。"""
    if not sync_url() or not sync_token():
        return 0
    with _LOCK:
        state = _state()
        if remote_is_empty() and state:
            state = {}
            _write_state(state)
        paths = _pending_paths(state)
        sent = 0
        for path in paths:
            if push_hall_file(path):
                state[str(path)] = path.stat().st_mtime_ns
                sent += 1
                if sent % 20 == 0:
                    _write_state(state)
                    _note(f"本番へ判定を送信しました: {sent}店舗")
        if sent:
            _write_state(state)
            _note(f"本番へ判定を送信しました: {sent}店舗")
        return sent


def _pending_paths(state: dict[str, int]) -> list[Path]:
    ranked: list[tuple[int, str, Path]] = []
    for name in configured_halls():
        path = DEFAULT_OUTPUT_DIR / "halls" / hall_dir_slug(name) / "latest.json"
        if not path.is_file():
            continue
        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            continue
        if state.get(str(path)) == stamp:
            continue
        hall = hall_by_name(name)
        prefecture = (hall.prefecture if hall else "") or ""
        rank = PRIORITY.index(prefecture) if prefecture in PRIORITY else len(PRIORITY)
        ranked.append((rank, name, path))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return [path for _rank, _name, path in ranked]


def sync_forever() -> None:
    """巡回プロセスのあいだ、未送信分を本番へ送り続ける。"""
    global _STARTED
    with _LOCK:
        if _STARTED:
            return
        _STARTED = True
    while True:
        try:
            push_saved_halls()
        except Exception as exc:
            _note(f"本番同期を中断しました: {type(exc).__name__}")
        time.sleep(90)


def start_sync_thread() -> None:
    threading.Thread(target=sync_forever, name="judgment-sync", daemon=True).start()
