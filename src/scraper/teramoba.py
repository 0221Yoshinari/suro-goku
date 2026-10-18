"""テラモバ2（ホール公式データサイト）からパチスロ全台データを取得する。"""

from __future__ import annotations

import base64
import json
import os
import random
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from src.scraper.base import FetchResult, PROJECT_ROOT
from src.scraper.log_safe import emit, failure_reason
from src.scraper.halls import Hall, format_duration, hall_data_dir, halls_for_source, pause_range
from src.scraper.targets import is_watched

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
INERTIA_PAGE_RE = re.compile(r'data-page="([^"]+)"')
CSRF_RE = re.compile(r'csrf-token" content="([^"]+)"', re.I)
DEFAULT_TIMEOUT = 12
RENDER_WAIT_SEC = 3.0
STORAGE_STATE_PATH = PROJECT_ROOT / "data" / "teramoba" / ".auth" / "storage_state.json"
# 同じ公式ドメインへ同時に当てると CAPTCHA になる。ドメインあたりこの本数まで。
TERAMOBA_DOMAIN_CONCURRENCY = 1
# 「サービス復旧のお知らせとお詫び」の「サイトを閲覧する」が書くCookie。10年。
RECOVERY_DISMISS_COOKIE = "idcf_maintenance_dismissed"
RECOVERY_DISMISS_MAX_AGE = 60 * 60 * 24 * 365 * 10


class TeramobaError(RuntimeError):
    """テラモバ2の取得失敗。"""


class TeramobaMaintenance(TeramobaError):
    """メンテナンス表示。CapSolver は呼ばない。"""


def is_maintenance_page(html: str, page: dict[str, Any] | None = None) -> bool:
    """システム障害・メンテナンス画面なら True。認証の前に判定する。"""
    props: dict[str, Any] = {}
    if isinstance(page, dict):
        raw_props = page.get("props")
        if isinstance(raw_props, dict):
            props = raw_props
        if props.get("showMaintenanceImage") is True:
            return True
        if page.get("component") == "UnsupportedDevicePage" and any(
            token in html for token in ("システム障害", "メンテナンス", "idcf_message")
        ):
            return True
    return any(token in html for token in ("システム障害に関するお詫び", "idcf_message"))


def is_access_interstitial(body: bytes) -> bool:
    """Cloudflare の確認画面。JSON と Inertia の台データページは通す。"""
    text = body.decode("utf-8", errors="replace").lstrip()
    if text.startswith("{") or text.startswith("["):
        return False
    if "data-page=" in text[:8000]:
        return False
    head = text[:1200].casefold()
    return any(token in head for token in ("just a moment", "cf-challenge", "ロボットではありません", "captcha"))


def is_recovery_notice(page: dict[str, Any] | None) -> bool:
    """復旧案内のクッション。メンテナンス停止ではなく、「サイトを閲覧する」で通る。"""
    return str((page or {}).get("component") or "") == "MaintenancePage"


@dataclass(frozen=True)
class TeramobaTarget:
    hall_name: str
    base_url: str
    department: str
    kind_code: str
    hall_id: int | None
    list_url: str
    kind_codes: tuple[str, ...] = ()

    @property
    def origin(self) -> str:
        parsed = urlparse(self.base_url)
        return f"{parsed.scheme}://{parsed.netloc}"


def target_from_hall(hall: Hall) -> TeramobaTarget:
    cfg = dict(hall.source_config or {})
    list_url = str(cfg.get("list_url") or hall.source_url or "").strip()
    parsed = urlparse(list_url) if list_url else None
    base_url = str(cfg.get("base_url") or "").strip()
    if not base_url:
        if parsed and parsed.scheme and parsed.netloc:
            base_url = f"{parsed.scheme}://{parsed.netloc}"
        else:
            raise TeramobaError(f"{hall.name}: teramoba.base_url または source_url が必要です")
    department = str(cfg.get("department") or "").strip()
    if not department and parsed:
        parts = [p for p in (parsed.path or "").split("/") if p]
        department = parts[0] if parts else ""
    if not department:
        raise TeramobaError(f"{hall.name}: teramoba.department が必要です")
    kind_code = str(cfg.get("kind_code") or "").strip()
    if not kind_code and parsed and parsed.query:
        kind_code = (parse_qs(parsed.query).get("kind_code") or [""])[0]
    kind_code = kind_code or "21"
    raw_kinds = cfg.get("kind_codes")
    kind_codes: list[str] = []
    if isinstance(raw_kinds, list):
        for item in raw_kinds:
            code = str(item or "").strip()
            if code and code not in kind_codes:
                kind_codes.append(code)
    if kind_code and kind_code not in kind_codes:
        kind_codes.insert(0, kind_code)
    hall_id_raw = cfg.get("hall_id")
    hall_id = int(hall_id_raw) if hall_id_raw not in (None, "") else None
    if not list_url:
        list_url = f"{base_url.rstrip('/')}/{department}/rack_info_kt?kind_code={kind_code}"
    return TeramobaTarget(
        hall_name=hall.name,
        base_url=base_url.rstrip("/"),
        department=department,
        kind_code=kind_code,
        hall_id=hall_id,
        list_url=list_url,
        kind_codes=tuple(kind_codes),
    )


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    if isinstance(value, int):
        return value
    text = str(value).replace(",", "").replace("，", "").strip()
    if not text or text in ("--", "-", "―"):
        return None
    match = re.search(r"-?\d+", text)
    return int(match.group(0)) if match else None


