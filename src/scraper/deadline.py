"""1店舗の取得がハングしても、次の店舗へ進めるための時間制限。"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout

DEFAULT_HALL_TIMEOUT_SEC = 180
DEFAULT_REQUEST_TIMEOUT_SEC = 12
_ABANDONED = 0
_ABANDONED_LOCK = threading.Lock()
_LAST_PROGRESS = time.monotonic()
_PROGRESS_LOCK = threading.Lock()


def hall_timeout_sec() -> float:
    """1店舗の台一覧を取り切る上限。応答がない接続は request_timeout_sec で切る。"""
    raw = (os.getenv("HALL_FETCH_TIMEOUT_SEC") or "").strip()
    if not raw:
        return float(DEFAULT_HALL_TIMEOUT_SEC)
    try:
        value = float(raw)
    except ValueError:
        return float(DEFAULT_HALL_TIMEOUT_SEC)
    return max(30.0, min(240.0, value))


def request_timeout_sec() -> float:
    """応答がない HTTP を切る秒数。10〜15秒に収める。"""
    raw = (os.getenv("HALL_REQUEST_TIMEOUT_SEC") or "").strip()
    if not raw:
        return float(DEFAULT_REQUEST_TIMEOUT_SEC)
    try:
        value = float(raw)
    except ValueError:
        return float(DEFAULT_REQUEST_TIMEOUT_SEC)
    return max(10.0, min(15.0, value))


def note_progress() -> None:
    """店舗の取得が進んだ印。常駐側が、無反応の巡回を打ち切るために見る。"""
    global _LAST_PROGRESS
    with _PROGRESS_LOCK:
        _LAST_PROGRESS = time.monotonic()


def idle_seconds() -> float:
    with _PROGRESS_LOCK:
        return max(0.0, time.monotonic() - _LAST_PROGRESS)


def abandoned_threads() -> int:
    with _ABANDONED_LOCK:
        return _ABANDONED


def run_within(func, timeout: float | None = None, label: str = "店舗"):
    """同期処理を制限時間で打ち切る。制限後も裏の通信は放置し、呼び出し元は先へ進む。"""
    global _ABANDONED
    limit = hall_timeout_sec() if timeout is None else max(1.0, float(timeout))
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hall")
    future = pool.submit(func)
    try:
        return future.result(timeout=limit)
    except FuturesTimeout as exc:
        with _ABANDONED_LOCK:
            _ABANDONED += 1
        raise TimeoutError(f"{label} が {limit:.0f}秒以内に完了しませんでした") from exc
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
