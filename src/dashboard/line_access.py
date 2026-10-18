"""リッチメニューから開くツールの LINE 本人確認。

ID トークンは LINE の検証 API で確認し、ユーザー ID はクエリや本文では受け取らない。
セッション Cookie には確認済みの LINE ID だけを入れ、利用可否は毎回 square_members を読む。
"""

from __future__ import annotations

import hashlib
import hmac
import html
import logging
import os
import re
import secrets
import time
from urllib.parse import urlencode
from urllib.parse import urlparse

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
from fastapi.responses import RedirectResponse
from fastapi.responses import Response

from src.dashboard.square_members import SquareMemberError
from src.dashboard.square_members import tool_access

logger = logging.getLogger(__name__)

LINE_COOKIE = "line_member"
OAUTH_STATE_COOKIE = "line_oauth_state"
_LINE_USER_RE = re.compile(r"^U[0-9a-fA-F]{32}$")
_LIFF_ID_RE = re.compile(r"^[0-9]{1,20}-[A-Za-z0-9]{1,20}$")
_CHANNEL_ID_RE = re.compile(r"^[0-9]{1,20}$")
_NO_STORE = {"Cache-Control": "no-store"}
_OAUTH_MAX_AGE = 600
_PLAN_COPY = {
    "unregistered": (
        "会員登録が必要です",
        "このツールは、無料体験中または有料会員の方だけが利用できます。LINEのトークで案内に沿ってメールアドレスを送ると、決済用のリンクが届きます。登録が完了してから、もう一度開き直してください。",
    ),
    "pending": (
        "登録がまだ完了していません",
        "LINEに届いた決済リンクを開いて、無料体験を開始してください。体験の開始が確認できるまで、ツールは開けません。",
    ),
    "canceled": (
        "解約済みのため利用できません",
        "有料プランを再開すると、再びツールを利用できます。LINEのトークからお申し込みください。",
    ),
    "past_due": (
        "お支払いを確認できないため停止しています",
        "決済が完了するまでツールは開けません。LINEの案内からお支払いを再開してください。",
    ),
    "suspended": (
        "現在ご利用いただけません",
        "アカウントが停止されています。再開についてはLINEからご連絡ください。",
    ),
    "trial_ended": (
        "無料体験は終了しています",
        "引き続き利用するには有料プランの登録が必要です。LINEの案内からお申し込みください。",
    ),
}
_DETAIL = {key: title for key, (title, _lead) in _PLAN_COPY.items()}


class LineAccessError(Exception):
    """LINE の本人確認を完了できない。"""


def _env(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1].strip()
    return value


def line_liff_id() -> str:
    value = _env("LINE_LIFF_ID")
    if _LIFF_ID_RE.fullmatch(value):
        return value
    return ""


def line_login_channel_id() -> str:
    value = _env("LINE_LOGIN_CHANNEL_ID")
    if _CHANNEL_ID_RE.fullmatch(value):
        return value
    return ""


def line_login_channel_secret() -> str:
    return _env("LINE_LOGIN_CHANNEL_SECRET")


def line_guard_enabled() -> bool:
    """LINE 会員ガード。明示が無くても LIFF かログインチャネルがあれば有効。"""
    flag = _env("LINE_ACCESS_GUARD").lower()
    if flag in {"0", "false", "no", "off"}:
        return False
    if flag in {"1", "true", "yes", "on"}:
        return True
    return bool(line_liff_id() or line_login_channel_id())


def line_official_url() -> str:
    raw = _env("LINE_OFFICIAL_URL") or _env("LINE_ADD_FRIEND_URL")
    parsed = urlparse(raw)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        return ""
    return raw