def _ratio_from_counts(games: int | None, hits: int | None) -> str | None:
    if games is None or not hits:
        return None
    return f"1/{round(games / hits)}"


def _aes_cbc_decrypt(payload: str, key_hex: str, iv_hex: str) -> Any:
    raw = base64.b64decode(payload)
    key_hex = key_hex.strip()
    iv_hex = iv_hex.strip()
    try:
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import unpad

        plain = unpad(AES.new(bytes.fromhex(key_hex), AES.MODE_CBC, bytes.fromhex(iv_hex)).decrypt(raw), 16)
        return json.loads(plain.decode("utf-8"))
    except ImportError:
        pass
    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as handle:
        handle.write(raw)
        path = handle.name
    try:
        result = subprocess.run(
            ["openssl", "enc", "-aes-256-cbc", "-d", "-K", key_hex, "-iv", iv_hex, "-in", path],
            capture_output=True,
            check=False,
        )
    finally:
        Path(path).unlink(missing_ok=True)
    if result.returncode != 0:
        err = (result.stderr or b"").decode("utf-8", errors="replace")[:240]
        raise TeramobaError(f"台データの復号に失敗しました: {err or 'openssl error'}")
    return json.loads(result.stdout.decode("utf-8"))


def _maybe_decrypt(payload: Any, key_hex: str | None, iv_hex: str | None) -> Any:
    if key_hex and iv_hex and isinstance(payload, str) and payload and not payload.lstrip().startswith(("{", "[")):
        return _aes_cbc_decrypt(payload, key_hex, iv_hex)
    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            if key_hex and iv_hex:
                return _aes_cbc_decrypt(payload, key_hex, iv_hex)
            raise
    return payload


def saved_user_agent() -> str:
    """cf_clearance を取ったブラウザと同じ UA を使う。"""
    override = (os.getenv("TERAMOBA_USER_AGENT") or "").strip()
    if override:
        return override
    path = storage_state_path()
    if path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        ua = str(payload.get("user_agent") or "").strip()
        if ua:
            return ua
    return USER_AGENT


def global_session_ready(path: Path | None = None) -> bool:
    """teramoba2.com 向けの共有 Cookie が1組あるか。店舗ごとのキーは要らない。"""
    state_path = path or storage_state_path()
    if not state_path.is_file():
        return False
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    for item in payload.get("cookies") or []:
        if not isinstance(item, dict):
            continue
        domain = str(item.get("domain") or "")
        if "teramoba2.com" in domain and item.get("name") and item.get("value"):
            return True
    return False


def persist_protection_cookies(jar: CookieJar, *, origin: str, path: Path | None = None) -> None:
    """検証で付いた captchaProtectionKey を共有セッションへ書き戻す。"""
    state_path = path or storage_state_path()
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    cookies = [item for item in (payload.get("cookies") or []) if isinstance(item, dict)]
    host = (urlparse(origin).hostname or "").lower()
    index = {
        (str(item.get("domain") or ""), str(item.get("name") or ""), str(item.get("path") or "/")): i
        for i, item in enumerate(cookies)
    }
    for cookie in jar:
        name = cookie.name or ""
        if not name.startswith("captchaProtectionKey[") and name not in {
            "XSRF-TOKEN",
            "laravel_session",
            RECOVERY_DISMISS_COOKIE,
        }:
            continue
        domain = (cookie.domain or host).lstrip(".")
        if host and domain and host != domain and not host.endswith("." + domain) and not domain.endswith(host):
            continue
        item = {
            "name": name,
            "value": cookie.value or "",
            "domain": cookie.domain or host,
            "path": cookie.path or "/",
            "expires": cookie.expires if cookie.expires else -1,
            "httpOnly": False,
            "secure": bool(cookie.secure),
            "sameSite": "Lax",
        }
        key = (item["domain"], item["name"], item["path"])
        if key in index:
            cookies[index[key]] = item
        else:
            index[key] = len(cookies)
            cookies.append(item)
    payload["cookies"] = cookies
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def storage_state_path() -> Path:
    override = (os.getenv("TERAMOBA_STORAGE_STATE") or "").strip()
    return Path(override) if override else STORAGE_STATE_PATH


def load_storage_cookies(jar: CookieJar, *, origin: str, path: Path | None = None) -> int:
    state_path = path or storage_state_path()
    if not state_path.is_file():
        return 0
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    host = (urlparse(origin).hostname or "").lower()
    loaded = 0
    for item in payload.get("cookies") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        value = str(item.get("value") or "")
        if not name:
            continue
        domain = str(item.get("domain") or host or "").lstrip(".")
        if host and domain and host != domain and not host.endswith("." + domain):
            continue
        expires_raw = item.get("expires")
        expires = int(expires_raw) if isinstance(expires_raw, (int, float)) and expires_raw > 0 else None
        cookie = Cookie(
            version=0,
            name=name,
            value=value,
            port=None,
            port_specified=False,
            domain=domain or host,
            domain_specified=True,
            domain_initial_dot=str(item.get("domain") or "").startswith("."),
            path=str(item.get("path") or "/") or "/",
            path_specified=True,
            secure=bool(item.get("secure")),
            expires=expires,
            discard=expires is None,
            comment=None,
            comment_url=None,
            rest={"HttpOnly": "True"} if item.get("httpOnly") else {},
            rfc2109=False,
        )
        jar.set_cookie(cookie)
        loaded += 1
    return loaded


