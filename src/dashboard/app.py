"""判定結果を表示するローカル Web ダッシュボード。"""

from __future__ import annotations

import html
import json
import os
import threading
import time
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
from fastapi.responses import RedirectResponse
from fastapi.responses import Response
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from src.analyzer.judgment import DEFAULT_OUTPUT_DIR, PROJECT_ROOT, judge_all
from src.analyzer.juggler import attach_juggler_view
from src.analyzer.overrides import OverrideError
from src.dashboard.admin import clear_master_edit, create_master, inspection_rows, list_master_rows, save_master_edit
from src.dashboard.hall_admin import HallAdminError, hall_status_index, list_hall_rows, set_hall_enabled
from src.dashboard.auth import MASTER_PANEL_PREFIX
from src.dashboard.contracts import ContractQueryError
from src.dashboard.contracts import list_contracts
from src.dashboard.auth import api_docs_enabled
from src.dashboard.demo_secret import render_demo_page
from src.dashboard.auth import auth_middleware
from src.dashboard.auth import public_demo_mode
from src.dashboard.auth import is_staff
from src.dashboard.auth import master_unlock_post
from src.scraper.pace import public_http_detail
from src.dashboard.events import JST
from src.dashboard.events import EventError
from src.dashboard.events import create_event
from src.dashboard.events import delete_event
from src.dashboard.events import events_for_date
from src.dashboard.events import events_on_dates
from src.dashboard.events import events_updated_at
from src.dashboard.events import group_joint_picks
from src.dashboard.events import list_events
from src.dashboard.events import present_member_payload
from src.dashboard.events import target_options
from src.dashboard.events import today_jst
from src.dashboard.events import update_event
from src.dashboard.auth import billing_get
from src.dashboard.line_access import accept_line_session
from src.dashboard.line_access import line_login_page
from src.dashboard.line_access import line_oauth_callback
from src.dashboard.line_access import line_official_url
from src.dashboard.line_access import render_plan
from src.dashboard.auth import login_get
from src.dashboard.auth import login_post
from src.dashboard.auth import logout
from src.dashboard.auth import register_get
from src.dashboard.auth import register_post
from src.dashboard.square_members import SquareMemberError
from src.dashboard.square_members import SquareWebhookError
from src.dashboard.square_members import apply_member_action
from src.dashboard.square_members import handle_square_webhook
from src.dashboard.square_members import list_members
from src.dashboard.watch import build_watch_payload
from src.scraper.site7 import _load_dotenv

_load_dotenv(PROJECT_ROOT / ".env")
from src.pipeline import run_source
from src.scraper.halls import AREA_PRESETS, HALLS_CONFIG_PATH, PREFECTURE_LABELS, area_presets, configured_halls, hall_area_index, hall_groups, hall_locale_index, load_halls, load_halls_config, prefectures

DASHBOARD_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = DASHBOARD_DIR / "templates"
STATIC_DIR = DASHBOARD_DIR / "static"
MASTER_PANEL_PATH = MASTER_PANEL_PREFIX


@asynccontextmanager
async def _lifespan(_app):
    from src.dashboard.auth import shared_password_enabled

    configured = bool((os.environ.get("DASHBOARD_PASSWORD") or "").strip())
    state = "受け付けます" if shared_password_enabled() else "停止中です"
    print(f"運営ログインは{state}。DASHBOARD_PASSWORD は{'設定済み' if configured else '未設定'}です。", flush=True)
    from src.dashboard.members import init_member_store
    from src.dashboard.square_members import init_square_store

    init_member_store()
    ledger = init_square_store()
    print("ツールのログインは、登録メールアドレスです。", flush=True)
    if ledger != "postgres":
        print(
            "会員台帳が Postgres ではありません。請求サーバーと同じ DATABASE_URL を設定してください。",
            flush=True,
        )
    from src.dashboard.square_sync import ensure_square_members
    from src.dashboard.square_sync import keep_square_members

    ensure_square_members()
    keep_square_members()
    from src.scraper.x_monitor import ensure_background_monitor

    ensure_background_monitor()
    threading.Thread(target=_landing_coverage, name="lp-coverage", daemon=True).start()
    yield


