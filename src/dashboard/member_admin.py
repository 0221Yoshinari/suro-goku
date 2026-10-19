"""請求サービス（main.py）向けの会員管理画面。

suro-goku-billing は uvicorn main:app で動く。会員一覧はそのアプリに載せる。
MEMBER_ADMIN_OPEN が 0 のときだけ管理者トークンを要求する。未設定ならトークン画面は出さない。
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Request
from fastapi.responses import FileResponse
from fastapi.responses import JSONResponse
from fastapi.responses import RedirectResponse

from src.dashboard.auth import MASTER_PANEL_PREFIX
from src.dashboard.auth import clear_master_cookie
from src.dashboard.auth import master_request_authorized
from src.dashboard.auth import master_unlock_post
from src.dashboard.auth import render_master_unlock
from src.dashboard.square_members import SquareMemberError
from src.dashboard.square_members import email_access
from src.dashboard.square_members import apply_member_action
from src.dashboard.square_members import list_members

_DASHBOARD_DIR = Path(__file__).resolve().parent
_TEMPLATES_DIR = _DASHBOARD_DIR / "templates"
_STATIC_DIR = _DASHBOARD_DIR / "static"
_PUBLIC_STATIC = {"styles.css"}
_ADMIN_STATIC = {"admin.css", "contracts.css", "members.css", "members.js"}
_NO_STORE = {"Cache-Control": "no-store"}
_CLOSED = {"0", "false", "no", "off"}


def member_admin_open() -> bool:
    """トークン入力を出さずに会員管理を開く。明示的に 0 のときだけ閉じる。"""
    raw = os.environ.get("MEMBER_ADMIN_OPEN")
    if raw is None or not str(raw).strip():
        return True
    return str(raw).strip().lower() not in _CLOSED


def members_dashboard_url() -> str:
    """ブラウザで直接開ける会員管理のURL。"""
    configured = (
        os.environ.get("BILLING_PUBLIC_URL")
        or os.environ.get("RENDER_EXTERNAL_URL")
        or os.environ.get("DASHBOARD_URL")
        or ""
    ).strip().rstrip("/")
    path = f"{MASTER_PANEL_PREFIX}/members"
    if configured:
        return f"{configured}{path}"
    port = (os.environ.get("PORT") or "8000").strip() or "8000"
    return f"http://127.0.0.1:{port}{path}"


def log_members_dashboard_url() -> None:
    print(f"会員管理ダッシュボード: {members_dashboard_url()}", flush=True)


def _member_admin_authorized(request: Request) -> bool:
    if member_admin_open():
        return True
    return master_request_authorized(request)


def register_member_admin(app: FastAPI) -> None:
    """会員管理の画面と API を請求アプリへ登録する。"""

    @app.on_event("startup")
    def _announce_members_dashboard() -> None:
        from src.dashboard.square_members import init_square_store

        log_members_dashboard_url()
        kind = init_square_store()
        if kind != "postgres":
            print(
                "会員台帳が Postgres ではありません。再起動で消えないよう、Squareの定期購入から復元します。",
                flush=True,
            )
        from src.dashboard.square_sync import ensure_square_members
        from src.dashboard.square_sync import keep_square_members

        ensure_square_members()
        keep_square_members()

    @app.get(f"{MASTER_PANEL_PREFIX}/members")
    def billing_members_page(request: Request):
        if not _member_admin_authorized(request):
            return render_master_unlock(next_path=f"{MASTER_PANEL_PREFIX}/members")
        return FileResponse(_TEMPLATES_DIR / "members.html", headers=_NO_STORE)

    @app.post(f"{MASTER_PANEL_PREFIX}/unlock")
    async def billing_master_unlock(request: Request):
        return await master_unlock_post(request)

    @app.post("/logout")
    def billing_logout() -> RedirectResponse:
        response = RedirectResponse(f"{MASTER_PANEL_PREFIX}/members", status_code=303)
        clear_master_cookie(response)
        return response

    @app.get("/member-access")
    def billing_member_access(request: Request, email: str = "") -> JSONResponse:
        if not _member_admin_authorized(request):
            return JSONResponse({"allowed": False}, status_code=401, headers=_NO_STORE)
        try:
            access = email_access(email)
        except SquareMemberError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=503, headers=_NO_STORE)
        return JSONResponse(
            {
                "allowed": bool(access.get("allowed")),
                "reason": access.get("reason") or "",
                "status": access.get("status") or "",
                "email": access.get("email") or "",
                "line_user_id": access.get("line_user_id") or "",
            },
            headers=_NO_STORE,
        )

    @app.get(f"{MASTER_PANEL_PREFIX}/members/rows")
    def billing_members_rows(
        request: Request,
        q: str = "",
        status: str = "all",
        sort: str = "registered_at",
        order: str = "desc",
    ) -> JSONResponse:
        if not _member_admin_authorized(request):
            return JSONResponse({"detail": "認証が必要です"}, status_code=401, headers=_NO_STORE)
        from src.dashboard.square_sync import ensure_square_members
        from src.dashboard.square_sync import needs_square_restore

        if needs_square_restore() and not ensure_square_members():
            return JSONResponse(
                {"detail": "会員台帳を読み込めませんでした。少し待って、再読み込みしてください。"},
                status_code=503,
                headers=_NO_STORE,
            )
        try:
            payload = list_members(query=q, status=status, sort=sort, order=order)
        except SquareMemberError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400, headers=_NO_STORE)
        return JSONResponse(payload, headers=_NO_STORE)

    @app.post(f"{MASTER_PANEL_PREFIX}/members/{{member_id}}/action")
    async def billing_members_action(member_id: str, request: Request) -> JSONResponse:
        if not _member_admin_authorized(request):
            return JSONResponse({"detail": "認証が必要です"}, status_code=401, headers=_NO_STORE)
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"detail": "送信形式が正しくありません"}, status_code=400, headers=_NO_STORE)
        if not isinstance(payload, dict):
            return JSONResponse({"detail": "送信形式が正しくありません"}, status_code=400, headers=_NO_STORE)
        try:
            result = apply_member_action(
                member_id,
                str(payload.get("action") or ""),
                days=int(payload.get("days") if payload.get("days") is not None else 7),
            )
        except (SquareMemberError, TypeError, ValueError) as exc:
            detail = str(exc) if isinstance(exc, SquareMemberError) else "延長日数を確認できません"
            status_code = 404 if detail == "会員が見つかりません" else 400
            return JSONResponse({"detail": detail}, status_code=status_code, headers=_NO_STORE)
        return JSONResponse(result, headers=_NO_STORE)

    @app.get("/static/{filename}")
    def billing_member_static(filename: str, request: Request):
        if filename not in _PUBLIC_STATIC and filename not in _ADMIN_STATIC:
            raise HTTPException(status_code=404, detail="Not Found")
        if filename in _ADMIN_STATIC and not _member_admin_authorized(request):
            raise HTTPException(status_code=404, detail="Not Found")
        path = (_STATIC_DIR / filename).resolve()
        if path.parent != _STATIC_DIR.resolve() or not path.is_file():
            raise HTTPException(status_code=404, detail="Not Found")
        return FileResponse(path, headers=_NO_STORE)
