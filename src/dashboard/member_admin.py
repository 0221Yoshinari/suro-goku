"""請求サービス（main.py）向けの会員管理画面。

suro-goku-billing は uvicorn main:app で動く。会員一覧はそのアプリに載せる。
入場はマスターパネルと同じ管理者トークンで、未設定のときは全員拒否する。
"""

from __future__ import annotations

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
from src.dashboard.square_members import apply_member_action
from src.dashboard.square_members import list_members

_DASHBOARD_DIR = Path(__file__).resolve().parent
_TEMPLATES_DIR = _DASHBOARD_DIR / "templates"
_STATIC_DIR = _DASHBOARD_DIR / "static"
_PUBLIC_STATIC = {"styles.css"}
_ADMIN_STATIC = {"admin.css", "contracts.css", "members.css", "members.js"}
_NO_STORE = {"Cache-Control": "no-store"}


def register_member_admin(app: FastAPI) -> None:
    """会員管理の画面と API を請求アプリへ登録する。"""

    @app.get(f"{MASTER_PANEL_PREFIX}/members")
    def billing_members_page(request: Request):
        if not master_request_authorized(request):
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

    @app.get(f"{MASTER_PANEL_PREFIX}/members/rows")
    def billing_members_rows(
        request: Request,
        q: str = "",
        status: str = "all",
        sort: str = "registered_at",
        order: str = "desc",
    ) -> JSONResponse:
        if not master_request_authorized(request):
            return JSONResponse({"detail": "認証が必要です"}, status_code=401, headers=_NO_STORE)
        try:
            payload = list_members(query=q, status=status, sort=sort, order=order)
        except SquareMemberError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400, headers=_NO_STORE)
        return JSONResponse(payload, headers=_NO_STORE)

    @app.post(f"{MASTER_PANEL_PREFIX}/members/{{member_id}}/action")
    async def billing_members_action(member_id: str, request: Request) -> JSONResponse:
        if not master_request_authorized(request):
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
        if filename in _ADMIN_STATIC and not master_request_authorized(request):
            raise HTTPException(status_code=404, detail="Not Found")
        path = (_STATIC_DIR / filename).resolve()
        if path.parent != _STATIC_DIR.resolve() or not path.is_file():
            raise HTTPException(status_code=404, detail="Not Found")
        return FileResponse(path, headers=_NO_STORE)
