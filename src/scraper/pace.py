"""取得の間隔と同時接続数。

正常な応答が続くあいだは短い間隔で進む。
429、503、タイムアウト、CAPTCHA のときだけ 15秒→30秒→60秒と減速し、
次の正常応答で元の間隔へ戻す。ソースごとに同時接続枠を分ける。
"""

from __future__ import annotations

import os
import random
import threading
from contextlib import contextmanager
from typing import Callable
from typing import Iterator

HEALTHY_MIN_SEC = 0.2
HEALTHY_MAX_SEC = 0.5
BACKOFF_STEPS = (15.0, 30.0, 60.0)
MAX_CONCURRENCY = 12

MOBILE_USER_AGENTS = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
)

DESKTOP_USER_AGENTS = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Safari/605.1.15",
)
GENERIC_HTTP_USER_AGENT = DESKTOP_USER_AGENTS[0]


class AccessPacer:
    """スレッド間で共有する間隔と同時接続の上限。"""

    def __init__(
        self,
        *,
        sleeper: Callable[[float], None] | None = None,
        concurrency: int = MAX_CONCURRENCY,
    ) -> None:
        self._sleeper = sleeper
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(max(1, concurrency))
        self.strikes = 0

    def slot(self) -> threading.BoundedSemaphore:
        return self._slots

    @contextmanager
    def limit(self, wait_sec: float = 15.0) -> Iterator[None]:
        """同時接続枠を借りる。空きがなければ、打ち切り済みの通信を待ち続けない。"""
        if not self._slots.acquire(timeout=max(0.1, float(wait_sec))):
            raise TimeoutError(f"同時接続が {wait_sec:.0f}秒空かなかった")
        try:
            yield
        finally:
            self._slots.release()

    def interval(self) -> float:
        with self._lock:
            if self.strikes:
                return BACKOFF_STEPS[min(self.strikes, len(BACKOFF_STEPS)) - 1]
            return random.uniform(HEALTHY_MIN_SEC, HEALTHY_MAX_SEC)

    def mark_success(self) -> None:
        with self._lock:
            self.strikes = 0

    def mark_pressure(self) -> float:
        with self._lock:
            self.strikes = min(self.strikes + 1, len(BACKOFF_STEPS))
            return BACKOFF_STEPS[self.strikes - 1]

    def sleep(self, delay: float) -> None:
        if delay <= 0:
            return
        if self._sleeper is not None:
            self._sleeper(delay)
            return
        import time

        time.sleep(delay)


_PACERS: dict[str, AccessPacer] = {}
_PACER_LOCK = threading.Lock()


def max_concurrency() -> int:
    raw = (os.getenv("SCRAPER_MAX_WORKERS") or "").strip()
    if not raw:
        return MAX_CONCURRENCY
    try:
        value = int(raw)
    except ValueError:
        return MAX_CONCURRENCY
    return max(1, min(20, value))


def request_gap(pacer: AccessPacer) -> float:
    """通常のリクエスト間隔。遮断時の15秒以上の減速は短く切らない。"""
    gap = pacer.interval()
    if gap >= BACKOFF_STEPS[0]:
        return gap
    return min(gap, HEALTHY_MAX_SEC)


def get_pacer(source: str = "shared") -> AccessPacer:
    """ソースごとに枠を分ける。片方が遅れても、もう片方の枠は減らない。"""
    with _PACER_LOCK:
        pacer = _PACERS.get(source)
        if pacer is None:
            pacer = AccessPacer(concurrency=max_concurrency())
            _PACERS[source] = pacer
        return pacer


def rotate_user_agent(kind: str = "desktop") -> str:
    pool = MOBILE_USER_AGENTS if kind == "mobile" else DESKTOP_USER_AGENTS
    return random.choice(pool)


def is_pressure(status: int | None = None, text: str = "") -> bool:
    """遮断や過負荷の兆候か。通常の 404 や接続拒否は含めない。"""
    if status in {429, 503}:
        return True
    folded = (text or "").casefold()
    tokens = (
        "429",
        "503",
        "too many requests",
        "timed out",
        "タイムアウト",
        "captcha",
        "ロボットではありません",
        "cf-challenge",
        "just a moment",
    )
    return any(token in folded for token in tokens)


def summarize_failure(error: str) -> str:
    """外部通知と画面に出してよい定型の理由だけを返す。"""
    text = " ".join((error or "").split())
    if text in {
        "再試行待ち",
        "取得失敗",
        "429上限",
        "一時停止（503）",
        "タイムアウト（応答なし）",
        "CAPTCHA",
        "ページ構造変更 / 404エラー",
        "接続拒否",
        "要素未発見",
        "台データなし",
    }:
        return text
    folded = text.casefold()
    if "再試行待ち" in text:
        return "再試行待ち"
    if "429" in folded or "too many requests" in folded:
        return "429上限"
    if "503" in folded:
        return "一時停止（503）"
    if "秒以内" in text or "空かなかった" in text or any(token in folded for token in ("timeout", "timed out", "タイムアウト")):
        return "タイムアウト（応答なし）"
    if any(token in folded for token in ("captcha", "ロボットではありません")):
        return "CAPTCHA"
    if "404" in folded:
        return "ページ構造変更 / 404エラー"
    if any(token in folded for token in ("connection refused", "接続拒否", "接続できません")):
        return "接続拒否"
    if any(token in text for token in ("ページ構造", "要素", "見つかりません", "Inertia")):
        return "要素未発見"
    if "0 件" in text or "0件" in text:
        return "台データなし"
    if "メンテナンス中" in text or "店舗ステータス" in text:
        return "メンテナンス中"
    if "メンテナンス" in text:
        return "メンテナンス画面"
    if "復旧案内" in text:
        return "復旧案内を通過できず"
    return "取得失敗"


def public_http_detail(detail: object) -> str:
    """API の応答に、例外本文や内部パスを載せない。"""
    if not isinstance(detail, str):
        return "処理に失敗しました。しばらくしてから再度お試しください。"
    text = " ".join(detail.split())
    folded = text.casefold()
    leaked = ("traceback", 'file "', ".py", "http://", "https://", "exception", "token", "password")
    if not text or len(text) > 80 or any(token in folded for token in leaked):
        return "処理に失敗しました。しばらくしてから再度お試しください。"
    return text


def failure_record(hall: str, error: object) -> dict[str, str]:
    return {"hall": hall, "reason": summarize_failure(str(error))}
