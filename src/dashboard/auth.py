"""ダッシュボード閲覧用のパスワードと署名付きCookie。"""

from __future__ import annotations

import hashlib
import hmac
import html
import os
import re
import time
from pathlib import Path
from urllib.parse import parse_qs
from urllib.parse import quote
from urllib.parse import unquote

from src.dashboard.members import authorize_member_request
from src.dashboard.members import close_member_session
from src.dashboard.members import member_session_live
from src.dashboard.members import open_member_session

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
from fastapi.responses import RedirectResponse
from fastapi.responses import Response

COOKIE_NAME = "dashboard_session"
MASTER_COOKIE = "master_panel_session"
MASTER_PANEL_PREFIX = "/master-panel-x7k3q9"
MASTER_UNLOCK_PATH = f"{MASTER_PANEL_PREFIX}/unlock"
DEFAULT_PASSWORD = "2026"
DEFAULT_SESSION_DAYS = 7
DEFAULT_MASTER_HOURS = 12
_MASTER_HTML_PATHS = {MASTER_PANEL_PREFIX, f"{MASTER_PANEL_PREFIX}/halls"}
_TOKEN_WARNING_SENT = False
_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
_LOGIN_TEMPLATE = None


def dashboard_password() -> str:
    raw = os.environ.get("DASHBOARD_PASSWORD")
    if raw is None:
        return DEFAULT_PASSWORD
    value = str(raw).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1].strip()
    if not value:
        return DEFAULT_PASSWORD
    return value


def session_max_age_seconds() -> int:
    raw = (os.environ.get("DASHBOARD_SESSION_DAYS") or str(DEFAULT_SESSION_DAYS)).strip()
    try:
        days = int(raw)
    except ValueError:
        days = DEFAULT_SESSION_DAYS
    return max(1, days) * 24 * 60 * 60


def passwords_match(submitted: str, expected: str | None = None) -> bool:
    """文字列が一致するか、両方とも整数表記なら数値として一致するかを見る。"""
    left = (submitted or "").strip()
    right = dashboard_password() if expected is None else str(expected).strip()
    if hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8")):
        return True
    if left.isdigit() and right.isdigit():
        return int(left) == int(right)
    return False