_DOC_URLS = {} if api_docs_enabled() else {"docs_url": None, "redoc_url": None, "openapi_url": None}
app = FastAPI(title="全台系判定ダッシュボード", lifespan=_lifespan, **_DOC_URLS)
app.add_middleware(GZipMiddleware, minimum_size=800)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.exception_handler(HTTPException)
async def masked_http_error(_request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(
        {"detail": public_http_detail(exc.detail)},
        status_code=exc.status_code,
        headers={"Cache-Control": "no-store"},
    )


@app.exception_handler(RequestValidationError)
async def masked_validation_error(_request: Request, _exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        {"detail": "送信形式が正しくありません"},
        status_code=422,
        headers={"Cache-Control": "no-store"},
    )


@app.exception_handler(Exception)
async def masked_unexpected_error(_request: Request, exc: Exception) -> JSONResponse:
    print(f"dashboard error: {type(exc).__name__}", flush=True)
    return JSONResponse(
        {"detail": "処理に失敗しました。しばらくしてから再度お試しください。"},
        status_code=500,
        headers={"Cache-Control": "no-store"},
    )


def _fresh_halls() -> list[str]:
    load_halls_config.cache_clear()
    return configured_halls()


def _row_hall_name(row: dict) -> str:
    return str(row.get("hall_name") or row.get("hall") or "").strip()


def _recount_judgment(payload: dict) -> None:
    machines = [row for row in (payload.get("machines") or []) if isinstance(row, dict)]
    results = [row for row in (payload.get("results") or []) if isinstance(row, dict)]
    counted = machines or results
    payload["machines"] = machines
    payload["results"] = results
    payload["machine_count"] = len(machines)
    payload["unit_count"] = len(results)
    payload["count"] = len(machines) or len(results)
    payload["all_high_count"] = sum(1 for row in counted if row.get("is_all_high"))
    payload["high_56_count"] = sum(
        1 for row in counted if row.get("is_high_56") or row.get("status") == "HIGH_56"
    )
    payload["high_base_count"] = sum(
        1 for row in counted if row.get("is_high_base") or row.get("status") == "HIGH_BASE"
    )
    payload["early_alert_count"] = sum(
        1 for row in counted if row.get("is_early_alert") or row.get("status") == "EARLY_ALERT"
    )


def _apply_hall_catalog(payload: dict) -> None:
    """利用者画面の店舗一覧・都道府県カウント・取得状況。取得元は含めない。"""
    from src.scraper.halls import scope_filter_active

    halls = _fresh_halls()
    payload["configured_halls"] = halls
    merged = list(dict.fromkeys([*halls, *(payload.get("halls") or [])]))
    if scope_filter_active():
        allowed = set(halls)
        payload["halls"] = [name for name in merged if name in allowed]
        for key in ("machines", "results"):
            rows = payload.get(key)
            if isinstance(rows, list):
                payload[key] = [
                    row for row in rows
                    if isinstance(row, dict) and _row_hall_name(row) in allowed
                ]
        _recount_judgment(payload)
    else:
        payload["halls"] = merged
    payload["hall_groups"] = hall_groups(scoped=True)
    payload["hall_areas"] = hall_area_index()
    payload["hall_locales"] = hall_locale_index()
    payload["prefectures"] = prefectures(include_disabled=False)
    payload["area_presets"] = area_presets(include_disabled=False)
    _attach_user_catalog(payload)


def _empty_payload() -> dict:
    payload = {
        "judged_at": None,
        "fetched_at": None,
        "count": 0,
        "all_high_count": 0,
        "high_56_count": 0,
        "high_base_count": 0,
        "early_alert_count": 0,
        "machine_count": 0,
        "unit_count": 0,
        "machines": [],
        "results": [],
    }
    _apply_hall_catalog(payload)
    return _with_events(payload)


@app.middleware("http")
async def disable_cache(request: Request, call_next):
    response = await call_next(request)
    if request.url.path in ("/", MASTER_PANEL_PATH, "/login", "/register", "/billing", "/checkout", "/thanks", "/line", "/line/callback", "/plan", "/demo-secret", "/pr-demo", "/static/app.js", "/static/admin.js", "/static/halls.js", "/static/contracts.js", "/static/members.js", "/static/admin.css", "/static/contracts.css", "/static/members.css", "/static/styles.css", "/static/demo-secret.js", "/static/demo-secret.css", "/square-webhook") or request.url.path.startswith(f"{MASTER_PANEL_PATH}/") or request.url.path.startswith("/api/") or request.url.path.startswith("/legal/"):
        response.headers["Cache-Control"] = "no-store"
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
    return response


@app.middleware("http")
async def require_dashboard_login(request: Request, call_next):
    return await auth_middleware(request, call_next)


def _html(name: str) -> FileResponse:
    return FileResponse(TEMPLATES_DIR / name, headers={"Cache-Control": "no-store"})


@app.get("/login")
async def login_page(request: Request) -> Response:
    return await login_get(request)


@app.post("/login")
async def login_submit(request: Request) -> Response:
    return await login_post(request)


@app.post("/logout")
async def logout_page(request: Request) -> Response:
    return await logout(request)


@app.get("/register")
async def register_page(request: Request) -> Response:
    return await register_get(request)


@app.post("/register")
async def register_submit(request: Request) -> Response:
    return await register_post(request)


@app.get("/billing")
def billing_page(request: Request) -> Response:
    return billing_get(request)


@app.get("/line")
async def line_entry(request: Request) -> Response:
    return await line_login_page(request)


@app.get("/line/")
def line_entry_slash() -> RedirectResponse:
    return RedirectResponse("/line", status_code=307)


@app.get("/line/callback")
async def line_callback(request: Request) -> Response:
    return await line_oauth_callback(request)


@app.get("/line/callback/")
def line_callback_slash() -> RedirectResponse:
    return RedirectResponse("/line/callback", status_code=307)


@app.get("/plan/")
def plan_slash() -> RedirectResponse:
    return RedirectResponse("/plan", status_code=307)


@app.post("/api/line/session")
async def line_session(request: Request) -> Response:
    return await accept_line_session(request)


@app.get("/plan")
def plan_page(request: Request) -> Response:
    return render_plan(request)


@app.get("/checkout")
def checkout_page() -> RedirectResponse:
    return RedirectResponse(line_official_url(), status_code=303)


@app.post(f"{MASTER_PANEL_PATH}/unlock")
async def master_unlock(request: Request) -> Response:
    return await master_unlock_post(request)


@app.get("/")
def index():
    if public_demo_mode():
        return demo_secret_page()
    return _html("index.html")


@app.get("/demo-secret", include_in_schema=False)
def demo_secret_page() -> HTMLResponse:
    """訴求用の固定サンプル。通常のナビには出さない。"""
    body = render_demo_page(TEMPLATES_DIR / "demo_secret.html")
    return HTMLResponse(body, headers={"Cache-Control": "no-store"})


@app.get("/pr-demo", include_in_schema=False)
def pr_demo_page() -> RedirectResponse:
    return RedirectResponse("/demo-secret", status_code=302)


@app.get(MASTER_PANEL_PATH)
def admin_page() -> FileResponse:
    return _html("admin.html")


@app.get(f"{MASTER_PANEL_PATH}/halls")
def halls_admin_page() -> FileResponse:
    return _html("halls.html")


@app.get(f"{MASTER_PANEL_PATH}/members")
def members_admin_page() -> FileResponse:
    return _html("members.html")


@app.get(f"{MASTER_PANEL_PATH}/members/rows")
def members_admin_rows(
    request: Request,
    q: str = "",
    status: str = "all",
    sort: str = "registered_at",
    order: str = "desc",
) -> dict[str, Any]:
    _require_staff(request)
    from src.dashboard.square_sync import ensure_square_members
    from src.dashboard.square_sync import needs_square_restore

    if needs_square_restore() and not ensure_square_members():
        raise HTTPException(status_code=503, detail="会員台帳を読み込めませんでした。少し待って、再読み込みしてください。")
    try:
        return list_members(query=q, status=status, sort=sort, order=order)
    except SquareMemberError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(f"{MASTER_PANEL_PATH}/members/{{member_id}}/action")
async def members_admin_action(member_id: str, request: Request) -> dict[str, Any]:
    _require_staff(request)
    payload = await _read_json_object(request)
    try:
        return apply_member_action(
            member_id,
            str(payload.get("action") or ""),
            days=int(payload.get("days") if payload.get("days") is not None else 7),
        )
    except (SquareMemberError, TypeError, ValueError) as exc:
        detail = str(exc) if isinstance(exc, SquareMemberError) else "延長日数を確認できません"
        status = 404 if detail == "会員が見つかりません" else 400
        raise HTTPException(status_code=status, detail=detail) from exc


@app.post("/square-webhook")
@app.post("/api/square/webhook")
async def square_webhook(request: Request) -> JSONResponse:
    try:
        result = handle_square_webhook(await request.body(), request.headers.get("x-square-hmacsha256-signature") or "")
    except SquareWebhookError as exc:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers={"Cache-Control": "no-store"})
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@app.get(f"{MASTER_PANEL_PATH}/contracts")
def contracts_admin_page() -> FileResponse:
    return _html("contracts.html")


@app.get(f"{MASTER_PANEL_PATH}/contracts/rows")
def contracts_admin_rows(request: Request, q: str = "", status: str = "all") -> dict[str, Any]:
    _require_staff(request)
    try:
        return list_contracts(query=q, status=status)
    except ContractQueryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _require_staff(request: Request) -> None:
    if is_staff(request):
        return
    raise HTTPException(status_code=403, detail="管理者権限が必要です")


