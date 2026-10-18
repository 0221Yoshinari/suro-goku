"""取得通信のプロキシ設定。未設定のときは直接接続のまま。"""

from __future__ import annotations

import json
import os
import threading
import urllib.request
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import quote

from src.scraper.base import PROJECT_ROOT

DEFAULT_PATH = PROJECT_ROOT / "config" / "proxy_config.json"
_CURSOR = 0
_LOCK = threading.Lock()


def config_path() -> Path:
    raw = (os.getenv("PROXY_CONFIG_PATH") or "").strip()
    return Path(raw) if raw else DEFAULT_PATH


@lru_cache(maxsize=1)
def load_proxy_config() -> dict[str, Any]:
    path = config_path()
    if not path.is_file():
        return {"enabled": False, "rotation": "round_robin", "endpoints": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"enabled": False, "rotation": "round_robin", "endpoints": []}
    if not isinstance(payload, dict):
        return {"enabled": False, "rotation": "round_robin", "endpoints": []}
    endpoints = payload.get("endpoints")
    payload["endpoints"] = [item for item in endpoints if isinstance(item, dict)] if isinstance(endpoints, list) else []
    payload["enabled"] = bool(payload.get("enabled"))
    payload["rotation"] = str(payload.get("rotation") or "round_robin")
    return payload


def reset_proxy_state() -> None:
    """テスト用。読み込みキャッシュとローテーション位置を戻す。"""
    global _CURSOR
    load_proxy_config.cache_clear()
    with _LOCK:
        _CURSOR = 0


def _env(*keys: str) -> str:
    for key in keys:
        value = (os.getenv(key) or "").strip()
        if value:
            return value
    return ""


def _env_endpoint() -> dict[str, Any] | None:
    server = _env("SCRAPER_PROXY_SERVER", "SITE7_PROXY_SERVER")
    host = _env("SCRAPER_PROXY_HOST", "SITE7_PROXY_HOST")
    port = _env("SCRAPER_PROXY_PORT", "SITE7_PROXY_PORT")
    username = _env("SCRAPER_PROXY_USER", "SCRAPER_PROXY_USERNAME", "SITE7_PROXY_USER")
    password = _env("SCRAPER_PROXY_PASS", "SCRAPER_PROXY_PASSWORD", "SITE7_PROXY_PASS")
    protocol = (_env("SCRAPER_PROXY_PROTOCOL", "SITE7_PROXY_PROTOCOL") or "http").lower()
    if protocol in {"socks", "socks5h"}:
        protocol = "socks5"
    if not server and host and port:
        server = f"{protocol}://{host}:{port}"
    if not server:
        return None
    if "://" not in server:
        server = f"{protocol}://{server}"
    return {
        "id": "env",
        "protocol": protocol,
        "server": server,
        "host": host,
        "port": port,
        "username": username,
        "password": password,
    }


def _fill_secret(raw: dict[str, Any], field: str) -> str:
    direct = str(raw.get(field) or "").strip()
    if direct:
        return direct
    env_key = str(raw.get(f"{field}_env") or "").strip()
    if not env_key:
        return ""
    return (os.getenv(env_key) or "").strip()


def _normalize_endpoint(raw: dict[str, Any], index: int) -> dict[str, Any] | None:
    protocol = str(raw.get("protocol") or "http").strip().lower() or "http"
    if protocol in {"socks", "socks5h"}:
        protocol = "socks5"
    host = str(raw.get("host") or "").strip()
    port = str(raw.get("port") or "").strip()
    server = str(raw.get("server") or "").strip()
    if not server and host and port:
        server = f"{protocol}://{host}:{port}"
    if not server:
        return None
    if "://" not in server:
        server = f"{protocol}://{server}"
    endpoint = {
        "id": str(raw.get("id") or f"endpoint-{index + 1}"),
        "protocol": protocol,
        "server": server,
        "host": host,
        "port": port,
    }
    username = _fill_secret(raw, "username")
    password = _fill_secret(raw, "password")
    if username:
        endpoint["username"] = username
    if password:
        endpoint["password"] = password
    return endpoint


