"""CapSolver で Cloudflare Turnstile トークンを取る。"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from src.scraper.site7 import PROJECT_ROOT, _load_dotenv

CREATE_URL = "https://api.capsolver.com/createTask"
RESULT_URL = "https://api.capsolver.com/getTaskResult"
POLL_SEC = 3.0
TIMEOUT_SEC = 120.0


class CapSolverError(RuntimeError):
    """CapSolver の解決失敗。"""


def api_key() -> str:
    _load_dotenv(PROJECT_ROOT / ".env")
    return (os.getenv("CAPSOLVER_API_KEY") or "").strip()


def _post(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "tamaya-slot-scraper"},
    )
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:240].decode("utf-8", errors="replace")
        raise CapSolverError(f"CapSolver HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise CapSolverError(f"CapSolver に接続できません: {exc.reason}") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise CapSolverError("CapSolver の応答が JSON ではありません") from exc
    if not isinstance(data, dict):
        raise CapSolverError("CapSolver の応答形式が想定外です")
    if data.get("errorId"):
        code = data.get("errorCode") or "error"
        desc = data.get("errorDescription") or ""
        raise CapSolverError(f"CapSolver {code}: {desc}")
    return data


def solve_turnstile(website_url: str, website_key: str, *, action: str = "scraping_protection") -> str:
    """Turnstile のトークンを返す。website_key はサイトの site_key。"""
    key = api_key()
    if not key:
        raise CapSolverError("CAPSOLVER_API_KEY が未設定です")
    task: dict[str, Any] = {
        "type": "AntiTurnstileTaskProxyLess",
        "websiteURL": website_url,
        "websiteKey": website_key,
    }
    if action:
        task["metadata"] = {"action": action}
    created = _post(CREATE_URL, {"clientKey": key, "task": task})
    task_id = str(created.get("taskId") or "")
    if not task_id:
        raise CapSolverError("CapSolver が taskId を返しませんでした")
    deadline = time.monotonic() + TIMEOUT_SEC
    while time.monotonic() < deadline:
        time.sleep(POLL_SEC)
        result = _post(RESULT_URL, {"clientKey": key, "taskId": task_id})
        status = str(result.get("status") or "")
        if status == "ready":
            solution = result.get("solution") or {}
            token = str(solution.get("token") or "") if isinstance(solution, dict) else ""
            if not token:
                raise CapSolverError("CapSolver の解に token がありません")
            return token
        if status not in ("processing", "idle", ""):
            raise CapSolverError(f"CapSolver の状態が想定外です: {status}")
    raise CapSolverError("CapSolver が時間内に Turnstile を解けませんでした")