@app.get(f"{MASTER_PANEL_PATH}/hall-rows")
def halls_admin_rows(request: Request) -> dict[str, Any]:
    _require_staff(request)
    return list_hall_rows()


@app.patch(f"{MASTER_PANEL_PATH}/hall-rows/{{hall_id}}")
async def halls_admin_toggle(hall_id: str, request: Request) -> dict[str, Any]:
    _require_staff(request)
    payload = await _read_json_object(request)
    if "enabled" not in payload:
        raise HTTPException(status_code=400, detail="有効か無効かを指定してください")
    try:
        return set_hall_enabled(hall_id, bool(payload.get("enabled")))
    except HallAdminError as exc:
        status = 404 if "登録されて" in str(exc) else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc


def _short_prefecture(label: str) -> str:
    if label == "北海道":
        return label
    for suffix in ("都", "府", "県"):
        if label.endswith(suffix) and len(label) > len(suffix):
            return label[: -len(suffix)]
    return label


def _area_labels() -> dict[str, str]:
    labels: dict[str, str] = {}
    for item in AREA_PRESETS:
        for area_id in item.get("areas") or []:
            labels[str(area_id)] = str(item.get("label") or area_id)
    return labels


MONTHLY_PRICE_YEN = "2,980"
_LP_COVERAGE_CACHE: tuple[float, dict[str, str]] | None = None
_LP_COVERAGE_LOCK = threading.Lock()
_LP_TEMPLATE: str | None = None
_LP_TEMPLATE_STAMP: float | None = None


def _landing_coverage() -> dict[str, str]:
    """LPの対応ホールは、2店舗以上ある都道府県だけを組む。結果はファイル更新まで再利用する。"""
    global _LP_COVERAGE_CACHE
    try:
        stamp = HALLS_CONFIG_PATH.stat().st_mtime
    except OSError:
        stamp = 0.0
    with _LP_COVERAGE_LOCK:
        if _LP_COVERAGE_CACHE and _LP_COVERAGE_CACHE[0] == stamp:
            return _LP_COVERAGE_CACHE[1]
        payload = _build_landing_coverage()
        _LP_COVERAGE_CACHE = (stamp, payload)
        return payload


def _build_landing_coverage() -> dict[str, str]:
    halls = load_halls()
    area_labels = _area_labels()
    by_pref = Counter(hall.prefecture for hall in halls if hall.prefecture)
    shown = [(key, count) for key, count in by_pref.items() if count >= 2]
    shown.sort(key=lambda item: (-item[1], PREFECTURE_LABELS.get(item[0], item[0])))
    shown_halls = [hall for hall in halls if by_pref.get(hall.prefecture, 0) >= 2]
    blocks: list[str] = []
    for key, count in shown:
        label = _short_prefecture(PREFECTURE_LABELS.get(key, key))
        rows = sorted((hall for hall in halls if hall.prefecture == key), key=lambda hall: hall.name)
        items: list[str] = []
        for hall in rows:
            area = area_labels.get(hall.area, "")
            area_html = f"<small>{html.escape(area)}</small>" if area else ""
            items.append(f"<li><span>{html.escape(hall.name)}</span>{area_html}</li>")
        blocks.append(
            f'<details class="pref-acc" data-pref="{html.escape(label)}">'
            f"<summary><span>{html.escape(label)}</span><b>{count}</b></summary>"
            f"<ul>{''.join(items)}</ul></details>"
        )
    return {
        "__HALL_COUNT__": str(len(shown_halls)),
        "__PREF_COUNT__": str(len(shown)),
        "__HALL_MODAL__": "".join(blocks),
    }


@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"ok": True})


def _lp_template() -> str:
    """LP本文はファイルの更新時刻が変わったら読み直す。起動中のプロセスでも反映できる。"""
    global _LP_TEMPLATE, _LP_TEMPLATE_STAMP
    path = TEMPLATES_DIR / "landing.html"
    try:
        stamp = path.stat().st_mtime
    except OSError:
        stamp = 0.0
    if _LP_TEMPLATE is None or _LP_TEMPLATE_STAMP != stamp:
        _LP_TEMPLATE = path.read_text(encoding="utf-8")
        _LP_TEMPLATE_STAMP = stamp
    return _LP_TEMPLATE


def _line_official_url() -> str:
    return html.escape(line_official_url(), quote=True)


def _with_public_price(body: str) -> str:
    return (
        body.replace("__MONTHLY_PRICE__", MONTHLY_PRICE_YEN)
        .replace("__LINE_OFFICIAL_URL__", _line_official_url())
    )