class TeramobaClient:
    def __init__(self, target: TeramobaTarget) -> None:
        self.target = target
        self.jar = CookieJar()
        import secrets

        from src.scraper.proxy_config import sticky_proxy_url

        proxy = sticky_proxy_url(f"{target.hall_name}-{secrets.token_hex(3)}")
        cookie_handler = urllib.request.HTTPCookieProcessor(self.jar)
        if proxy:
            self.opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
                cookie_handler,
            )
        else:
            from src.scraper.proxy_config import build_client_opener

            self.opener = build_client_opener(cookie_handler)
        loaded = load_storage_cookies(self.jar, origin=target.origin)
        if loaded:
            emit(f"  保存済みCookie {loaded} 件を読み込み: {storage_state_path()}", flush=True)
        self.csrf = ""
        self.hall_id = target.hall_id
        self.kind_code = target.kind_code
        self.key = ""
        self.iv = ""
        self.latest_date = ""
        self.history_day = 7
        self.turnstile_token = ""
        self._accept_recovery_notice()
        emit("  復旧案内は「サイトを閲覧する」と同じCookieで通過します", flush=True)
        if self.hall_id:
            self._set_cookie(f"agreeServiceTerm[{self.hall_id}]", "true")

    def _headers(self, *, json_api: bool = False, referer: str | None = None) -> dict[str, str]:
        headers = {
            "User-Agent": saved_user_agent(),
            "Accept": "application/json, text/plain, */*" if json_api else "text/html,application/xhtml+xml",
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        }
        headers["Referer"] = referer or self.target.list_url
        if self.turnstile_token:
            headers["cf-turnstile-response"] = self.turnstile_token
        if json_api:
            headers["X-Requested-With"] = "XMLHttpRequest"
            if self.csrf:
                headers["X-CSRF-TOKEN"] = self.csrf
        return headers

    def _request(
        self,
        url: str,
        *,
        json_api: bool = False,
        referer: str | None = None,
        data: bytes | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> bytes:
        headers = self._headers(json_api=json_api or data is not None, referer=referer)
        if data is not None:
            headers["Content-Type"] = "application/json"
            token = self._xsrf_token()
            if token:
                headers["X-XSRF-TOKEN"] = token
        if extra_headers:
            headers.update(extra_headers)
        req = urllib.request.Request(url, data=data, headers=headers)
        from src.scraper.pace import get_pacer, is_pressure, request_gap

        pacer = get_pacer("teramoba")
        pacer.sleep(request_gap(pacer))
        with pacer.limit(wait_sec=DEFAULT_TIMEOUT):
            try:
                with self.opener.open(req, timeout=DEFAULT_TIMEOUT) as response:
                    body = response.read()
                    status = getattr(response, "status", 200)
            except urllib.error.HTTPError as exc:
                if is_pressure(exc.code):
                    pacer.mark_pressure()
                raise TeramobaError(f"HTTP {exc.code}") from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                reason = str(getattr(exc, "reason", exc))
                if is_pressure(None, reason):
                    pacer.mark_pressure()
                    if any(token in reason.casefold() for token in ("timeout", "timed out")):
                        raise TeramobaError("タイムアウト") from exc
                raise TeramobaError("接続できません") from exc
            if is_pressure(status):
                pacer.mark_pressure()
                raise TeramobaError(f"HTTP {status}")
            if is_access_interstitial(body):
                pacer.mark_pressure()
                raise TeramobaError("CAPTCHA")
            pacer.mark_success()
            return body
        raise TeramobaError("接続できません")

    def _xsrf_token(self) -> str:
        raw = self._cookie_value("XSRF-TOKEN")
        if not raw:
            return ""
        return urllib.parse.unquote(raw)

    def _set_cookie(self, name: str, value: str) -> None:
        parsed = urlparse(self.target.origin)
        cookie = Cookie(
            version=0,
            name=name,
            value=value,
            port=None,
            port_specified=False,
            domain=parsed.hostname or "",
            domain_specified=True,
            domain_initial_dot=False,
            path="/",
            path_specified=True,
            secure=parsed.scheme == "https",
            expires=None,
            discard=True,
            comment=None,
            comment_url=None,
            rest={"SameSite": "Lax"},
            rfc2109=False,
        )
        self.jar.set_cookie(cookie)

    def _cookie_value(self, name: str) -> str | None:
        host = (urlparse(self.target.origin).hostname or "").lower()
        for cookie in self.jar:
            if cookie.name == name:
                domain = (cookie.domain or "").lstrip(".").lower()
                if not host or not domain or host == domain or host.endswith("." + domain):
                    return cookie.value
        return None

    def _parse_html(self, html: str) -> dict[str, Any]:
        csrf_match = CSRF_RE.search(html)
        if csrf_match:
            self.csrf = csrf_match.group(1)
        hall_id_match = re.search(r'name="hall_id" content=(\d+)', html)
        if hall_id_match:
            self.hall_id = int(hall_id_match.group(1))
        page_match = INERTIA_PAGE_RE.search(html)
        if not page_match:
            if "protection_redirect" in html or "利用規約" in html:
                raise TeramobaError("利用規約 / 保護ページに遷移したため台データを取得できませんでした")
            raise TeramobaError("Inertia のデータブロックが見つかりません。ページ構造が変わった可能性があります")
        payload = json.loads(unescape(page_match.group(1)))
        return payload if isinstance(payload, dict) else {}

    def _accept_recovery_notice(self) -> None:
        """「サイトを閲覧する」と同じCookieを保存して、案内ページを通り抜ける。"""
        parsed = urlparse(self.target.origin)
        cookie = Cookie(
            version=0,
            name=RECOVERY_DISMISS_COOKIE,
            value="1",
            port=None,
            port_specified=False,
            domain=parsed.hostname or "",
            domain_specified=True,
            domain_initial_dot=False,
            path="/",
            path_specified=True,
            secure=parsed.scheme == "https",
            expires=int(time.time()) + RECOVERY_DISMISS_MAX_AGE,
            discard=False,
            comment=None,
            comment_url=None,
            rest={"SameSite": "None"},
            rfc2109=False,
        )
        self.jar.set_cookie(cookie)

    def _read_html_page(self, url: str, *, referer: str | None = None) -> tuple[str, dict[str, Any]]:
        html = self._request(url, referer=referer).decode("utf-8", errors="replace")
        page = self._parse_html(html)
        if not is_recovery_notice(page):
            return html, page
        emit("  復旧案内を検出したため、「サイトを閲覧する」を通過します", flush=True)
        self._accept_recovery_notice()
        try:
            persist_protection_cookies(self.jar, origin=self.target.origin)
        except OSError as exc:
            emit(f"  復旧案内のCookieを保存できませんでした: {failure_reason(exc)}", flush=True)
        html = self._request(url, referer=referer).decode("utf-8", errors="replace")
        page = self._parse_html(html)
        if is_recovery_notice(page):
            raise TeramobaError("復旧案内を通過したあとも台データ画面に進めませんでした")
        return html, page

    def open_list_page(self) -> dict[str, Any]:
        html, page = self._read_html_page(self.target.list_url)
        if is_maintenance_page(html, page):
            emit("  店舗ステータス：メンテナンス中（CapSolver は使いません）", flush=True)
            raise TeramobaMaintenance("店舗ステータス：メンテナンス中")
        props = page.get("props") or {}
        info = props.get("hall_informations") or {}
        if not self.hall_id:
            raw = info.get("hall_id")
            if raw not in (None, ""):
                self.hall_id = int(raw)
        if info.get("history_day"):
            self.history_day = int(info["history_day"])
        if props.get("unauthorizedIp"):
            raise TeramobaError("IP 制限によりテラモバ2へアクセスできません")
        return props

    def post_json(self, path: str, payload: dict[str, Any], *, referer: str | None = None) -> Any:
        url = path if path.startswith("http") else f"{self.target.origin}{path}"
        raw = self._request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            referer=referer,
        )
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            snippet = text[:180].replace("\n", " ")
            raise TeramobaError(f"JSON を解析できません {url}: {snippet}") from exc

    def agree_terms(self) -> None:
        if not self.hall_id:
            raise TeramobaError("hall_id を特定できませんでした")
        self._set_cookie(f"agreeServiceTerm[{self.hall_id}]", "true")
        challenge = self.get_json(f"/n-api/common/captcha/challenge?hall_id={self.hall_id}")
        if isinstance(challenge, dict) and challenge.get("should_verify"):
            self._solve_turnstile(challenge)

    def _solve_turnstile(self, challenge: dict[str, Any]) -> None:
        from src.scraper.capsolver import CapSolverError, solve_turnstile

        providers = challenge.get("providers") or []
        site_key = ""
        provider = "turnstile"
        for item in providers:
            if isinstance(item, dict) and item.get("provider") == "turnstile" and item.get("site_key"):
                site_key = str(item["site_key"])
                provider = "turnstile"
                break
        if not site_key:
            for item in providers:
                if isinstance(item, dict) and item.get("site_key"):
                    site_key = str(item["site_key"])
                    provider = str(item.get("provider") or provider)
                    break
        if not site_key:
            raise TeramobaError("Turnstile の site_key が challenge にありません")
        emit(f"  Turnstile を CapSolver で更新します hall_id={self.hall_id}", flush=True)
        try:
            token = solve_turnstile(self.target.list_url, site_key, action="scraping_protection")
        except CapSolverError as exc:
            raise TeramobaError(f"Turnstile の自動突破に失敗しました: {exc}") from exc
        self._install_turnstile_token(token)
        result = self.post_json(
            "/n-api/common/captcha/verify",
            {"hall_id": self.hall_id, "provider": provider, "token": token},
        )
        ok = isinstance(result, dict) and result.get("result") is True
        if not ok:
            raise TeramobaError(f"captcha 検証が拒否されました: {result}")
        persist_protection_cookies(self.jar, origin=self.target.origin)
        emit(f"  captchaProtectionKey[{self.hall_id}] を更新しました", flush=True)
        self._wait_for_rendered_catalog()

    def _install_turnstile_token(self, token: str) -> None:
        """検証後の機種一覧・台データ通信へ、同じトークンを Cookie とヘッダーで載せる。"""
        self.turnstile_token = token
        self._set_cookie("cf-turnstile-response", token)
        emit("  cf-turnstile-response を Cookie とリクエストヘッダーへセットしました", flush=True)

    def _wait_for_rendered_catalog(self) -> None:
        """トークン反映後、一覧の JavaScript 描画が終わるまで待ってから読み直す。"""
        emit(f"  トークン反映後、描画完了まで {RENDER_WAIT_SEC:.0f}秒待ちます", flush=True)
        time.sleep(RENDER_WAIT_SEC)
        try:
            self.open_list_page()
        except TeramobaError as exc:
            emit(f"  一覧の再読込をスキップ: {failure_reason(exc)}", flush=True)
        time.sleep(RENDER_WAIT_SEC)

    def resolve_slot_kind(self) -> str:
        """店舗ごとのスロット種別。設定の kind_code は一覧が空のときの予備。"""
        codes = self.resolve_slot_kinds()
        return codes[0] if codes else self.kind_code

    def resolve_slot_kinds(self) -> list[str]:
        """設定に複数 kind_code がある店舗は、そのスロット種別をすべて取る。"""
        configured = [code for code in self.target.kind_codes if code]
        if len(configured) > 1:
            self.kind_code = configured[0]
            emit(f"  スロット種別: kind_code={','.join(configured)}", flush=True)
            return configured
        if not self.hall_id:
            return configured or [self.kind_code]
        payload = self.get_json(f"/n-api/rack_info/kashidama_kind_list?hall_id={self.hall_id}")
        items = payload.get("kashidama_items") if isinstance(payload, dict) else None
        slots = [item for item in (items or []) if isinstance(item, dict) and str(item.get("ps_kind")) == "2"]
        preferred = [item for item in slots if "46" in str(item.get("kind_name") or "")]
        if configured:
            preferred = [item for item in slots if str(item.get("kind_code") or "") in configured] or preferred
        chosen = (preferred or slots or [None])[0]
        code = str((chosen or {}).get("kind_code") or "").strip()
        if code:
            self.kind_code = code
            emit(f"  スロット種別: {chosen.get('kind_name')} kind_code={code}", flush=True)
            return [code]
        return configured or [self.kind_code]

    def get_json(self, path: str, *, referer: str | None = None) -> Any:
        url = path if path.startswith("http") else f"{self.target.origin}{path}"
        raw = self._request(url, json_api=True, referer=referer)
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            snippet = text[:180].replace("\n", " ")
            raise TeramobaError(f"JSON を解析できません {url}: {snippet}") from exc

    def load_session_keys(self, machine_name_enc: str) -> None:
        url = (
            f"{self.target.origin}/{self.target.department}/standlist_slot/"
            f"?kind_code={urllib.parse.quote(self.kind_code)}"
            f"&machine_name={machine_name_enc}"
        )
        html, page = self._read_html_page(url, referer=self.target.list_url)
        if (page.get("component") or "") == "protection_redirect":
            raise TeramobaError("保護ページを解除できませんでした（Cookie または CAPTCHA）")
        props = page.get("props") or {}
        data = props.get("data") or {}
        self.key = str(data.get("key") or "")
        self.iv = str(data.get("iv") or "")
        self.latest_date = str(props.get("latest_date") or "")
        info = props.get("hall_informations") or {}
        if info.get("history_day"):
            self.history_day = int(info["history_day"])
        if not self.latest_date:
            raise TeramobaError("最新営業日（latest_date）がページから取れませんでした")
        if not self.key or not self.iv:
            emit("  暗号化キーなし。平文JSONとして取得します。", flush=True)

    def machine_index(self) -> list[dict[str, Any]]:
        path = (
            f"/n-api/rack_info/search_kind?hall_id={self.hall_id}"
            f"&kind_code={urllib.parse.quote(self.kind_code)}&place="
        )
        payload = self.get_json(path)
        if not isinstance(payload, list):
            raise TeramobaError(f"機種一覧の形式が想定外です: {type(payload).__name__}")
        return [row for row in payload if isinstance(row, dict)]

    def machine_units(self, machine_name_enc: str) -> dict[str, Any]:
        path = (
            f"/n-api/rack_info/machine_list?hall_id={self.hall_id}"
            f"&kind_code={urllib.parse.quote(self.kind_code)}"
            f"&machine_name={machine_name_enc}"
            f"&target_date={urllib.parse.quote(self.latest_date)}"
            f"&disp=2&place=&history_day={self.history_day}"
        )
        payload = self.get_json(path)
        decrypted = _maybe_decrypt(payload, self.key or None, self.iv or None)
        if not isinstance(decrypted, dict):
            raise TeramobaError("台一覧の復号結果がオブジェクトではありません")
        return decrypted

    def machine_history(self, machine_name_enc: str) -> dict[str, Any] | None:
        path = (
            f"/n-api/rack_info/machine_bb_history?hall_id={self.hall_id}"
            f"&kind_code={urllib.parse.quote(self.kind_code)}"
            f"&machine_name={machine_name_enc}"
            f"&history_day={self.history_day}&place="
        )
        try:
            payload = self.get_json(path)
        except TeramobaError as exc:
            emit(f"    履歴取得失敗: {failure_reason(exc)}", flush=True)
            return None
        try:
            decrypted = _maybe_decrypt(payload, self.key or None, self.iv or None)
        except TeramobaError:
            decrypted = payload
        return decrypted if isinstance(decrypted, dict) else None