def proxy_endpoints() -> list[dict[str, Any]]:
    """使うプロキシ一覧。環境変数の1本があるときはそれだけ。"""
    env_endpoint = _env_endpoint()
    if env_endpoint is not None:
        return [env_endpoint]
    payload = load_proxy_config()
    if not payload.get("enabled"):
        return []
    rows: list[dict[str, Any]] = []
    for index, raw in enumerate(payload.get("endpoints") or []):
        item = _normalize_endpoint(raw, index)
        if item is not None:
            rows.append(item)
    return rows


def next_endpoint() -> dict[str, Any] | None:
    rows = proxy_endpoints()
    if not rows:
        return None
    global _CURSOR
    with _LOCK:
        item = rows[_CURSOR % len(rows)]
        _CURSOR += 1
        return item


def sticky_endpoint(endpoint: dict[str, Any], session_id: str) -> dict[str, Any]:
    """同じ店舗の通信を同じ出口IPに固定する。未対応のプロキシ名ならそのまま返す。"""
    copied = dict(endpoint)
    username = str(copied.get("username") or "")
    token = "".join(ch for ch in session_id if ch.isalnum())[:24]
    if not username or not token or "-session-" in username.casefold():
        return copied
    copied["username"] = f"{username}-session-{token}"
    return copied


def sticky_proxy_url(session_id: str) -> str | None:
    """店舗ごとに Bright Data の出口IPを固定したプロキシURLを返す。"""
    rows = proxy_endpoints()
    if not rows:
        return None
    token = "".join(ch for ch in session_id if ch.isalnum()) or "session"
    base = rows[sum(ord(ch) for ch in token) % len(rows)]
    return endpoint_proxy_url(sticky_endpoint(base, token))


def endpoint_proxy_url(endpoint: dict[str, Any]) -> str:
    server = str(endpoint.get("server") or "").strip()
    username = str(endpoint.get("username") or "")
    password = str(endpoint.get("password") or "")
    if not username or not server or "://" not in server:
        return server
    scheme, _, rest = server.partition("://")
    auth = f"{quote(username, safe='')}:{quote(password, safe='')}@"
    return f"{scheme}://{auth}{rest}"


def playwright_proxy(endpoint: dict[str, Any] | None = None) -> dict[str, str] | None:
    selected = endpoint if endpoint is not None else next_endpoint()
    if not selected:
        return None
    proxy: dict[str, str] = {"server": str(selected.get("server") or "")}
    username = str(selected.get("username") or "")
    password = str(selected.get("password") or "")
    if username:
        proxy["username"] = username
    if password:
        proxy["password"] = password
    if not proxy["server"]:
        return None
    return proxy


def proxy_log_label(proxy: dict[str, str] | None) -> str:
    """ログ用。ホスト名もユーザー名も出さない。"""
    if not proxy:
        return "直接接続"
    return "プロキシ経由"


class RotatingProxyHandler(urllib.request.BaseHandler):
    """リクエストごとに次のプロキシを割り当てる。未設定なら何もしない。"""

    handler_order = 100

    def http_open(self, req):
        return self._open(req, "http")

    def https_open(self, req):
        return self._open(req, "https")

    def _open(self, req, scheme: str):
        selected = next_endpoint()
        if not selected:
            return None
        proxy_url = endpoint_proxy_url(selected)
        handler = urllib.request.ProxyHandler({scheme: proxy_url})
        handler.parent = self.parent
        return getattr(handler, f"{scheme}_open")(req)


def build_client_opener(*handlers: urllib.request.BaseHandler) -> urllib.request.OpenerDirector:
    extra: list[urllib.request.BaseHandler] = []
    if proxy_endpoints():
        extra.append(RotatingProxyHandler())
    return urllib.request.build_opener(*handlers, *extra)


def urlopen(request: urllib.request.Request, *, timeout: float):
    return build_client_opener().open(request, timeout=timeout)