@app.get("/lp")
def landing_page() -> StreamingResponse:
    """起動画面を先に返し、店舗一覧の組み立てが終わってから本文を続ける。"""
    shell, rest = _lp_template().split("__LP_SPLIT__", 1)
    shell = _with_public_price(shell)

    def chunks():
        yield shell
        coverage = _landing_coverage()
        head, _, tail = rest.partition("__HALL_MODAL__")
        for key, value in coverage.items():
            if key == "__HALL_MODAL__":
                continue
            head = head.replace(key, value)
            tail = tail.replace(key, value)
        yield _with_public_price(head)
        yield coverage["__HALL_MODAL__"]
        yield _with_public_price(tail)

    return StreamingResponse(
        chunks(),
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/lp-checkout")
def lp_checkout() -> RedirectResponse:
    return RedirectResponse(line_official_url(), status_code=303)


@app.api_route("/api/stripe/webhook", methods=["GET", "POST"])
def retired_stripe_webhook() -> JSONResponse:
    return JSONResponse({"detail": "Not Found"}, status_code=404, headers={"Cache-Control": "no-store"})


@app.get("/thanks")
def thanks_page() -> HTMLResponse:
    url = html.escape(line_official_url(), quote=True)
    body = f"""<!DOCTYPE html>
<html lang="ja">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>スロ極</title></head>
<body>
  <main>
    <h1>申し込みは公式LINEから</h1>
    <p>カード登録と解約は、公式LINEの案内に沿って行います。</p>
    <p><a href="{url}">公式LINEを開く</a></p>
    <p><a href="/lp">紹介ページへ戻る</a></p>
  </main>
</body>
</html>
"""
    return HTMLResponse(body, headers={"Cache-Control": "no-store"})


@app.get("/legal/terms")
def legal_terms() -> HTMLResponse:
    body = _with_public_price((TEMPLATES_DIR / "legal-terms.html").read_text(encoding="utf-8"))
    return HTMLResponse(body, headers={"Cache-Control": "no-store"})


def _seller_line(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    shown = value or f"未設定（環境変数 {name}）"
    return "<br>".join(html.escape(line) for line in shown.splitlines())


@app.get("/legal/tokusho")
def legal_tokusho() -> HTMLResponse:
    body = (TEMPLATES_DIR / "legal-tokusho.html").read_text(encoding="utf-8")
    for key in ("SELLER_NAME", "SELLER_ADDRESS", "SELLER_EMAIL", "SELLER_TEL"):
        body = body.replace(f"__{key}__", _seller_line(key))
    return HTMLResponse(_with_public_price(body), headers={"Cache-Control": "no-store"})


@app.get("/legal/privacy")
def legal_privacy() -> FileResponse:
    return _html("legal-privacy.html")


@app.post("/api/lp-inquiry")
async def create_lp_inquiry(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="送信内容を読み取れませんでした") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="送信形式が正しくありません")
    name = str(payload.get("name") or "").strip()
    email = str(payload.get("email") or "").strip()
    message = str(payload.get("message") or "").strip()
    if not name or not email or "@" not in email:
        raise HTTPException(status_code=400, detail="お名前とメールアドレスを入力してください")
    if not message:
        raise HTTPException(status_code=400, detail="問い合わせ内容を入力してください")
    if not payload.get("agree"):
        raise HTTPException(status_code=400, detail="利用規約への同意が必要です")
    record = {
        "received_at": datetime.now(timezone.utc).isoformat(),
        "name": name,
        "email": email,
        "area": str(payload.get("area") or "").strip(),
        "message": message,
    }
    out_dir = PROJECT_ROOT / "data"
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "lp_inquiries.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return {"ok": True}


@app.get("/api/hall-catalog")
def hall_catalog() -> dict:
    """店舗数と取得状況だけを返す。判定結果の大きなJSONを待たずに画面を出す。"""
    payload: dict[str, Any] = {}
    _apply_hall_catalog(payload)
    return payload


# 県ごとの画面用レスポンス。期限が切れても、組み直しが終わるまでは前の画面を返す。
_VIEW_TTL_SECONDS = 120.0
_VIEW_EMPTY_TTL_SECONDS = 3.0
_VIEW_CACHE: dict[str, tuple[float, float, float, dict]] = {}
_OPENING_CACHE: dict[str, tuple[str, float, dict]] = {}
_disk_opening_cache: dict[str, tuple[float, dict]] = {}
_BOARD_VIEW_CACHE: dict[str, tuple[float, dict]] = {}
_VIEW_GUARD = threading.Lock()
_VIEW_LOCKS: dict[str, threading.Lock] = {}
_VIEW_REFRESHING: set[str] = set()
_LINEUP_RUNNING: set[int] = set()


def _events_mtime() -> float:
    from src.dashboard.events import EVENTS_PATH

    try:
        return EVENTS_PATH.stat().st_mtime if EVENTS_PATH.is_file() else 0.0
    except OSError:
        return 0.0


@app.get("/api/internal/hall-judgments")
def hall_judgment_status(request: Request, prefecture: str = "fukuoka") -> JSONResponse:
    """Macが本番の件数を確認する。画面と同じ組み立て結果の件数だけ返す。"""
    from src.dashboard.judgment_sync import judgment_receipt, token_matches

    if not token_matches(request.headers.get("authorization")):
        return JSONResponse({"detail": "認証が必要です"}, status_code=401)
    selected = (prefecture or "").strip().lower() or "fukuoka"
    return JSONResponse(judgment_receipt(selected))


@app.post("/api/internal/hall-judgments")
async def receive_hall_judgment(request: Request) -> JSONResponse:
    """Macの巡回が1店舗分の判定を本番へ置く。"""
    from src.dashboard.judgment_sync import store_hall_payload, token_matches

    if not token_matches(request.headers.get("authorization")):
        return JSONResponse({"detail": "認証が必要です"}, status_code=401)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"detail": "JSONではありません"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"detail": "店舗データではありません"}, status_code=400)
    if body.get("kind") == "reflection_status":
        from src.dashboard.judgment_sync import store_reflection_status

        store_reflection_status(body)
        return JSONResponse({"ok": True, "note": body.get("note")})
    try:
        store_hall_payload(body)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "hall_name": body.get("hall_name")})


def _without_units(row: dict) -> dict:
    if "units" not in row:
        return row
    slim = dict(row)
    slim.pop("units", None)
    return slim


def public_judgment_view(payload: dict) -> dict:
    """画面の最初の応答から台明細を外す。台明細は開いたときだけ別APIで返す。"""
    view = dict(payload)
    machines = payload.get("machines")
    if isinstance(machines, list):
        view["machines"] = [_without_units(row) if isinstance(row, dict) else row for row in machines]
    juggler = payload.get("juggler")
    if isinstance(juggler, dict):
        slim = dict(juggler)
        islands = juggler.get("islands")
        if isinstance(islands, list):
            slim["islands"] = [_without_units(row) if isinstance(row, dict) else row for row in islands]
        view["juggler"] = slim
    view.pop("results", None)
    return view


def machine_units_from(payload: dict, hall: str, machine: str) -> list[dict]:
    wanted_hall = (hall or "").strip()
    wanted_machine = (machine or "").strip()
    for row in payload.get("machines") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("hall_name") or "").strip() != wanted_hall:
            continue
        if str(row.get("machine_name") or "").strip() != wanted_machine:
            continue
        return [unit for unit in (row.get("units") or []) if isinstance(unit, dict)]
    return []


def juggler_units_from(payload: dict, hall: str, family: str) -> list[dict]:
    wanted_hall = (hall or "").strip()
    wanted_family = (family or "").strip()
    islands = ((payload.get("juggler") or {}).get("islands") or [])
    for island in islands:
        if not isinstance(island, dict):
            continue
        if str(island.get("hall_name") or "").strip() != wanted_hall:
            continue
        if str(island.get("family_id") or "").strip() != wanted_family:
            continue
        return [unit for unit in (island.get("units") or []) if isinstance(unit, dict)]
    return []


@app.get("/api/judgments/digit-units")
def get_digit_units(hall: str = "", digit: int = 0, prefecture: str = "") -> dict:
    """指定した店舗の、末尾0〜9のどれか1つに属する台一覧。"""
    name = (hall or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="hall がありません")
    if digit < 0 or digit > 9:
        raise HTTPException(status_code=400, detail="digit は 0 から 9 です")
    from src.analyzer.extras import digit_unit_rows

    payload = _hall_with_units(name)
    units = digit_unit_rows(payload.get("machines") or [], name, digit)
    return {
        "hall_name": name,
        "digit": digit,
        "prefecture": (prefecture or "").strip().lower() or "fukuoka",
        "count": len(units),
        "units": units,
    }


