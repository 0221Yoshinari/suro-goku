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
    hall = hall_by_name(name)
    if hall and hall.prefecture and not body.get("prefecture"):
        body["prefecture"] = hall.prefecture
    dest = (output_dir or DEFAULT_OUTPUT_DIR) / "halls" / hall_dir_slug(name) / "latest.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(body, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    marker = dest.parents[2] / ".sync-mtime"
    marker.write_text(str(time.time()), encoding="utf-8")
    from src.dashboard.judgment_store import save_durable

    save_durable(body, str(body.get("prefecture") or ""))
    return dest


def _hall_index() -> dict[str, tuple[str, str]]:
    """店舗名 → (保存ディレクトリ, 県)。halls.json は1回だけ読む。"""
    from src.scraper.halls import load_halls

    index: dict[str, tuple[str, str]] = {}
    for hall in load_halls(include_disabled=True):
        index[hall.name] = (hall.id or hall_dir_slug(hall.name), (hall.prefecture or "").lower())
    return index


def _disk_payloads(root: Path, wanted: str) -> list[dict[str, Any]]:
    """その県の店舗ファイルだけ読む。全国分を開くと画面の応答が間に合わない。"""
    if not root.is_dir():
        return []
    index = _hall_index()
    paths: list[Path] = []
    if wanted:
        for name, (slug, prefecture) in index.items():
            if prefecture == wanted:
                paths.append(root / slug / "latest.json")
    else:
        paths = sorted(root.glob("*/latest.json"))
    payloads: list[dict[str, Any]] = []
    for path in paths:
        if not path.is_file():
            continue
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(loaded, dict) and str(loaded.get("hall_name") or "").strip():
            payloads.append(loaded)
    return payloads


def read_prefecture_payloads(prefecture: str, output_dir: Path | None = None) -> list[dict[str, Any]]:
    """県内の判定を返す。ディスクが空でも、Postgres に残っていればそれを使う。"""
    wanted = (prefecture or "").strip().lower()
    root = (output_dir or DEFAULT_OUTPUT_DIR) / "halls"
    from src.dashboard.judgment_store import load_durable

    by_name: dict[str, dict[str, Any]] = {}
    for payload in load_durable(wanted):
        name = str(payload.get("hall_name") or "").strip()
        if name:
            by_name[name] = payload
    for payload in _disk_payloads(root, wanted):
        name = str(payload.get("hall_name") or "").strip()
        if name:
            by_name[name] = payload
    return [by_name[name] for name in sorted(by_name)]


def iter_source_halls(prefecture: str, output_dir: Path | None = None):
    """県の原本を1店舗ずつ返す。ディスクがある店舗は、そちらを優先する。"""
    wanted = (prefecture or "").strip().lower()
    root = (output_dir or DEFAULT_OUTPUT_DIR) / "halls"
    index = _hall_index() if root.is_dir() else {}
    seen: set[str] = set()
    from src.dashboard.judgment_store import iter_durable_payloads

    for payload in iter_durable_payloads(wanted):
        name = str(payload.get("hall_name") or "").strip()
        if not name:
            continue
        disk = _read_disk_hall(root, index, name)
        if disk is not None:
            payload = disk
        seen.add(name)
        yield payload
    if not root.is_dir():
        return
    for name, (slug, prefecture_name) in sorted(index.items()):
        if wanted and prefecture_name != wanted:
            continue
        if name in seen:
            continue
        loaded = _read_disk_hall(root, index, name, slug=slug)
        if loaded is not None:
            yield loaded


def _read_disk_hall(
    root: Path,
    index: dict[str, tuple[str, str]],
    name: str,
    slug: str | None = None,
) -> dict[str, Any] | None:
    if not root.is_dir() or not name:
        return None
    folder = slug if slug is not None else (index.get(name) or ("", ""))[0]
    if not folder:
        return None
    path = root / folder / "latest.json"
    if not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(loaded, dict) and str(loaded.get("hall_name") or "").strip():
        return loaded
    return None