_HIT_LIST_KEYS = ("hits", "hit_history", "bonus_history", "atari_history", "bonus_list", "signals", "atari")
_GAME_FIELD_MARKERS = ("game", "span", "start", "end", "kind", "signal", "atari", "art_game", "at_game")


def _keep_measured_fields(item: dict[str, Any]) -> dict[str, Any]:
    """当り1件に付いた消化G・区間・種別だけ残す。回数そのものはゲーム数にしない。"""
    kept: dict[str, Any] = {}
    for key, value in item.items():
        if key in {"day", "date", "bounus_1", "bonus_1", "bounus_2", "bonus_2", "bounus_3", "bonus_3"}:
            continue
        if key in _HIT_LIST_KEYS and isinstance(value, list):
            kept[key] = [row for row in value if isinstance(row, dict)]
            continue
        if isinstance(value, (dict, list)):
            continue
        lowered = str(key).lower()
        if any(marker in lowered for marker in _GAME_FIELD_MARKERS):
            kept[key] = value
    return kept


def _history_for_rack(history: dict[str, Any] | None, rack_no: Any) -> list[dict[str, Any]] | None:
    if not history:
        return None
    rows = history.get(str(rack_no))
    if rows is None:
        rows = history.get(rack_no)
    if not isinstance(rows, list):
        return None
    out: list[dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        day = item.get("day") or item.get("date")
        measured = _keep_measured_fields(item)
        if not day and not measured:
            continue
        row: dict[str, Any] = {
            "day": day,
            "bb": _as_int(item.get("bounus_1") if "bounus_1" in item else item.get("bonus_1")),
            "rb": _as_int(item.get("bounus_2") if "bounus_2" in item else item.get("bonus_2")),
            "art": _as_int(item.get("bounus_3") if "bounus_3" in item else item.get("bonus_3")),
        }
        row.update(measured)
        out.append(row)
    return out or None


def _normalize_unit(unit: dict[str, Any], hall_name: str, history: dict[str, Any] | None) -> dict[str, Any] | None:
    dai = unit.get("rack_no")
    if dai in (None, "", "平均", "合計", "計"):
        return None
    name = str(unit.get("machine_name") or "").strip()
    if not name:
        return None
    games = _as_int(unit.get("all_game_count") or unit.get("total_games"))
    bb = _as_int(unit.get("bonus_1"))
    rb = _as_int(unit.get("bonus_2"))
    art = _as_int(unit.get("bonus_3"))
    row: dict[str, Any] = {
        "machine_name": name,
        "dai_no": str(dai),
        "total_games": games,
        "bb": bb,
        "rb": rb,
        "合成確率": _ratio_from_counts(games, (bb or 0) + (rb or 0) or None),
        "BB確率": _ratio_from_counts(games, bb),
        "RB確率": _ratio_from_counts(games, rb),
        "art": art,
        "hall_name": hall_name,
    }
    diff = _as_int(unit.get("substraction"))
    if diff is None:
        diff = _as_int(unit.get("sadama") if unit.get("sadama") is not None else unit.get("差玉"))
    if diff is not None:
        row["差玉"] = diff
    hist = _history_for_rack(history, dai)
    if hist:
        row["history"] = hist
    for key in _HIT_LIST_KEYS:
        rows = unit.get(key)
        if isinstance(rows, list) and rows:
            row["hits"] = [item for item in rows if isinstance(item, dict)]
            break
    for key in ("art_games", "at_games", "art_game_count", "at_game_count"):
        if unit.get(key) not in (None, ""):
            row[key] = unit.get(key)
            break
    return row


def fetch_teramoba_hall(hall: Hall) -> list[dict[str, Any]]:
    target = target_from_hall(hall)
    client = TeramobaClient(target)
    emit(f"テラモバ2へアクセス: {target.list_url}", flush=True)
    client.open_list_page()
    client.agree_terms()
    machines: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    kinds = client.resolve_slot_kinds()
    for kind_index, kind in enumerate(kinds, start=1):
        client.kind_code = kind
        if len(kinds) > 1:
            emit(f"  種別 {kind_index}/{len(kinds)} kind_code={kind}", flush=True)
        models = client.machine_index()
        if not models:
            emit(f"  kind_code={kind} は 0 件のためスキップ", flush=True)
            continue
        watched = [m for m in models if is_watched(str(m.get("machine_name") or ""))]
        skipped = len(models) - len(watched)
        emit(
            f"スロット機種数: {len(models)} / 監視対象: {len(watched)}（除外 {skipped}） hall_id={client.hall_id} kind_code={kind}",
            flush=True,
        )
        if not watched:
            continue
        first_enc = str(watched[0].get("machine_name_enc") or urllib.parse.quote(str(watched[0].get("machine_name") or "")))
        client.load_session_keys(first_enc)
        emit(f"  対象日: {client.latest_date}", flush=True)
        for index, model in enumerate(watched, start=1):
            label = str(model.get("machine_name") or "")
            enc = str(model.get("machine_name_enc") or urllib.parse.quote(label))
            emit(f"  [{index}/{len(watched)}] {label}", flush=True)
            try:
                payload = client.machine_units(enc)
            except TeramobaError as exc:
                emit(f"    一覧を開けませんでした: {failure_reason(exc)}", flush=True)
                continue
            history = None
            try:
                history = client.machine_history(enc)
            except TeramobaError as exc:
                emit(f"    履歴スキップ: {failure_reason(exc)}", flush=True)
            units = [payload[key] for key in payload if key != "sum" and isinstance(payload.get(key), dict)]
            rows = []
            for unit in units:
                row = _normalize_unit(unit, hall.name, history)
                if not row:
                    continue
                key = (row["machine_name"], row["dai_no"])
                if key in seen:
                    continue
                seen.add(key)
                rows.append(row)
            art_n = sum(1 for row in rows if row.get("art"))
            emit(f"    取得 {len(rows)} 台 / ARTあり {art_n}", flush=True)
            machines.extend(rows)
    if not machines:
        emit("監視対象機種が 0 件でした（target_machines.json を確認）", flush=True)
    return machines


def _recover_via_browser(hall: Hall) -> list[dict[str, Any]]:
    """HTTP の機種一覧が空のとき、ブラウザ上でトークンを載せて描画完了後に取り直す。"""
    import asyncio

    emit(f"  {hall.name}: ブラウザで Turnstile トークンを適用して再取得します", flush=True)
    try:
        asyncio.get_running_loop()
        running = True
    except RuntimeError:
        running = False
    if running:
        emit("  実行中のイベントループがあるため、ブラウザ再取得は親タスク側に任せます", flush=True)
        return []
    from src.scraper.teramoba_browser import fetch_halls_via_browser

    results = asyncio.run(fetch_halls_via_browser([hall]))
    outcome = results.get(hall.name)
    if isinstance(outcome, Exception):
        raise TeramobaError(str(outcome)) from outcome
    return outcome if isinstance(outcome, list) else []


def _needs_browser_fallback(exc: BaseException) -> bool:
    text = str(exc)
    return any(
        token in text
        for token in ("CAPTCHA", "Turnstile", "HTTP 403", "機種が 0 件", "保護ページ")
    )


def _teramoba_fetch_enabled() -> bool:
    flag = (os.getenv("TERAMOBA_FETCH") or "").strip().lower()
    if flag in {"1", "true", "yes", "on"}:
        return True
    if flag in {"0", "false", "no", "off"}:
        return False
    from src.scraper.halls import load_halls_config

    return load_halls_config().get("teramoba_fetch") is True


def _persist_teramoba_hall(hall: Hall, payload: list[dict[str, Any]]) -> None:
    from src.scraper.incremental import publish_hall_incoming

    hall_result = FetchResult(source="teramoba", hall_name=hall.name, machines=payload)
    path = hall_result.write_incoming(hall_data_dir(hall.name, PROJECT_ROOT / "data" / "teramoba"))
    publish_hall_incoming(path, hall.name, "teramoba")


def fetch_teramoba_data(halls: list[Hall] | None = None) -> FetchResult:
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        raise TeramobaError("fetch_teramoba_data は実行中のイベントループから呼べません。fetch_teramoba_data_async を使ってください")
    return asyncio.run(fetch_teramoba_data_async(halls))


async def fetch_teramoba_data_async(halls: list[Hall] | None = None) -> FetchResult:
    import asyncio

    from src.scraper.deadline import hall_timeout_sec

    # インソムニア攻撃でテラモバ2が止まっていた。TERAMOBA_FETCH か halls.json の teramoba_fetch が有効なときだけ取る。
    if not _teramoba_fetch_enabled():
        emit("テラモバ2は障害中のため、今回の取得からスキップします", flush=True)
        return FetchResult(
            source="teramoba",
            hall_name="テラモバ2",
            machines=[],
            extra={
                "skipped": True,
                "reason": "テラモバ2障害中のため一時停止",
                "halls": [],
                "failed_halls": [],
            },
        )

    targets = halls if halls is not None else halls_for_source("teramoba")
    if not targets:
        raise TeramobaError("source=teramoba の有効ホールが config/halls.json にありません")
    from src.scraper.goraggio import select_cycle_halls
    from src.scraper.halls import prioritize_halls

    fetched_names = {
        hall.name
        for hall in targets
        if (hall_data_dir(hall.name, PROJECT_ROOT / "data" / "teramoba") / "incoming.json").is_file()
    }
    chosen = select_cycle_halls(targets, fetched_names, new_limit=len(targets))
    deferred = len(targets) - len(chosen)
    if deferred:
        emit(
            f"テラモバ2: 未取得のうち {len(chosen) - len(fetched_names)} 店を今回取り、残り {deferred} 店は次回以降",
            flush=True,
        )
    targets = prioritize_halls(chosen)
    started = time.monotonic()
    combined: list[dict[str, Any]] = []
    fetched: list[str] = []
    failed: list[str] = []
    reasons: list[str] = []
    details: list[dict[str, str]] = []
    state = storage_state_path()
    emit(
        f"巡回ホール（テラモバ2 / ドメイン共通セッション）: {' → '.join(h.name for h in targets)}",
        flush=True,
    )
    emit("保存済みセッションを確認", flush=True)
    if not global_session_ready(state):
        raise TeramobaError(
            "teramoba2.com の共有セッションがありません。"
            "普段の Chrome で代表店舗（室見）の台詳細まで開いたあと、"
            "python3 scripts/import_chrome_teramoba_session.py を実行してください。"
        )
    from src.scraper.deadline import note_progress
    from src.scraper.halls import max_workers

    workers = min(max_workers(), len(targets))
    emit(
        f"テラモバ2: {len(targets)}店を同時{workers}、同一ドメインは{TERAMOBA_DOMAIN_CONCURRENCY}店まで",
        flush=True,
    )
    gate = asyncio.Semaphore(workers)
    host_gates: dict[str, asyncio.Semaphore] = {}

    def host_gate(hall: Hall) -> asyncio.Semaphore:
        host = (urlparse(target_from_hall(hall).origin).hostname or "").lower()
        slot = host_gates.get(host)
        if slot is None:
            slot = asyncio.Semaphore(TERAMOBA_DOMAIN_CONCURRENCY)
            host_gates[host] = slot
        return slot

    from src.scraper.halls import sleep_hall_gap
    from src.scraper.pace import summarize_failure

    async def _fetch_hall_once(hall: Hall) -> tuple[list[dict[str, Any]] | None, str]:
        limit = max(hall_timeout_sec(), 300.0)
        try:
            payload = await asyncio.wait_for(
                asyncio.to_thread(fetch_teramoba_hall, hall),
                timeout=limit,
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except (TimeoutError, asyncio.TimeoutError):
            return None, f"{hall.name} が {limit:.0f}秒以内に完了しませんでした"
        except Exception as exc:
            return None, str(exc) or "取得失敗"
        if not payload:
            return None, "0 件"
        return payload, ""

    async def _take(hall: Hall) -> tuple[Hall, list[dict[str, Any]] | None, str]:
        async with gate:
            async with host_gate(hall):
                note_progress()
                delay = random.uniform(1.0, 3.0)
                emit(f"  {hall.name}: ドメイン間隔 {delay:.1f}秒", flush=True)
                await asyncio.sleep(delay)
                try:
                    payload, error = await _fetch_hall_once(hall)
                    if error and any(token in error for token in ("CAPTCHA", "タイムアウト", "秒以内")):
                        for attempt in (1, 2):
                            retry_after = random.uniform(18.0, 32.0)
                            emit(
                                f"  {hall.name}: {summarize_failure(error)} のため {retry_after:.0f}秒後に再取得（{attempt}/2）",
                                flush=True,
                            )
                            await asyncio.sleep(retry_after)
                            payload, error = await _fetch_hall_once(hall)
                            if not error:
                                break
                    if payload:
                        await asyncio.to_thread(_persist_teramoba_hall, hall, payload)
                    return hall, payload, error
                finally:
                    await asyncio.to_thread(sleep_hall_gap)

    from src.scraper.pace import failure_record
    from src.scraper.smart_retry import partition_halls
    from src.scraper.source_fallback import finish_hall_attempt

    ready, waiting = partition_halls(targets, "teramoba")
    if waiting:
        emit(f"テラモバ2: 再試行待ち {len(waiting)}店をスキップ", flush=True)
    taken = list(await asyncio.gather(*[_take(hall) for hall in ready]))
    taken.extend((hall, None, "再試行待ち") for hall in waiting)
    for hall, payload, error in taken:
        payload, error, _via = finish_hall_attempt(hall, payload, error, "teramoba")
        if error or payload is None:
            safe = error or "取得失敗"
            emit(f"  {hall.name} の取得失敗: {safe}", flush=True)
            failed.append(hall.name)
            reasons.append(f"{hall.name}: {safe}")
            details.append(failure_record(hall.name, safe))
            continue
        hall_result = FetchResult(source="teramoba", hall_name=hall.name, machines=payload)
        hall_path = hall_result.write_incoming(hall_data_dir(hall.name, PROJECT_ROOT / "data" / "teramoba"))
        emit(f"  {hall.name}: {len(payload)} 件を {hall_path} に保存", flush=True)
        combined.extend(payload)
        fetched.append(hall.name)

    elapsed = time.monotonic() - started
    if not combined:
        detail = " / ".join(reasons)
        raise TeramobaError(
            "テラモバ2の取得に失敗したため incoming.json は更新していません。"
            + (f" {detail}" if detail else "")
        )
    label = fetched[0] if len(fetched) == 1 else "複数店舗"
    result = FetchResult(
        source="teramoba",
        hall_name=label,
        machines=combined,
        extra={
            "halls": fetched,
            "target_halls": [h.name for h in targets],
            "failed_halls": failed,
            "failure_details": details,
            "elapsed_sec": round(elapsed, 1),
            "elapsed_label": format_duration(elapsed),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    output = result.write_incoming()
    site7_copy = FetchResult(
        source="teramoba",
        hall_name=result.hall_name,
        machines=result.machines,
        fetched_at=result.fetched_at,
        extra=result.extra,
    )
    site7_path = site7_copy.write_incoming(PROJECT_ROOT / "data" / "site7")
    emit(
        f"データ取得完了: {output}（互換コピー: {site7_path} / 対象: {', '.join(h.name for h in targets)} / "
        f"成功: {', '.join(fetched)} / 合計 {len(combined)} 件 / {format_duration(elapsed)}）",
        flush=True,
    )
    return result