@app.get("/api/judgments/machine-units")
def get_machine_units(hall: str = "", machine: str = "", prefecture: str = "") -> dict:
    """1店舗・1機種の台明細。一覧の応答には含めない。"""
    name = (hall or "").strip()
    machine_name = (machine or "").strip()
    if not name or not machine_name:
        raise HTTPException(status_code=400, detail="hall と machine が必要です")
    payload = _hall_with_units(name)
    units = machine_units_from(payload, name, machine_name)
    return {
        "hall_name": name,
        "machine_name": machine_name,
        "prefecture": (prefecture or "").strip().lower() or "fukuoka",
        "count": len(units),
        "units": units,
    }


@app.get("/api/judgments/juggler-units")
def get_juggler_units(hall: str = "", family: str = "", prefecture: str = "") -> dict:
    """ジャグラー島の台明細。島の集計とは別に、開いたときだけ返す。"""
    name = (hall or "").strip()
    family_id = (family or "").strip()
    if not name or not family_id:
        raise HTTPException(status_code=400, detail="hall と family が必要です")
    payload = _hall_with_units(name)
    islands = ((payload.get("juggler") or {}).get("islands") or [])
    has_units = any(isinstance(row, dict) and row.get("units") for row in islands)
    if not has_units and payload.get("machines"):
        attach_juggler_view(payload)
    units = juggler_units_from(payload, name, family_id)
    return {
        "hall_name": name,
        "family_id": family_id,
        "prefecture": (prefecture or "").strip().lower() or "fukuoka",
        "count": len(units),
        "units": units,
    }


def clear_judgment_view_cache() -> None:
    with _VIEW_GUARD:
        _VIEW_CACHE.clear()
        _OPENING_CACHE.clear()
        _disk_opening_cache.clear()
        _BOARD_VIEW_CACHE.clear()


def _hall_with_units(hall: str) -> dict:
    """台明細は、その店舗の1件だけ読む。県の全台は組み立てない。"""
    from src.dashboard.judgment_sync import read_hall_payload

    payload = read_hall_payload(hall)
    return payload if isinstance(payload, dict) else {}


def _finish_opening(payload: dict) -> dict:
    """一覧用の件数を保ったまま、イベントと店舗一覧を足す。"""
    kept = {
        key: payload.get(key)
        for key in (
            "machine_count",
            "unit_count",
            "count",
            "all_high_count",
            "high_56_count",
            "high_base_count",
        )
    }
    featured = [row for row in (payload.get("featured_machines") or []) if isinstance(row, dict)]
    payload["machines"] = featured
    _apply_hall_catalog(payload)
    featured = [row for row in (payload.get("machines") or []) if isinstance(row, dict)]
    finished = _with_events(payload, prepare=False)
    finished["featured_machines"] = featured
    finished["machines"] = []
    finished["opening"] = True
    for key, value in kept.items():
        if value is not None:
            finished[key] = value
    return finished


def _opening_cache_key(coverage: dict[str, int]) -> str:
    # 同じ秒に別店舗が届いても、合計が変わるので一覧キャッシュを作り直す。
    return (
        f"{int(coverage.get('updated_at') or 0)}:"
        f"{int(coverage.get('updated_sum') or 0)}:"
        f"{int(coverage.get('ready') or 0)}:"
        f"{int(coverage.get('halls') or 0)}"
    )


def _opening_from_cards(prefecture: str, coverage: dict[str, int]) -> dict:
    """小さい一覧列が未完成でも、保存済みの台カードから全店舗を返す。"""
    from src.dashboard.judgment_store import load_prefecture_cards
    from src.dashboard.judgment_sync import assemble_opening, read_reflection_status, summaries_from_hall_cards

    cached = _BOARD_VIEW_CACHE.get(prefecture)
    if cached is not None and time.monotonic() - cached[0] < 45:
        view = dict(cached[1])
        _attach_reflection_status(view, read_reflection_status())
        return view
    rows = summaries_from_hall_cards(load_prefecture_cards(prefecture))
    body = assemble_opening(
        rows,
        pending=False,
        stamp=f"cards:{int(coverage.get('halls') or 0)}:{len(rows)}",
        hall_count=max(int(coverage.get("halls") or 0), len(rows)),
    )
    view = _finish_opening(body)
    _BOARD_VIEW_CACHE[prefecture] = (time.monotonic(), view)
    _attach_reflection_status(view, read_reflection_status())
    return dict(view)


def _opening_judgments(prefecture: str) -> dict | None:
    """保存済みサマリーだけで最初の画面を作る。台のJSONは読まない。"""
    from src.dashboard.judgment_store import (
        durable_enabled,
        load_opening_summaries,
        summary_coverage,
    )
    from src.dashboard.judgment_sync import assemble_opening, read_reflection_status

    if not durable_enabled():
        return None
    selected = (prefecture or "").strip().lower() or "fukuoka"
    coverage = summary_coverage(selected)
    if coverage is None:
        return None
    if not (coverage["halls"] and coverage["ready"] >= coverage["halls"] and coverage["ready"] > 0):
        return _opening_from_cards(selected, coverage)
    stamp = _opening_cache_key(coverage)
    events_mtime = _events_mtime()
    with _VIEW_GUARD:
        cached = _OPENING_CACHE.get(selected)
    if cached is not None and cached[0] == stamp and cached[1] == events_mtime:
        view = dict(cached[2])
    else:
        pending = coverage["ready"] < coverage["halls"]
        body = assemble_opening(
            load_opening_summaries(selected),
            pending=pending,
            stamp=stamp,
            hall_count=int(coverage["halls"]),
        )
        view = _finish_opening(body)
        with _VIEW_GUARD:
            _OPENING_CACHE[selected] = (stamp, events_mtime, view)
        view = dict(view)
    _attach_reflection_status(view, read_reflection_status())
    return view


def _hall_board(hall: str) -> dict:
    from src.dashboard.judgment_store import load_hall_board, write_hall_parts
    from src.dashboard.judgment_sync import build_hall_parts

    name = (hall or "").strip()
    stored = load_hall_board(name)
    if isinstance(stored, dict) and stored.get("machines") is not None and stored.get("hall_name"):
        return stored
    payload = _hall_with_units(name)
    if not payload.get("machines"):
        return {"hall_name": name, "machines": [], "machine_count": 0, "unit_count": 0}
    summary, board = build_hall_parts(payload)
    write_hall_parts(name, summary, board)
    return board


def _view_lock(prefecture: str) -> threading.Lock:
    with _VIEW_GUARD:
        lock = _VIEW_LOCKS.get(prefecture)
        if lock is None:
            lock = threading.Lock()
            _VIEW_LOCKS[prefecture] = lock
        return lock


def _cached_view(prefecture: str, events_mtime: float) -> dict | None:
    cached = _VIEW_CACHE.get(prefecture)
    if cached is None or not _view_is_fresh(cached, events_mtime):
        return None
    return cached[3]


