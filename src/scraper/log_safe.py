"""取得ログから URL、絶対パス、Cookie 値、例外原文を落とす。"""

from __future__ import annotations

import re

_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_ABS_PATH_RE = re.compile(r"(?:/Users|/home|/var|/private|/tmp|/opt)/[^\s\"']+")
_COOKIE_VALUE_RE = re.compile(
    r"(?i)\b(cookie|set-cookie|cf-turnstile-response)\b\s*[:=]\s*\S+"
)
_TRACEBACK_MARK = "Traceback (most recent call last)"


def redact(text: object) -> str:
    """ログ1行から、外部に残したくない断片を除く。"""
    value = " ".join(str(text or "").split())
    if _TRACEBACK_MARK in value:
        from src.scraper.pace import summarize_failure

        return summarize_failure(value)
    value = _URL_RE.sub("（URL）", value)
    value = _ABS_PATH_RE.sub("（パス）", value)
    value = _COOKIE_VALUE_RE.sub(r"\1=（省略）", value)
    return value


def failure_reason(exc: BaseException | str) -> str:
    """例外は定型の理由だけにする。"""
    from src.scraper.pace import summarize_failure

    text = exc if isinstance(exc, str) else f"{type(exc).__name__}: {exc}"
    return summarize_failure(redact(text))


def emit(*args: object, **kwargs: object) -> None:
    """print と同じ呼び出しで、URL と絶対パスをマスクして出す。"""
    sep = kwargs.pop("sep", " ")
    if not isinstance(sep, str):
        sep = " "
    parts = [redact(arg) if isinstance(arg, str) else arg for arg in args]
    print(*parts, sep=sep, **kwargs)