def fold_hall_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    """1店舗を、台明細を外す直前の材料に畳む。失敗した店舗は飛ばす。"""
    try:
        name = str(payload.get("hall_name") or "")
        hall_machines: list[dict[str, Any]] = []
        unit_count = 0
        for row in payload.get("machines") or []:
            if not isinstance(row, dict):
                continue
            item = row if row.get("hall_name") or not name else dict(row, hall_name=name)
            hall_machines.append(item)
            unit_count += sum(1 for unit in (item.get("units") or []) if isinstance(unit, dict))
        if not name:
            for item in hall_machines:
                name = str(item.get("hall_name") or "")
                if name:
                    break
        from src.analyzer.juggler import build_juggler_islands

        return {
            "hall_name": name,
            "machines": hall_machines,
            "digits": _digit_boards(payload, name, hall_machines),
            "islands": build_juggler_islands(hall_machines),
            "unit_count": unit_count,
            "judged_at": str(payload.get("judged_at") or ""),
            "fetched_at": str(payload.get("fetched_at") or ""),
        }
    except Exception as exc:
        _note(f"店舗判定の読み込みを飛ばしました: {type(exc).__name__}")
        return None


def _summary_units(machines: list[dict[str, Any]], hall_name: str) -> list[dict[str, Any]]:
    """末尾集計に必要な台だけを揃える。店舗名と判定型が既にある台はコピーしない。"""
    units: list[dict[str, Any]] = []
    for item in machines:
        for unit in item.get("units") or []:
            if not isinstance(unit, dict):
                continue
            if unit.get("hall_name") and unit.get("judgment_type"):
                units.append(unit)
                continue
            copied = dict(unit)
            copied.setdefault("hall_name", item.get("hall_name") or hall_name)
            copied.setdefault("judgment_type", item.get("judgment_type"))
            units.append(copied)
    return units


def _stored_boards(payload: dict[str, Any], hall_name: str) -> list[dict[str, Any]] | None:
    boards = payload.get("hall_digit_summaries")
    if not isinstance(boards, list) or not boards:
        return None
    usable: list[dict[str, Any]] = []
    for board in boards:
        if not isinstance(board, dict) or not isinstance(board.get("digits"), list):
            return None
        board_hall = str(board.get("hall_name") or "")
        if hall_name and board_hall and board_hall != hall_name:
            continue
        if hall_name and not board_hall:
            board = dict(board, hall_name=hall_name)
        usable.append(board)
    return usable or None