def _view_is_fresh(entry: tuple[float, float, float, dict], events_mtime: float) -> bool:
    stored_at, stored_events, ttl, _payload = entry
    return stored_events == events_mtime and time.monotonic() - stored_at < ttl


def _schedule_view_refresh(prefecture: str, events_mtime: float) -> None:
    """期限切れの画面は裏で組み直す。開いた人は、今ある数値を先に見る。"""
    with _VIEW_GUARD:
        if prefecture in _VIEW_REFRESHING:
            return
        _VIEW_REFRESHING.add(prefecture)

    def run() -> None:
        try:
            with _view_lock(prefecture):
                with _VIEW_GUARD:
                    current = _VIEW_CACHE.get(prefecture)
                    if current is not None and _view_is_fresh(current, events_mtime):
                        return
                built = _build_prefecture_view(prefecture)
                with _VIEW_GUARD:
                    _store_view(prefecture, events_mtime, built)
        finally:
            with _VIEW_GUARD:
                _VIEW_REFRESHING.discard(prefecture)

    threading.Thread(target=run, name=f"judgment-refresh-{prefecture}", daemon=True).start()


def _store_view(prefecture: str, events_mtime: float, payload: dict) -> None:
    has_rows = bool(payload.get("machines"))
    ttl = _VIEW_TTL_SECONDS if has_rows else _VIEW_EMPTY_TTL_SECONDS
    _VIEW_CACHE[prefecture] = (time.monotonic(), events_mtime, ttl, payload)


def _build_prefecture_view(prefecture: str) -> dict:
    from src.dashboard.judgment_sync import assemble_prefecture

    try:
        synced = assemble_prefecture(prefecture)
    except Exception:
        synced = {}
    if synced:
        return _finish_synced(synced)
    latest = DEFAULT_OUTPUT_DIR / "latest.json"
    return _build_judgments(latest) if latest.is_file() else _empty_payload()


def _full_judgments(prefecture: str) -> dict:
    """県の判定一式。台明細も保持する。画面へ返す前に public_judgment_view を通す。"""
    from src.dashboard.judgment_sync import read_reflection_status

    selected = (prefecture or "").strip().lower() or "fukuoka"
    events_mtime = _events_mtime()
    with _VIEW_GUARD:
        entry = _VIEW_CACHE.get(selected)
        cached = _cached_view(selected, events_mtime)
    if cached is None and entry is not None:
        cached = entry[3]
        _schedule_view_refresh(selected, events_mtime)
    if cached is None:
        with _view_lock(selected):
            with _VIEW_GUARD:
                entry = _VIEW_CACHE.get(selected)
                cached = _cached_view(selected, events_mtime)
            if cached is None and entry is not None:
                cached = entry[3]
                _schedule_view_refresh(selected, events_mtime)
            elif cached is None:
                cached = _build_prefecture_view(selected)
                with _VIEW_GUARD:
                    _store_view(selected, events_mtime, cached)
    _attach_reflection_status(cached, read_reflection_status())
    return cached


def _revision_body(prefecture: str, payload: dict) -> dict:
    return {
        "prefecture": prefecture,
        "judged_at": payload.get("judged_at"),
        "fetched_at": payload.get("fetched_at"),
        "events_updated_at": payload.get("events_updated_at"),
        "machine_count": payload.get("machine_count") or 0,
        "unit_count": payload.get("unit_count") or 0,
        "reflection_status": payload.get("reflection_status") or "",
        "reflected_hall_count": payload.get("reflected_hall_count"),
        "failed_hall_count": payload.get("failed_hall_count"),
        "lineup_ready": bool(payload.get("lineup_ready")),
    }


@app.get("/api/judgments/revision")
def get_judgment_revision(prefecture: str = "") -> dict:
    """更新の有無だけ返す。15秒ごとの確認で全台JSONを組み直さない。"""
    from src.dashboard.judgment_store import durable_enabled, summary_coverage
    from src.dashboard.judgment_sync import read_reflection_status

    selected = (prefecture or "").strip().lower() or "fukuoka"
    coverage = summary_coverage(selected) if durable_enabled() else None
    if coverage is not None:
        try:
            updated_at = events_updated_at()
        except EventError:
            updated_at = None
        body = {
            "prefecture": selected,
            "opening": True,
            "summary_stamp": _opening_cache_key(coverage),
            "summary_pending": coverage["ready"] < coverage["halls"],
            "events_updated_at": updated_at,
            "machine_count": 0,
            "unit_count": 0,
            "lineup_ready": True,
        }
        _attach_reflection_status(body, read_reflection_status())
        return body
    events_mtime = _events_mtime()
    with _VIEW_GUARD:
        entry = _VIEW_CACHE.get(selected)
    payload = entry[3] if entry is not None else {}
    if entry is not None and not _view_is_fresh(entry, events_mtime):
        _schedule_view_refresh(selected, events_mtime)
    body = _revision_body(selected, payload)
    _attach_reflection_status(body, read_reflection_status())
    return body


@app.get("/api/judgments/summary")
def get_judgment_summary(prefecture: str = "") -> dict:
    """開いた瞬間の全6・全56・全456。台明細と全機種カードは含めない。"""
    selected = (prefecture or "").strip().lower() or "fukuoka"
    opening = _opening_judgments(selected)
    if opening is not None:
        return opening
    from src.dashboard.judgment_store import durable_enabled, schedule_summary_backfill

    if durable_enabled():
        schedule_summary_backfill()
    else:
        cached = _disk_opening_cache.get(selected)
        if cached is not None and time.monotonic() - cached[0] < 20:
            return cached[1]
        from src.dashboard.judgment_sync import opening_from_files

        built = _finish_opening(opening_from_files(selected))
        _disk_opening_cache[selected] = (time.monotonic(), built)
        return built
    # 台明細の全件組み立てはここでやらない。開いた画面が数分止まって見える。
    return _finish_opening(
        {
            "opening": True,
            "summary_pending": True,
            "featured_machines": [],
            "reported_halls": [],
            "halls": [],
            "machines": [],
            "machine_count": 0,
            "unit_count": 0,
            "count": 0,
            "all_high_count": 0,
            "high_56_count": 0,
            "high_base_count": 0,
        }
    )


@app.get("/api/judgments/hall")
def get_judgment_hall(hall: str = "", prefecture: str = "") -> dict:
    """1店舗の機種カード。台明細は開いたとき別APIで返す。"""
    name = (hall or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="hall がありません")
    board = _hall_board(name)
    board = dict(board)
    board["prefecture"] = (prefecture or "").strip().lower() or "fukuoka"
    return board