def issue_line_token(user_id: str, now: float | None = None) -> str:
    if not _LINE_USER_RE.fullmatch(user_id or ""):
        raise LineAccessError("LINEの確認ができません")
    issued = int(now if now is not None else time.time())
    body = f"l1.{user_id}.{issued}"
    signature = hmac.new(_line_signing_key(), body.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def line_token_user(token: str | None, now: float | None = None) -> str:
    if not token:
        return ""
    parts = token.split(".")
    if len(parts) != 4:
        return ""
    version, user_id, issued_raw, signature = parts
    if version != "l1" or not _LINE_USER_RE.fullmatch(user_id) or not issued_raw.isdigit() or not signature:
        return ""
    body = f"l1.{user_id}.{issued_raw}"
    expected = hmac.new(_line_signing_key(), body.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return ""
    issued = int(issued_raw)
    current = int(now if now is not None else time.time())
    age = current - issued
    if age < -60 or age > _session_max_age():
        return ""
    return user_id


def line_user_from_request(request: Request) -> str:
    return line_token_user(request.cookies.get(LINE_COOKIE))


def apply_line_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(
        key=LINE_COOKIE,
        value=token,
        max_age=_session_max_age(),
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(request),
        path="/",
    )


def clear_line_cookie(response: Response) -> None:
    response.delete_cookie(LINE_COOKIE, path="/")


def verify_line_id_token(id_token: str) -> str:
    """LINE の ID トークンを検証し、sub（LINE ユーザー ID）を返す。"""
    token = (id_token or "").strip()
    channel_id = line_login_channel_id()
    if not token or not channel_id or len(token) > 4000 or any(ch in token for ch in "\r\n\x00"):
        raise LineAccessError("LINEの確認ができません")
    import httpx

    try:
        response = httpx.post(
            "https://api.line.me/oauth2/v2.1/verify",
            data={"id_token": token, "client_id": channel_id},
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        raise LineAccessError("LINEの確認ができません") from exc
    if response.status_code != 200:
        raise LineAccessError("LINEの確認ができません")
    try:
        payload = response.json()
    except ValueError as exc:
        raise LineAccessError("LINEの確認ができません") from exc
    if not isinstance(payload, dict):
        raise LineAccessError("LINEの確認ができません")
    if str(payload.get("aud") or "") != channel_id:
        raise LineAccessError("LINEの確認ができません")
    try:
        expires = int(payload.get("exp") or 0)
    except (TypeError, ValueError) as exc:
        raise LineAccessError("LINEの確認ができません") from exc
    if expires < int(time.time()) - 60:
        raise LineAccessError("LINEの確認ができません")
    user_id = str(payload.get("sub") or "")
    if not _LINE_USER_RE.fullmatch(user_id):
        raise LineAccessError("LINEの確認ができません")
    return user_id


def exchange_line_code(code: str, redirect_uri: str) -> str:
    secret = line_login_channel_secret()
    channel_id = line_login_channel_id()
    cleaned = (code or "").strip()
    if not secret or not channel_id or not redirect_uri or not cleaned or len(cleaned) > 2048:
        raise LineAccessError("LINEの確認ができません")
    import httpx

    try:
        response = httpx.post(
            "https://api.line.me/oauth2/v2.1/token",
            data={
                "grant_type": "authorization_code",
                "code": cleaned,
                "redirect_uri": redirect_uri,
                "client_id": channel_id,
                "client_secret": secret,
            },
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        raise LineAccessError("LINEの確認ができません") from exc
    if response.status_code != 200:
        raise LineAccessError("LINEの確認ができません")
    try:
        payload = response.json()
    except ValueError as exc:
        raise LineAccessError("LINEの確認ができません") from exc
    if not isinstance(payload, dict):
        raise LineAccessError("LINEの確認ができません")
    return verify_line_id_token(str(payload.get("id_token") or ""))


def membership_block(request: Request) -> Response | None:
    """確認済み LINE 会員がツールを見てよければ None。それ以外は案内へ返す。"""
    user_id = line_user_from_request(request)
    if not user_id:
        return _anonymous_tool(request)
    try:
        access = tool_access(user_id)
    except SquareMemberError:
        logger.info("line tool blocked because the ledger could not be read")
        return _ledger_unavailable(request)
    if access["allowed"]:
        return None
    logger.info("line tool blocked reason=%s status=%s", access["reason"], access["status"] or "-")
    return _denied_tool(request, str(access["reason"]))


def render_plan(request: Request) -> Response:
    user_id = line_user_from_request(request)
    if user_id:
        access = tool_access(user_id)
        if access["allowed"]:
            response = RedirectResponse("/", status_code=303)
            response.headers["Cache-Control"] = "no-store"
            return response
        reason = str(access["reason"])
    else:
        reason = (request.query_params.get("reason") or "").strip()
    title, lead = _PLAN_COPY.get(reason, _PLAN_COPY["unregistered"])
    return HTMLResponse(_plan_html(title, lead, show_logout=bool(user_id)), headers=_NO_STORE)


async def line_login_page(request: Request) -> Response:
    user_id = line_user_from_request(request)
    if user_id:
        access = tool_access(user_id)
        target = "/" if access["allowed"] else f"/plan?reason={access['reason']}"
        response = RedirectResponse(target, status_code=303)
        response.headers["Cache-Control"] = "no-store"
        return response
    liff_id = line_liff_id()
    if liff_id and line_login_channel_id():
        return HTMLResponse(_liff_html(liff_id), headers=_NO_STORE)
    if line_login_channel_id() and line_login_channel_secret() and line_redirect_uri(request):
        return _oauth_redirect(request)
    title, _lead = _PLAN_COPY["unregistered"]
    lead = "LINEログインの設定が未完了のため、会員確認ができません。設定後に、もう一度開いてください。"
    return HTMLResponse(_plan_html(title, lead, show_logout=False), headers=_NO_STORE)


async def line_oauth_callback(request: Request) -> Response:
    if (request.query_params.get("error") or "").strip():
        response = RedirectResponse("/plan?reason=unregistered", status_code=303)
        _clear_state_cookie(response)
        response.headers["Cache-Control"] = "no-store"
        return response
    code = (request.query_params.get("code") or "").strip()
    state = (request.query_params.get("state") or "").strip()
    if not _state_cookie_ok(request.cookies.get(OAUTH_STATE_COOKIE) or "", state):
        response = RedirectResponse("/line", status_code=303)
        response.headers["Cache-Control"] = "no-store"
        return response
    try:
        user_id = exchange_line_code(code, line_redirect_uri(request))
    except LineAccessError:
        logger.info("line oauth callback rejected")
        response = RedirectResponse("/plan?reason=unregistered", status_code=303)
        _clear_state_cookie(response)
        response.headers["Cache-Control"] = "no-store"
        return response
    return _finish_browser_login(request, user_id)


async def accept_line_session(request: Request) -> Response:
    body = await request.body()
    if len(body) > 8000:
        return JSONResponse({"detail": "送信形式が正しくありません"}, status_code=413, headers=_NO_STORE)
    try:
        import json

        payload = json.loads(body.decode("utf-8"))
    except (UnicodeError, ValueError):
        return JSONResponse({"detail": "送信形式が正しくありません"}, status_code=400, headers=_NO_STORE)
    if not isinstance(payload, dict):
        return JSONResponse({"detail": "送信形式が正しくありません"}, status_code=400, headers=_NO_STORE)
    try:
        user_id = verify_line_id_token(str(payload.get("id_token") or ""))
    except LineAccessError:
        logger.info("line id token rejected")
        return JSONResponse({"detail": "LINEの確認ができません"}, status_code=401, headers=_NO_STORE)
    return _finish_json_login(request, user_id)


def line_redirect_uri(request: Request) -> str:
    configured = _env("LINE_LOGIN_REDIRECT_URL")
    if configured:
        parsed = urlparse(configured)
        if parsed.scheme in {"http", "https"} and parsed.netloc and not parsed.username:
            return configured
        return ""
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "").split(",")[0].strip().lower()
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip()
    if proto not in {"http", "https"} or not host or "/" in host or "\\" in host or " " in host:
        return ""
    return f"{proto}://{host}/line/callback"


def _finish_browser_login(request: Request, user_id: str) -> Response:
    access = tool_access(user_id)
    target = "/" if access["allowed"] else f"/plan?reason={access['reason']}"
    response = RedirectResponse(target, status_code=303)
    _attach_login(response, request, user_id)
    return response


def _finish_json_login(request: Request, user_id: str) -> Response:
    access = tool_access(user_id)
    target = "/" if access["allowed"] else f"/plan?reason={access['reason']}"
    response = JSONResponse(
        {"ok": True, "allowed": bool(access["allowed"]), "reason": access["reason"], "next": target},
        headers=_NO_STORE,
    )
    _attach_login(response, request, user_id)
    return response


def _attach_login(response: Response, request: Request, user_id: str) -> None:
    apply_line_cookie(response, request, issue_line_token(user_id))
    _clear_state_cookie(response)
    response.headers["Cache-Control"] = "no-store"


def _ledger_unavailable(request: Request) -> Response:
    if _wants_json(request):
        return JSONResponse({"detail": "会員確認ができません"}, status_code=503, headers=_NO_STORE)
    return HTMLResponse("会員確認ができません。しばらくしてから再度開いてください。", status_code=503, headers=_NO_STORE)


def _anonymous_tool(request: Request) -> Response:
    if _wants_json(request):
        return JSONResponse({"detail": "LINEでの確認が必要です"}, status_code=401, headers=_NO_STORE)
    response = RedirectResponse("/line", status_code=303)
    response.headers["Cache-Control"] = "no-store"
    return response


def _denied_tool(request: Request, reason: str) -> Response:
    if reason not in _PLAN_COPY:
        reason = "unregistered"
    if _wants_json(request):
        return JSONResponse(
            {"detail": _DETAIL[reason], "reason": reason},
            status_code=403,
            headers=_NO_STORE,
        )
    response = RedirectResponse(f"/plan?reason={reason}", status_code=303)
    response.headers["Cache-Control"] = "no-store"
    return response


def _oauth_redirect(request: Request) -> Response:
    nonce = secrets.token_urlsafe(24)
    query = urlencode(
        {
            "response_type": "code",
            "client_id": line_login_channel_id(),
            "redirect_uri": line_redirect_uri(request),
            "state": nonce,
            "scope": "openid profile",
        }
    )
    response = RedirectResponse(f"https://access.line.me/oauth2/v2.1/authorize?{query}", status_code=303)
    response.set_cookie(
        key=OAUTH_STATE_COOKIE,
        value=_sign_state(nonce),
        max_age=_OAUTH_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(request),
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def _sign_state(nonce: str) -> str:
    signature = hmac.new(_line_signing_key(), nonce.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{nonce}.{signature}"


def _state_cookie_ok(cookie: str, presented: str) -> bool:
    nonce, separator, signature = (cookie or "").partition(".")
    if not separator or not nonce or not signature or not presented:
        return False
    if not hmac.compare_digest(nonce, presented):
        return False
    expected = hmac.new(_line_signing_key(), nonce.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def _clear_state_cookie(response: Response) -> None:
    response.delete_cookie(OAUTH_STATE_COOKIE, path="/")


def _plan_html(title: str, lead: str, *, show_logout: bool) -> str:
    official = line_official_url()
    action = ""
    if official:
        action = (
            f'<p class="login-links"><a href="{html.escape(official, quote=True)}">LINEで登録・再開する</a></p>'
        )
    if show_logout:
        action += (
            '<form method="post" action="/logout" class="login-form">'
            '<button type="submit">別のLINEアカウントで確認</button>'
            "</form>"
        )
    else:
        action += '<p class="login-links"><a href="/line">LINEで確認する</a></p>'
    return f"""<!DOCTYPE html>
<html lang="ja">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
    <title>{html.escape(title)} | スロ極</title>
    <link rel="stylesheet" href="/static/styles.css?v=57" />
  </head>
  <body class="login-body">
    <main class="login-card">
      <p class="eyebrow">スロ極</p>
      <h1>{html.escape(title)}</h1>
      <p class="login-lead">{html.escape(lead)}</p>
      {action}
      <p class="login-links"><a href="/">登録状況を再確認</a></p>
    </main>
  </body>
</html>
"""


def _liff_html(liff_id: str) -> str:
    safe_id = html.escape(liff_id, quote=True)
    return f"""<!DOCTYPE html>
<html lang="ja">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
    <title>確認中 | スロ極</title>
    <link rel="stylesheet" href="/static/styles.css?v=57" />
  </head>
  <body class="login-body">
    <main class="login-card">
      <p class="eyebrow">スロ極</p>
      <h1>会員確認中</h1>
      <p class="login-lead">LINEアカウントを確認しています。</p>
    </main>
    <script src="https://static.line-scdn.net/liff/edge/2/sdk.js"></script>
    <script>
      const liffId = "{safe_id}";
      liff.init({{ liffId }}).then(async () => {{
        if (!liff.isLoggedIn()) {{
          liff.login({{ redirectUri: window.location.origin + "/line" }});
          return;
        }}
        const idToken = liff.getIDToken();
        const response = await fetch("/api/line/session", {{
          method: "POST",
          headers: {{ "content-type": "application/json", "accept": "application/json" }},
          body: JSON.stringify({{ id_token: idToken }}),
        }});
        let next = "/plan?reason=unregistered";
        try {{
          const data = await response.json();
          if (data && typeof data.next === "string" && data.next.startsWith("/") && !data.next.startsWith("//")) {{
            next = data.next;
          }}
        }} catch (error) {{
          next = "/plan?reason=unregistered";
        }}
        window.location.assign(next);
      }}).catch(() => {{
        window.location.assign("/plan?reason=unregistered");
      }});
    </script>
  </body>
</html>
"""


def _wants_json(request: Request) -> bool:
    path = request.url.path
    if path.startswith("/api/"):
        return True
    accept = (request.headers.get("accept") or "").lower()
    return "application/json" in accept


def _session_max_age() -> int:
    from src.dashboard.auth import session_max_age_seconds

    return session_max_age_seconds()


def _line_signing_key() -> bytes:
    from src.dashboard.auth import dashboard_password

    secret = (os.environ.get("DASHBOARD_SESSION_SECRET") or "").strip() or dashboard_password()
    return hashlib.sha256(f"tamaya-line-access:{secret}".encode("utf-8")).digest()


def _cookie_secure(request: Request) -> bool:
    forwarded = request.headers.get("x-forwarded-proto") or request.url.scheme
    return forwarded.split(",")[0].strip().lower() == "https"