def _digit_boards(payload: dict[str, Any], hall_name: str, machines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """店舗に末尾集計が残っていればそれを使う。無ければその店舗だけ計算して記憶する。"""
    stored = _stored_boards(payload, hall_name)
    if stored is not None:
        return stored
    from src.analyzer.extras import hall_digit_summaries

    units = _summary_units(machines, hall_name)
    try:
        boards = hall_digit_summaries(units) if units else []
    except Exception as exc:
        _note(f"末尾集計を飛ばして店舗データは返します: {type(exc).__name__}")
        return []
    if boards:
        payload["hall_digit_summaries"] = boards
    return boards


def assemble_prefecture(prefecture: str, output_dir: Path | None = None) -> dict[str, Any]:
    """県内の店舗ファイルを、画面向けの1レスポンスにまとめる。

    1店舗のファイルが壊れていても、読めた店舗だけを返す。
    """
    payloads = read_prefecture_payloads(prefecture, output_dir)
    if not payloads:
        return {}
    machines: list[dict[str, Any]] = []
    halls: list[str] = []
    digits: list[dict[str, Any]] = []
    unit_count = 0
    judged_at = ""
    fetched_at = ""
    for payload in payloads:
        try:
            name = str(payload.get("hall_name") or "")
            hall_machines: list[dict[str, Any]] = []
            for row in payload.get("machines") or []:
                if not isinstance(row, dict):
                    continue
                item = row if row.get("hall_name") or not name else dict(row, hall_name=name)
                hall_machines.append(item)
                unit_count += sum(1 for unit in (item.get("units") or []) if isinstance(unit, dict))
            if not name:
                for item in hall_machines:
                    name = str(item.get("hall_name") or "")
                    if name:
                        break
            if name:
                halls.append(name)
            judged_at = max(judged_at, str(payload.get("judged_at") or ""))
            fetched_at = max(fetched_at, str(payload.get("fetched_at") or ""))
            machines.extend(hall_machines)
            digits.extend(_digit_boards(payload, name, hall_machines))
        except Exception as exc:
            _note(f"店舗判定の読み込みを飛ばしました: {type(exc).__name__}")
    digits.sort(key=lambda board: str(board.get("hall_name") or ""))
    return {
        "judged_at": judged_at or None,
        "fetched_at": fetched_at or None,
        "machines": machines,
        "halls": halls,
        "hall_digit_summaries": digits,
        "machine_count": len(machines),
        "unit_count": unit_count,
        "count": len(machines),
    }


def judgment_receipt(prefecture: str) -> dict[str, Any]:
    """本番が今返せている店舗数。台明細は読まない。"""
    from src.dashboard.judgment_store import durable_enabled, durable_hall_count, receipt_counts

    selected = (prefecture or "").strip().lower()
    if durable_enabled():
        counted = receipt_counts(selected)
        if counted is not None:
            return counted
        halls = durable_hall_count(selected)
        count = int(halls or 0)
        return {
            "ok": True,
            "prefecture": selected,
            "machine_count": count,
            "hall_count": count,
            "halls": [],
            "fetched_at": None,
            "durable": True,
            "durable_halls": halls,
        }
    view = assemble_prefecture(selected)
    halls = [str(name) for name in (view.get("halls") or []) if str(name).strip()]
    return {
        "ok": True,
        "prefecture": selected,
        "machine_count": int(view.get("machine_count") or 0),
        "hall_count": len(halls),
        "halls": halls,
        "fetched_at": view.get("fetched_at"),
        "durable": False,
        "durable_halls": None,
    }


def reflection_status_path(output_dir: Path | None = None) -> Path:
    return (output_dir or DEFAULT_OUTPUT_DIR) / "reflection_status.json"


def store_reflection_status(payload: dict[str, Any], output_dir: Path | None = None) -> Path:
    """本番が画面に出す、成功店舗と失敗店舗の件数。"""
    reflected = [str(item) for item in (payload.get("reflected") or []) if str(item).strip()]
    failed = [str(item) for item in (payload.get("failed") or []) if str(item).strip() and str(item) not in reflected]
    ok_count = len(reflected)
    failed_count = len(failed)
    from src.scraper.reflection import status_line

    body = {
        "reflected": reflected,
        "failed": failed,
        "ok_count": ok_count,
        "failed_count": failed_count,
        "note": str(payload.get("note") or "") or status_line(ok_count, failed_count),
    }
    dest = reflection_status_path(output_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    marker = dest.parent / ".sync-mtime"
    marker.write_text(str(time.time()), encoding="utf-8")
    return dest


def read_reflection_status(output_dir: Path | None = None) -> dict[str, Any]:
    path = reflection_status_path(output_dir)
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def push_reflection_status(status: dict[str, Any]) -> bool:
    """成功件数と失敗件数を本番の画面ステータスへ送る。"""
    url = sync_url()
    token = sync_token()
    if not url or not token or os.getenv("PYTEST_CURRENT_TEST"):
        return False
    body = {
        "kind": "reflection_status",
        "ok_count": int(status.get("ok_count") or 0),
        "failed_count": int(status.get("failed_count") or 0),
        "note": str(status.get("note") or ""),
        "reflected": list(status.get("reflected") or []),
        "failed": list(status.get("failed") or []),
    }
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
        with urllib.request.urlopen(request, timeout=8) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        _note(f"本番ステータスの送信に失敗しました: {type(exc).__name__}")
        return False


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
    except urllib.error.HTTPError as exc:
        _note(f"本番への判定送信に失敗しました: HTTP {exc.code}")
        return False
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


def remote_status(prefecture: str = "fukuoka") -> dict[str, Any] | None:
    """本番が今持っている県の件数。読めないときは None。"""
    url = sync_url()
    token = sync_token()
    if not url or not token:
        return None
    request = urllib.request.Request(
        f"{url}{SYNC_PATH}?prefecture={prefecture}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            loaded = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def remote_is_empty(prefecture: str = "fukuoka") -> bool:
    """本番のその県が空なら、再送が必要。確認できないときは再送しない。"""
    loaded = remote_status(prefecture)
    if not loaded:
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