@app.get("/api/judgments/machines")
def get_judgment_machines(prefecture: str = "") -> dict:
    """機種横断を開いたときだけ、県内の機種カードを返す。台明細は含めない。"""
    from src.dashboard.judgment_store import durable_enabled, load_prefecture_boards, summary_coverage

    selected = (prefecture or "").strip().lower() or "fukuoka"
    coverage = summary_coverage(selected) if durable_enabled() else None
    if coverage is None:
        view = public_judgment_view(_full_judgments(selected))
        return {
            "prefecture": selected,
            "machines": view.get("machines") or [],
            "machine_count": view.get("machine_count") or 0,
            "summary_pending": False,
        }
    machines: list[dict] = []
    halls: list[str] = []
    for board in load_prefecture_boards(selected):
        name = str(board.get("hall_name") or "").strip()
        if name:
            halls.append(name)
        for row in board.get("machines") or []:
            if isinstance(row, dict):
                machines.append(row)
    return {
        "prefecture": selected,
        "halls": halls,
        "machines": machines,
        "machine_count": len(machines),
        "summary_pending": bool(coverage["ready"] < coverage["halls"]),
    }


@app.get("/api/judgments")
def get_judgments(prefecture: str = "") -> dict:
    """県を指定しない呼び出しは福岡だけ返す。全国分を一度に返すと画面が空になる。"""
    return public_judgment_view(_full_judgments(prefecture))


def _attach_reflection_status(payload: dict, status: dict) -> None:
    if not status:
        return
    note = str(status.get("note") or "")
    if note:
        payload["reflection_status"] = note
    if status.get("ok_count") is not None:
        payload["reflected_hall_count"] = int(status.get("ok_count") or 0)
    if status.get("failed_count") is not None:
        payload["failed_hall_count"] = int(status.get("failed_count") or 0)


def _finish_synced(payload: dict) -> dict:
    payload.setdefault("machines", [])
    payload.setdefault("results", [])
    machines = payload["machines"]
    payload.setdefault("machine_count", len(machines))
    payload.setdefault("unit_count", payload.get("unit_count") or 0)
    payload.setdefault("count", payload.get("machine_count") or 0)
    payload.setdefault("all_high_count", sum(1 for row in machines if row.get("is_all_high")))
    payload.setdefault("high_56_count", sum(1 for row in machines if row.get("is_high_56") or row.get("status") == "HIGH_56"))
    payload.setdefault("high_base_count", sum(1 for row in machines if row.get("is_high_base") or row.get("status") == "HIGH_BASE"))
    payload.setdefault("early_alert_count", sum(1 for row in machines if row.get("is_early_alert") or row.get("status") == "EARLY_ALERT"))
    payload.setdefault("halls", [])
    _apply_hall_catalog(payload)
    return _with_events(payload)


def _build_judgments(latest) -> dict:
    try:
        payload = json.loads(latest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        return _empty_payload()
    return _present_judgment(payload)


def _present_judgment(payload: dict) -> dict:
    if not payload:
        return _empty_payload()
    payload.setdefault("results", [])
    payload.setdefault("machines", payload.get("machines") or [])
    results = payload.get("results") or []
    # 保存済みの末尾集計があるときは、台一覧を毎回組み直さない。
    if not payload.get("hall_digit_summaries") and isinstance(results, list) and results:
        from src.analyzer.extras import hall_digit_summaries

        payload["hall_digit_summaries"] = hall_digit_summaries(results)
    payload.setdefault("machine_count", len(payload.get("machines") or []))
    payload.setdefault("unit_count", len(payload.get("results") or []))
    payload.setdefault("count", payload.get("machine_count") or len(payload.get("results") or []))
    payload.setdefault("all_high_count", sum(1 for r in (payload.get("machines") or payload["results"]) if r.get("is_all_high")))
    payload.setdefault("high_56_count", sum(1 for r in (payload.get("machines") or payload["results"]) if r.get("is_high_56") or r.get("status") == "HIGH_56"))
    payload.setdefault("high_base_count", sum(1 for r in (payload.get("machines") or payload["results"]) if r.get("is_high_base") or r.get("status") == "HIGH_BASE"))
    payload.setdefault("early_alert_count", sum(1 for r in (payload.get("machines") or payload["results"]) if r.get("is_early_alert") or r.get("status") == "EARLY_ALERT"))
    payload.setdefault("halls", [])
    _apply_hall_catalog(payload)
    if not payload.get("fetched_at"):
        incoming = PROJECT_ROOT / "data" / "site7" / "incoming.json"
        if incoming.is_file():
            try:
                incoming_payload = json.loads(incoming.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                incoming_payload = {}
            if isinstance(incoming_payload, dict):
                payload["fetched_at"] = incoming_payload.get("fetched_at")
    return _with_events(payload)


def _attach_user_catalog(payload: dict) -> None:
    """利用者画面用の対応店舗数と、取得状況の表示名。取得元の種別は出さない。"""
    prefs = payload.get("prefectures") or []
    payload["enabled_hall_count"] = sum(int(item.get("hall_count") or 0) for item in prefs if isinstance(item, dict))
    payload["hall_status"] = hall_status_index()


def _schedule_lineup(payload: dict) -> None:
    """並び分析は台数が多いので、数値を返したあとに裏で付ける。"""
    if payload.get("lineup_ready"):
        return
    machines = payload.get("machines") or []
    if not machines:
        payload["lineup_ready"] = True
        return
    key = id(payload)
    with _VIEW_GUARD:
        if key in _LINEUP_RUNNING:
            return
        _LINEUP_RUNNING.add(key)

    def run() -> None:
        try:
            from src.analyzer.lineup import attach_lineup_blocks

            attach_lineup_blocks(machines)
        except Exception as exc:
            print(f"並び分析をスキップ: {type(exc).__name__}", flush=True)
        finally:
            payload["lineup_ready"] = True
            with _VIEW_GUARD:
                _LINEUP_RUNNING.discard(key)

    threading.Thread(target=run, name="lineup-blocks", daemon=True).start()


def _with_events(payload: dict, *, prepare: bool = True) -> dict:
    """本日のイベントと、合同系列の有力機種を判定結果に足す。"""
    # 県別の画面はここを通る。島を付けないとジャグラーモードの全6・全56・全456が空になる。
    # 最初の一覧は保存済みの島を使う。台明細から作り直すと、開いた瞬間が遅くなる。
    if prepare:
        attach_juggler_view(payload)
        _schedule_lineup(payload)
    today = today_jst()
    tomorrow = (datetime.now(JST).date() + timedelta(days=1)).isoformat()
    try:
        today_events = events_for_date(today)
        upcoming = events_on_dates([today, tomorrow])
        updated_at = events_updated_at()
    except EventError:
        today_events = []
        upcoming = []
        updated_at = None
    payload["events"] = today_events
    payload["upcoming_events"] = upcoming
    payload["events_date"] = today
    payload["tomorrow_date"] = tomorrow
    payload["events_updated_at"] = updated_at
    try:
        payload["group_joint_picks"] = group_joint_picks(payload.get("machines") or [], today_events)
    except Exception:
        payload["group_joint_picks"] = []
    return present_member_payload(payload)


def _x_monitor_status() -> dict:
    try:
        from src.scraper.x_monitor import monitor_status

        return monitor_status()
    except Exception:
        return {}


def _event_error(exc: EventError) -> HTTPException:
    missing = "見つかりません" in str(exc)
    return HTTPException(status_code=404 if missing else 400, detail=str(exc))


@app.get("/api/watch-config")
def get_watch_config() -> dict:
    return build_watch_payload()


@app.get(f"{MASTER_PANEL_PATH}/masters")
def admin_masters() -> dict[str, Any]:
    return list_master_rows()


@app.post(f"{MASTER_PANEL_PATH}/masters")
async def admin_create_master(request: Request) -> dict[str, Any]:
    payload = await _read_json_object(request)
    try:
        return create_master(payload)
    except OverrideError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put(f"{MASTER_PANEL_PATH}/masters/{{machine_id}}")
async def admin_save_master(machine_id: str, request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="保存内容を読み取れませんでした") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="保存形式が正しくありません")
    try:
        return save_master_edit(machine_id, payload)
    except OverrideError as exc:
        status = 404 if "登録されて" in str(exc) else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc


@app.delete(f"{MASTER_PANEL_PATH}/masters/{{machine_id}}")
def admin_clear_master(machine_id: str) -> dict[str, Any]:
    try:
        return clear_master_edit(machine_id)
    except OverrideError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get(f"{MASTER_PANEL_PATH}/inspection")
def admin_inspection() -> dict[str, Any]:
    return inspection_rows()


@app.get(f"{MASTER_PANEL_PATH}/events")
def admin_events() -> dict[str, Any]:
    try:
        return {
            "today": today_jst(),
            "updated_at": events_updated_at(),
            "events": list_events(),
            "targets": target_options(),
            "x_monitor": _x_monitor_status(),
        }
    except EventError as exc:
        raise _event_error(exc) from exc


@app.post(f"{MASTER_PANEL_PATH}/events")
async def admin_create_event(request: Request) -> dict[str, Any]:
    payload = await _read_json_object(request)
    try:
        return create_event(payload)
    except EventError as exc:
        raise _event_error(exc) from exc


@app.put(f"{MASTER_PANEL_PATH}/events/{{event_id}}")
async def admin_update_event(event_id: str, request: Request) -> dict[str, Any]:
    payload = await _read_json_object(request)
    try:
        return update_event(event_id, payload)
    except EventError as exc:
        raise _event_error(exc) from exc


@app.delete(f"{MASTER_PANEL_PATH}/events/{{event_id}}")
def admin_delete_event(event_id: str) -> dict[str, Any]:
    try:
        delete_event(event_id)
    except EventError as exc:
        raise _event_error(exc) from exc
    return {"ok": True}


def _execute_rejudge() -> dict:
    incoming = PROJECT_ROOT / "data" / "site7" / "incoming.json"
    if incoming.is_file():
        result = run_source("site7", input_path=incoming)["judgment"]
    else:
        result = judge_all()
    if not isinstance(result, dict):
        result = {}
    return _with_events(result)


@app.post(f"{MASTER_PANEL_PATH}/rejudge")
def admin_rejudge(request: Request) -> dict[str, Any]:
    _require_staff(request)
    result = _execute_rejudge()
    return {
        "ok": True,
        "judged_at": result.get("judged_at"),
        "fetched_at": result.get("fetched_at"),
        "machine_count": result.get("machine_count"),
        "all_high_count": result.get("all_high_count"),
        "high_56_count": result.get("high_56_count"),
        "high_base_count": result.get("high_base_count"),
    }


@app.post("/api/judge")
def rerun_judgment(request: Request) -> dict:
    _require_staff(request)
    return _execute_rejudge()


async def _read_json_object(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="保存内容を読み取れませんでした") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="保存形式が正しくありません")
    return payload