def issue_token(now: float | None = None) -> str:
    issued = int(now if now is not None else time.time())
    body = f"v1.{issued}"
    signature = hmac.new(_signing_key(), body.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def token_is_valid(token: str | None, now: float | None = None) -> bool:
    if not token:
        return False
    version, issued_raw, signature = (token.split(".", 2) + ["", ""])[:3]
    if version != "v1" or not issued_raw.isdigit() or not signature:
        return False
    body = f"v1.{issued_raw}"
    expected = hmac.new(_signing_key(), body.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return False
    issued = int(issued_raw)
    current = int(now if now is not None else time.time())
    age = current - issued
    if age < -60:
        return False
    return age <= session_max_age_seconds()


_MEMBER_ID_RE = re.compile(r"^m_[0-9a-f]{16}$")
_SESSION_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_LOGIN_NOTICES = {
    "expired": "利用権限がありません",
    "trial": "無料体験は終了しています",
    "lapsed": "利用期限が切れています",
    "session": "別の端末で利用中です",
}


def issue_member_token(member_id: str, session_id: str, now: float | None = None) -> str:
    issued = int(now if now is not None else time.time())
    body = f"u1.{member_id}.{session_id}.{issued}"
    signature = hmac.new(_signing_key(), body.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def member_token_claims(token: str | None, now: float | None = None) -> tuple[str, str, int] | None:
    if not token:
        return None
    parts = token.split(".")
    if len(parts) != 5:
        return None
    version, member_id, session_id, issued_raw, signature = parts
    if version != "u1" or not _MEMBER_ID_RE.fullmatch(member_id) or not _SESSION_ID_RE.fullmatch(session_id):
        return None
    if not issued_raw.isdigit() or not signature:
        return None
    body = f"u1.{member_id}.{session_id}.{issued_raw}"
    expected = hmac.new(_signing_key(), body.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    issued = int(issued_raw)
    current = int(now if now is not None else time.time())
    age = current - issued
    if age < -60 or age > session_max_age_seconds():
        return None
    return member_id, session_id, issued


def shared_password_enabled() -> bool:
    """初期パスワードのまま決済連携すると、そのパスワードでは入れなくする。"""
    flag = (os.environ.get("MEMBER_DISABLE_SHARED_PASSWORD") or "").strip().lower()
    if flag in {"1", "true", "yes", "on"}:
        return False
    if (os.environ.get("STRIPE_WEBHOOK_SECRET") or "").strip() and dashboard_password() == DEFAULT_PASSWORD:
        return False
    return True


def client_ip(request: Request) -> str:
    forwarded = (request.headers.get("cf-connecting-ip") or "").strip()
    if forwarded:
        return forwarded[:80]
    chain = (request.headers.get("x-forwarded-for") or "").strip()
    if chain:
        return chain.split(",")[0].strip()[:80]
    host = request.client.host if request.client else ""
    return (host or "")[:80]


def client_fp(request: Request) -> str:
    agent = request.headers.get("user-agent") or ""
    return hashlib.sha256(agent.encode("utf-8")).hexdigest()[:16]


def is_authenticated(request: Request) -> bool:
    cookie = request.cookies.get(COOKIE_NAME)
    if token_is_valid(cookie):
        return True
    claims = member_token_claims(cookie)
    if not claims:
        return False
    return member_session_live(claims[0], claims[1], client_fp(request), client_ip(request))


def master_panel_token() -> str:
    global _TOKEN_WARNING_SENT
    token = (os.environ.get("MASTER_PANEL_TOKEN") or "").strip()
    if not token and not _TOKEN_WARNING_SENT:
        _TOKEN_WARNING_SENT = True
        print("MASTER_PANEL_TOKEN が未設定のため、マスターパネルは拒否します", flush=True)
    return token


def master_session_seconds() -> int:
    raw = (os.environ.get("MASTER_PANEL_SESSION_HOURS") or str(DEFAULT_MASTER_HOURS)).strip()
    try:
        hours = int(raw)
    except ValueError:
        hours = DEFAULT_MASTER_HOURS
    return max(1, hours) * 60 * 60


def master_secrets_match(presented: str | None) -> bool:
    expected = master_panel_token()
    candidate = (presented or "").strip()
    if not expected or not candidate:
        return False
    return hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))


def issue_master_token(now: float | None = None) -> str:
    issued = int(now if now is not None else time.time())
    body = f"m1.{issued}"
    signature = hmac.new(_master_signing_key(), body.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def master_cookie_is_valid(token: str | None, now: float | None = None) -> bool:
    if not master_panel_token() or not token:
        return False
    version, issued_raw, signature = (token.split(".", 2) + ["", ""])[:3]
    if version != "m1" or not issued_raw.isdigit() or not signature:
        return False
    body = f"m1.{issued_raw}"
    expected = hmac.new(_master_signing_key(), body.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return False
    issued = int(issued_raw)
    current = int(now if now is not None else time.time())
    age = current - issued
    if age < -60:
        return False
    return age <= master_session_seconds()


def is_master_panel_path(path: str) -> bool:
    return path == MASTER_PANEL_PREFIX or path.startswith(f"{MASTER_PANEL_PREFIX}/")


def master_request_authorized(request: Request) -> bool:
    header = (request.headers.get("x-master-token") or "").strip()
    authorization = (request.headers.get("authorization") or "").strip()
    bearer = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    if master_secrets_match(header) or master_secrets_match(bearer):
        return True
    return master_cookie_is_valid(request.cookies.get(MASTER_COOKIE))


def apply_master_cookie(response: Response, request: Request) -> None:
    response.set_cookie(
        key=MASTER_COOKIE,
        value=issue_master_token(),
        max_age=master_session_seconds(),
        httponly=True,
        samesite="strict",
        secure=_cookie_secure(request),
        path="/",
    )


def clear_master_cookie(response: Response) -> None:
    response.delete_cookie(MASTER_COOKIE, path="/")


def is_retired_admin_path(path: str) -> bool:
    """旧 /admin は存在しないものとして扱う。"""
    return path == "/admin" or path.startswith("/admin/") or path == "/api/admin" or path.startswith("/api/admin/")


def is_blocked_path(path: str) -> bool:
    """ソース、環境変数、会員台帳を URL から見せない。"""
    for candidate in (path or "", unquote(unquote(path or ""))):
        if ".." in candidate or "\\" in candidate or "\x00" in candidate:
            return True
        folded = candidate.casefold()
        if any(marker in folded for marker in (".env", "/.git", ".py", "proxy_config", "members.json")):
            return True
    return False


def is_staff_api(path: str) -> bool:
    """再判定とホール管理。運営セッションかマスタートークンが要る。"""
    if path == "/api/judge":
        return True
    if path == f"{MASTER_PANEL_PREFIX}/rejudge":
        return True
    prefix = f"{MASTER_PANEL_PREFIX}/hall-rows"
    return path == prefix or path.startswith(f"{prefix}/")


def is_staff(request: Request) -> bool:
    """運営用パスワードのセッション、またはマスタートークン。"""
    if token_is_valid(request.cookies.get(COOKIE_NAME)):
        return True
    return master_request_authorized(request)


def public_demo_mode() -> bool:
    """共有リンク用。このプロセスでは入口をデモにし、ログイン画面を出さない。"""
    return (os.getenv("SURO_PUBLIC_DEMO") or "").strip().lower() in {"1", "true", "yes", "on"}


_OPEN_TOOL_PATHS = {"/", "/api/judgments", "/api/hall-catalog", "/api/watch-config"}


def open_tool_mode() -> bool:
    """リリース前。Render ではリンクを開くと、パスワードなしでツール画面になる。"""
    flag = (os.getenv("DASHBOARD_OPEN") or "").strip().lower()
    if flag in {"0", "false", "no", "off"}:
        return False
    if flag in {"1", "true", "yes", "on"}:
        return True
    return (os.getenv("RENDER") or "").strip().lower() in {"1", "true", "yes"}


def is_public_path(path: str) -> bool:
    if path in {"/health", "/login", "/logout", "/lp", "/thanks", "/api/lp-inquiry", "/api/lp-checkout", "/api/stripe/webhook", "/demo-secret", "/pr-demo"}:
        return True
    if path.startswith("/legal/") or path.startswith("/static/"):
        return True
    if open_tool_mode() and path in _OPEN_TOOL_PATHS:
        return True
    if not public_demo_mode():
        return False
    if path.startswith("/api/") or is_master_panel_path(path) or is_staff_api(path):
        return False
    if is_blocked_path(path) or is_retired_admin_path(path):
        return False
    return True


def safe_next(value: str | None) -> str:
    target = (value or "").strip()
    if not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    path = target.split("?", 1)[0]
    if path in {"/login", "/logout"}:
        return "/"
    return target


def apply_session_cookie(response: Response, request: Request) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=issue_token(),
        max_age=session_max_age_seconds(),
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(request),
        path="/",
    )


def apply_member_cookie(response: Response, request: Request, member_id: str, session_id: str) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=issue_member_token(member_id, session_id),
        max_age=session_max_age_seconds(),
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(request),
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def render_login(error: str = "", next_path: str = "/") -> HTMLResponse:
    template = _login_template()
    error_html = ""
    if error:
        error_html = f'<p class="login-error" role="alert">{html.escape(error)}</p>'
    release = ""
    if error == "別の端末で利用中です":
        release = '<form method="post" action="/logout" class="login-form"><button type="submit">このブラウザの接続を解除</button></form>'
    body = template.replace("__ERROR__", error_html + release).replace("__NEXT__", html.escape(safe_next(next_path), quote=True))
    return HTMLResponse(body, headers={"Cache-Control": "no-store"})


async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if is_blocked_path(path) or is_retired_admin_path(path):
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Not Found"}, status_code=404, headers={"Cache-Control": "no-store"})
        return HTMLResponse("Not Found", status_code=404, headers={"Cache-Control": "no-store"})
    if is_public_path(path):
        return await call_next(request)
    kind, denial = _access_kind(request)
    if denial is not None:
        return denial
    if kind == "anonymous":
        if path.startswith("/api/") or _wants_json(request):
            return JSONResponse({"detail": "ログインが必要です"}, status_code=401, headers={"Cache-Control": "no-store"})
        target = path
        if request.url.query:
            target = f"{target}?{request.url.query}"
        response = RedirectResponse(f"/login?next={quote(safe_next(target), safe='/')}", status_code=303)
        response.headers["Cache-Control"] = "no-store"
        return response
    if is_staff_api(path):
        if is_staff(request):
            return await call_next(request)
        if kind == "member":
            return JSONResponse(
                {"detail": "管理者権限が必要です"},
                status_code=403,
                headers={"Cache-Control": "no-store"},
            )
    if is_master_panel_path(path) and kind == "member" and not master_request_authorized(request):
        return HTMLResponse("Not Found", status_code=404, headers={"Cache-Control": "no-store"})
    if is_master_panel_path(path) and not master_request_authorized(request):
        if path == MASTER_UNLOCK_PATH and request.method.upper() == "POST":
            return await call_next(request)
        if request.method.upper() == "GET" and path in _MASTER_HTML_PATHS:
            return render_master_unlock()
        return JSONResponse({"detail": "認証が必要です"}, status_code=401, headers={"Cache-Control": "no-store"})
    return await call_next(request)


async def login_get(request: Request) -> Response:
    if public_demo_mode():
        return RedirectResponse("/demo-secret", status_code=302)
    if open_tool_mode():
        target = safe_next(request.query_params.get("next"))
        if not is_master_panel_path(target) and not is_staff_api(target):
            return RedirectResponse("/", status_code=302)
    notice = _LOGIN_NOTICES.get((request.query_params.get("notice") or "").strip(), "")
    if is_authenticated(request):
        return RedirectResponse(safe_next(request.query_params.get("next")), status_code=303)
    return render_login(error=notice, next_path=request.query_params.get("next") or "/")


async def login_post(request: Request) -> Response:
    form = parse_qs((await request.body()).decode("utf-8", errors="replace"), keep_blank_values=True)
    password = (form.get("password") or [""])[0].strip()
    next_path = safe_next((form.get("next") or ["/"])[0])
    if passwords_match(password):
        if not shared_password_enabled():
            return render_login(
                error="運営用のパスワードは、いまの設定では使えません。",
                next_path=next_path,
            )
        response = RedirectResponse(next_path, status_code=303)
        apply_session_cookie(response, request)
        return response
    member_id, session_id, error = open_member_session(password, client_ip(request), client_fp(request))
    if member_id and session_id:
        response = RedirectResponse(next_path, status_code=303)
        apply_member_cookie(response, request, member_id, session_id)
        return response
    if error:
        return render_login(error=error, next_path=next_path)
    return render_login(error="パスワードが違います", next_path=next_path)


async def logout(request: Request) -> Response:
    claims = member_token_claims(request.cookies.get(COOKIE_NAME))
    if claims:
        close_member_session(claims[0], claims[1])
    response = RedirectResponse("/login", status_code=303)
    clear_session_cookie(response)
    clear_master_cookie(response)
    return response


def safe_master_next(value: str | None) -> str:
    target = safe_next(value)
    path = target.split("?", 1)[0]
    if path == MASTER_UNLOCK_PATH or not is_master_panel_path(path):
        return MASTER_PANEL_PREFIX
    return target


def render_master_unlock(error: str = "", next_path: str = MASTER_PANEL_PREFIX) -> HTMLResponse:
    error_html = ""
    if error:
        error_html = f'<p class="login-error" role="alert">{html.escape(error)}</p>'
    body = f"""<!DOCTYPE html>
<html lang="ja">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>管理者認証 | スロ極</title>
    <link rel="stylesheet" href="/static/styles.css?v=56" />
  </head>
  <body class="login-body">
    <main class="login-card">
      <p class="eyebrow">スロ極</p>
      <h1>マスターパネル</h1>
      <p class="login-lead">管理者トークンを入力してください。</p>
      {error_html}
      <form method="post" action="{MASTER_UNLOCK_PATH}" class="login-form">
        <input type="hidden" name="next" value="{html.escape(safe_master_next(next_path), quote=True)}" />
        <label class="login-label" for="master-token">管理者トークン</label>
        <input id="master-token" name="master_token" type="password" autocomplete="off" autofocus required />
        <button type="submit" class="primary">入室</button>
      </form>
    </main>
  </body>
</html>
"""
    return HTMLResponse(body, headers={"Cache-Control": "no-store"})


async def master_unlock_post(request: Request) -> Response:
    form = parse_qs((await request.body()).decode("utf-8", errors="replace"), keep_blank_values=True)
    presented = (form.get("master_token") or [""])[0]
    next_path = safe_master_next((form.get("next") or [MASTER_PANEL_PREFIX])[0])
    if not master_secrets_match(presented):
        return render_master_unlock(error="認証に失敗しました", next_path=next_path)
    response = RedirectResponse(next_path, status_code=303)
    apply_master_cookie(response, request)
    return response


def _access_kind(request: Request) -> tuple[str, Response | None]:
    cookie = request.cookies.get(COOKIE_NAME)
    if token_is_valid(cookie):
        return "operator", None
    claims = member_token_claims(cookie)
    if not claims:
        return "anonymous", None
    state, detail = authorize_member_request(claims[0], claims[1], client_fp(request), client_ip(request))
    if state == "ok":
        return "member", None
    status_code = 403 if state == "revoked" else 401
    return "anonymous", _member_denied(request, detail or "ログインが必要です", status_code)


def _member_denied(request: Request, detail: str, status_code: int) -> Response:
    path = request.url.path
    if path.startswith("/api/") or _wants_json(request):
        return JSONResponse({"detail": detail}, status_code=status_code, headers={"Cache-Control": "no-store"})
    target = path
    if request.url.query:
        target = f"{target}?{request.url.query}"
    page = render_login(error=detail, next_path=target)
    page.status_code = status_code
    return page


def _wants_json(request: Request) -> bool:
    accept = (request.headers.get("accept") or "").lower()
    return "application/json" in accept


def _master_signing_key() -> bytes:
    return hashlib.sha256(f"tamaya-master-panel:{master_panel_token()}".encode("utf-8")).digest()


def _signing_key() -> bytes:
    secret = (os.environ.get("DASHBOARD_SESSION_SECRET") or "").strip() or dashboard_password()
    return hashlib.sha256(f"tamaya-dashboard-session:{secret}".encode("utf-8")).digest()


def _cookie_secure(request: Request) -> bool:
    forwarded = request.headers.get("x-forwarded-proto") or request.url.scheme
    return forwarded.split(",")[0].strip().lower() == "https"


def _login_template() -> str:
    global _LOGIN_TEMPLATE
    if _LOGIN_TEMPLATE is None:
        _LOGIN_TEMPLATE = (_TEMPLATES_DIR / "login.html").read_text(encoding="utf-8")
    return _LOGIN_TEMPLATE
