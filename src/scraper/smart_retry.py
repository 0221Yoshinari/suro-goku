"""429 とタイムアウトの店舗を、その場では叩かず時間を空けて再試行する。"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from src.scraper.base import PROJECT_ROOT
from src.scraper.pace import is_pressure, summarize_failure

DEFAULT_PATH = PROJECT_ROOT / "data" / "logs" / "smart_retry.json"
BACKOFF_BASE_SEC = 60.0
BACKOFF_CAP_SEC = 1800.0

_LOCK = threading.Lock()
_LEDGER: "SmartRetry | None" = None


def is_deferrable(error: str, status: int | None = None) -> bool:
    """同じ店舗を今すぐ取り直すと悪化する失敗か。"""
    if is_pressure(status, error):
        return True
    folded = (error or "").casefold()
    if "メンテナンス" in (error or ""):
        return True
    return any(token in folded for token in ("秒以内", "空かなかった"))


class SmartRetry:
    def __init__(self, path: Path | None = None, *, clock=time.time) -> None:
        self.path = path or DEFAULT_PATH
        self._clock = clock
        self._items: dict[str, dict[str, Any]] = {}
        self._load()

    def _key(self, hall_id: str, source: str) -> str:
        return f"{source}:{hall_id}"

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        items = payload.get("items") if isinstance(payload, dict) else None
        if isinstance(items, dict):
            self._items = {str(key): value for key, value in items.items() if isinstance(value, dict)}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = {"items": self._items}
        text = json.dumps(body, ensure_ascii=False, indent=2) + "\n"
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(self.path)

    def should_skip(self, hall_id: str, source: str, *, now: float | None = None) -> bool:
        item = self._items.get(self._key(hall_id, source))
        if not item:
            return False
        current = self._clock() if now is None else now
        return float(item.get("next_at") or 0) > current

    def defer(
        self,
        hall_id: str,
        source: str,
        error: object,
        *,
        delay_sec: float | None = None,
        now: float | None = None,
    ) -> float:
        """指数バックオフで次の試行時刻を決め、今回の巡回からは外す。"""
        key = self._key(hall_id, source)
        current = self._clock() if now is None else now
        previous = self._items.get(key) or {}
        attempts = int(previous.get("attempts") or 0) + 1
        delay = min(BACKOFF_CAP_SEC, BACKOFF_BASE_SEC * (2 ** (attempts - 1)))
        if delay_sec is not None:
            delay = min(BACKOFF_CAP_SEC, max(delay, float(delay_sec)))
        self._items[key] = {
            "attempts": attempts,
            "next_at": current + delay,
            "reason": summarize_failure(str(error)),
        }
        self.save()
        return delay

    def mark_ok(self, hall_id: str, source: str) -> None:
        key = self._key(hall_id, source)
        if key not in self._items:
            return
        self._items.pop(key, None)
        self.save()

    def snapshot(self, hall_id: str, source: str) -> dict[str, Any]:
        return dict(self._items.get(self._key(hall_id, source)) or {})


def get_ledger() -> SmartRetry:
    global _LEDGER
    with _LOCK:
        if _LEDGER is None:
            _LEDGER = SmartRetry()
        return _LEDGER


def set_ledger(ledger: SmartRetry | None) -> None:
    global _LEDGER
    with _LOCK:
        _LEDGER = ledger


def partition_halls(halls: list, source: str) -> tuple[list, list]:
    ledger = get_ledger()
    ready: list = []
    waiting: list = []
    for hall in halls:
        if ledger.should_skip(getattr(hall, "id", ""), source):
            waiting.append(hall)
        else:
            ready.append(hall)
    return ready, waiting