def _lan_ip() -> str | None:
    import socket

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
    except OSError:
        return None
    return ip if ip and not ip.startswith("127.") else None


def print_phone_access_banner(port: int, public_url: str | None = None) -> None:
    """同じWi-Fiと、回線外から開くURLをターミナルに出す。"""
    lan_ip = _lan_ip()
    local = f"http://127.0.0.1:{port}/"
    phone = f"http://{lan_ip}:{port}/" if lan_ip else None
    bold = "\033[1m"
    gold = "\033[1;93m"
    reset = "\033[0m"
    bar = "█" * 68
    print()
    print(f"{gold}{bold}{bar}{reset}")
    print(f"{gold}{bold}  スロ極 ／ ダッシュボード{reset}")
    print(f"{gold}{bold}{bar}{reset}")
    print()
    if public_url:
        print(f"{gold}{bold}  Wi-Fi外のスマホ（4G/5G）{reset}")
        print()
        print(f"{gold}{bold}      {public_url}{reset}")
        print()
        print("  同じリンクを Discord に送ります。タップするとブラウザで開けます。")
        print()
    print("  紹介ページ /lp は誰でも開けます。ツールは、課金中の会員だけが開けます。")
    print("  申し込みと解約は公式LINEから行います。")
    print("  運営用の閲覧パスワードは DASHBOARD_PASSWORD です。")
    print()
    if phone:
        print(f"{bold}  同じWi-Fi: {phone}{reset}")
    print(f"{bold}  PC用: {local}{reset}")
    print()
    print(f"{gold}{bold}{bar}{reset}")
    print()


@app.get("/{demo_path:path}", include_in_schema=False)
def public_demo_fallback(demo_path: str):
    """チャットで途中が省略されたリンクでも、共有用サーバーではデモを出す。"""
    if public_demo_mode() and not demo_path.startswith(("api/", "static/")):
        return demo_secret_page()
    raise HTTPException(status_code=404, detail="Not Found")


def main() -> None:
    import os
    import uvicorn

    from src.run_gate import exit_if_halted
    from src.tunnel import announce_public_url, ensure_public_url, tunnel_enabled

    on_render = (os.environ.get("RENDER") or "").strip().lower() in {"1", "true", "yes"}
    if on_render:
        os.environ["TUNNEL_ENABLED"] = "0"
    else:
        exit_if_halted("ダッシュボードと外部公開")

    host = os.environ.get("DASHBOARD_HOST", "0.0.0.0")
    port = int(os.environ.get("PORT") or os.environ.get("DASHBOARD_PORT") or "8000")
    public_url = None
    if tunnel_enabled():
        try:
            public_url, created = ensure_public_url(port)
            if created:
                announce_public_url(public_url)
        except Exception as exc:
            print(f"外部公開トンネルを開始できませんでした: {exc}", flush=True)
    print_phone_access_banner(port, public_url)
    uvicorn.run("src.dashboard.app:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
