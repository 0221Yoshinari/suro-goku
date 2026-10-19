"""LINE のメールアドレス受信から、カード登録ページを返す。

起動: uvicorn main:app --host 0.0.0.0 --port 8000

定期購入は登録日を0日目とし、案内上は最初の10日間が0円。11日目から月額2,980円。
0円の Square 決済リンクはカードを預からないため、このページでカードを保存し、
そのカードで定期購入を始める。11日目から Square がそのカードに月額2,980円を請求する。
/square-webhook は会員台帳のステータスを更新する。ツール本体は同じ台帳を見て、
無料体験中と有料会員以外をその次のアクセスから止める。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
from fastapi.responses import Response

from src.dashboard.member_admin import register_member_admin

load_dotenv()

logger = logging.getLogger("surogoku.billing")

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)+$"
)
_MAX_BODY = 1_000_000
_INITIAL_AMOUNT_JPY = 0
_MONTHLY_AMOUNT_JPY = 2980
# 登録日を0日目とし、日次0円フェーズを11期間にする。月額フェーズは11日目から始まる。
_TRIAL_DAYS = 11
_JST = timezone(timedelta(hours=9))
_SQUARE_VERSION = "2026-09-16"
_PLAN_NAME = "スロ極"
_VARIATION_NAME = "スロ極 10日間0円・以降月額2980円"
_CHECKOUT_ITEM_NAME = "スロ極 月額2,980円（最初の10日間は0円）"
_OPEN_SUBSCRIPTION = {"PENDING", "ACTIVE"}
_TRIAL_LABELS = {"10日間無料でスロ極を試す", "7日間無料でスロ極を試す"}
_TRIAL_BUTTON = "🚀 10日間無料でスロ極を試す"
_WELCOME = "スロ極へようこそ。10日間、全機能を無料で試せます。\n下のボタンから、1分で登録を始められます。"
_GUIDE = (
    "スロ極の10日間無料トライアルへようこそ！✨\n"
    "\n"
    "このチャットに【普段お使いのメールアドレス】を送信してください。すぐにカード登録ページをお届けします！\n"
    "\n"
    "※今日の支払いは0円です。10日以内に解約すれば、料金は一切かかりません。"
)
_CHECKOUT_TTL = 48 * 60 * 60
_ALREADY = "このLINEアカウントは登録済みです。"
_FAILED = "申し込みを完了できませんでした。時間をおいて、もう一度メールアドレスを送ってください。"

_resolved_ids: dict[str, str] = {}


class SquareAPIError(Exception):
    def __init__(self, status: int, codes: list[str]) -> None:
        self.status = status
        self.codes = codes
        super().__init__(f"square {status}: {','.join(codes)}")


def is_email(text: str) -> bool:
    candidate = text.strip()
    if not candidate or len(candidate) > 254:
        return False
    return _EMAIL_RE.fullmatch(candidate) is not None


def is_trial_intent(text: str) -> bool:
    normalized = " ".join(text.strip().split())
    if normalized == _TRIAL_BUTTON:
        return True
    return normalized.replace("🚀", "").strip() in _TRIAL_LABELS


def line_text_message(text: str, quick_reply: str | None = None) -> dict:
    message: dict = {"type": "text", "text": text}
    label = (quick_reply or "").strip()
    if not label:
        return message
    if len(label) > 20:
        raise SquareAPIError(500, ["QUICK_REPLY_LABEL"])
    message["quickReply"] = {
        "items": [
            {
                "type": "action",
                "action": {"type": "message", "label": label, "text": label},
            }
        ]
    }
    return message


def line_signature_valid(body: bytes, signature: str, secret: str) -> bool:
    if not body or not signature or not secret:
        return False
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("ascii")
    return hmac.compare_digest(expected, signature.strip())


def subscription_start_date(now: datetime | None = None) -> str:
    """カード登録日を0日目にする。Square の開始日は日本時間のその日。"""
    current = now.astimezone(_JST) if now is not None else datetime.now(_JST)
    return current.strftime("%Y-%m-%d")


def subscription_phases() -> list[dict]:
    return [
        {
            "ordinal": 0,
            "cadence": "DAILY",
            "periods": _TRIAL_DAYS,
            "pricing": {
                "type": "STATIC",
                "price": {"amount": _INITIAL_AMOUNT_JPY, "currency": "JPY"},
            },
        },
        {
            "ordinal": 1,
            "cadence": "MONTHLY",
            "pricing": {
                "type": "STATIC",
                "price": {"amount": _MONTHLY_AMOUNT_JPY, "currency": "JPY"},
            },
        },
    ]


def checkout_secret() -> str:
    secret = os.getenv("LINE_CHANNEL_SECRET", "").strip()
    if not secret:
        raise SquareAPIError(500, ["MISSING_CHECKOUT_SECRET"])
    return secret


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + ("=" * (-len(text) % 4)))


def issue_checkout_token(customer_id: str, email: str, user_id: str, *, now: int | None = None) -> str:
    issued = int(time.time() if now is None else now)
    body = _b64e(
        json.dumps(
            {"c": customer_id, "e": email, "u": user_id, "exp": issued + _CHECKOUT_TTL},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )
    mac = _b64e(hmac.new(checkout_secret().encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{mac}"


def read_checkout_token(token: str, *, now: int | None = None) -> dict[str, str | int]:
    current = int(time.time() if now is None else now)
    parts = (token or "").split(".")
    if len(parts) != 2 or not token or len(token) > 2000:
        raise SquareAPIError(400, ["TOKEN_INVALID"])
    body, mac = parts
    expected = _b64e(hmac.new(checkout_secret().encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest())
    try:
        valid = hmac.compare_digest(expected, mac)
    except ValueError:
        valid = False
    if not valid:
        raise SquareAPIError(400, ["TOKEN_INVALID"])
    try:
        payload = json.loads(_b64d(body))
    except (ValueError, json.JSONDecodeError):
        raise SquareAPIError(400, ["TOKEN_INVALID"]) from None
    if not isinstance(payload, dict):
        raise SquareAPIError(400, ["TOKEN_INVALID"])
    customer_id = str(payload.get("c") or "")
    email = str(payload.get("e") or "")
    user_id = str(payload.get("u") or "")
    exp = payload.get("exp")
    if not customer_id or not email or not user_id or not isinstance(exp, int):
        raise SquareAPIError(400, ["TOKEN_INVALID"])
    if exp < current:
        raise SquareAPIError(400, ["TOKEN_EXPIRED"])
    return {"customer_id": customer_id, "email": email, "user_id": user_id, "exp": exp}


def normalize_public_base(base_url: str) -> str:
    raw = (base_url or "").strip().rstrip("/")
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    local = host in {"localhost", "127.0.0.1", "testserver"}
    if parsed.scheme == "https" and host:
        return raw
    if parsed.scheme == "http" and local:
        return raw
    raise SquareAPIError(500, ["PUBLIC_BASE_URL"])


def request_public_base(request: Request) -> str:
    configured = os.getenv("PUBLIC_BASE_URL", "").strip() or os.getenv("RENDER_EXTERNAL_URL", "").strip()
    if configured:
        return normalize_public_base(configured)
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "https").split(",")[0].strip()
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip()
    return normalize_public_base(f"{proto}://{host}")


def checkout_page_url(base: str, token: str) -> str:
    # LINE は # 以降をリンクから落とすことがある。トークンはクエリで渡す。
    return f"{normalize_public_base(base)}/checkout?t={token}"


def checkout_reply(url: str) -> str:
    return (
        "カード登録ページを開いてください。\n"
        "月額2,980円です。最初の10日間は0円で、今日はカードを登録するだけです。\n"
        "11日目から、登録したカードに月額2,980円が自動で引き落とされます。\n"
        "10日以内に解約すれば、料金はかかりません。\n"
        f"{url}"
    )


def square_web_sdk_url() -> str:
    if os.getenv("SQUARE_ENVIRONMENT", "sandbox").strip().lower() == "production":
        return "https://web.squarecdn.com/v1/square.js"
    return "https://sandbox.web.squarecdn.com/v1/square.js"


def buyer_message(exc: SquareAPIError) -> str:
    codes = set(exc.codes)
    if "TOKEN_EXPIRED" in codes or "TOKEN_INVALID" in codes:
        return "この登録リンクは無効か、期限切れです。LINEにもう一度メールアドレスを送ってください。"
    if codes & {
        "CARD_DECLINED",
        "GENERIC_DECLINE",
        "CVV_FAILURE",
        "VERIFY_CVV_FAILURE",
        "VERIFY_AVS_FAILURE",
        "INVALID_CARD",
        "PAN_FAILURE",
        "SOURCE_ID",
    }:
        return "カードを登録できませんでした。別のカードで試してください。"
    return "登録を完了できませんでした。時間をおいて、もう一度お試しください。"


def checkout_error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"ok": False, "message": message}, status_code=status)


def square_base_url() -> str:
    environment = os.getenv("SQUARE_ENVIRONMENT", "sandbox").strip().lower()
    if environment == "production":
        return "https://connect.squareup.com"
    return "https://connect.squareupsandbox.com"


def _error_codes(payload: dict) -> list[str]:
    codes: list[str] = []
    for error in payload.get("errors") or []:
        if isinstance(error, dict):
            codes.append(str(error.get("code") or error.get("category") or "ERROR"))
    return codes or ["ERROR"]


async def square_json(
    method: str,
    path: str,
    *,
    params: dict[str, str] | None = None,
    payload: dict | None = None,
) -> dict:
    token = os.getenv("SQUARE_ACCESS_TOKEN", "").strip()
    if not token:
        raise SquareAPIError(500, ["MISSING_TOKEN"])
    headers = {
        "Authorization": f"Bearer {token}",
        "Square-Version": _SQUARE_VERSION,
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(base_url=square_base_url(), timeout=20.0) as client:
        response = await client.request(method, path, headers=headers, params=params, json=payload)
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code >= 400 or not isinstance(body, dict):
        raise SquareAPIError(response.status_code, _error_codes(body if isinstance(body, dict) else {}))
    return body


async def line_display_name(user_id: str) -> str:
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    if not token or not user_id:
        return ""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                f"https://api.line.me/v2/bot/profile/{user_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
        if response.status_code >= 400:
            return ""
        body = response.json()
    except Exception:
        logger.exception("line profile lookup failed")
        return ""
    if not isinstance(body, dict):
        return ""
    return str(body.get("displayName") or "").strip()[:80]


async def line_reply(reply_token: str, text: str, *, quick_reply: str | None = None) -> None:
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    if not token or not reply_token:
        raise SquareAPIError(500, ["MISSING_LINE_TOKEN"])
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            "https://api.line.me/v2/bot/message/reply",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"replyToken": reply_token, "messages": [line_text_message(text, quick_reply)]},
        )
    if response.status_code >= 400:
        logger.error("line reply failed status=%s", response.status_code)
        raise SquareAPIError(response.status_code, ["LINE_REPLY_FAILED"])


def _variation_id(objects: list[dict]) -> str:
    for obj in objects:
        if not isinstance(obj, dict) or obj.get("is_deleted"):
            continue
        if obj.get("type") == "SUBSCRIPTION_PLAN_VARIATION":
            data = obj.get("subscription_plan_variation_data") or {}
            if data.get("name") == _VARIATION_NAME and obj.get("id"):
                return str(obj["id"])
        plan_data = obj.get("subscription_plan_data") or {}
        for variation in plan_data.get("subscription_plan_variations") or []:
            if not isinstance(variation, dict) or variation.get("is_deleted"):
                continue
            data = variation.get("subscription_plan_variation_data") or {}
            if data.get("name") == _VARIATION_NAME and variation.get("id"):
                return str(variation["id"])
    return ""


async def resolve_location_id() -> str:
    configured = os.getenv("SQUARE_LOCATION_ID", "").strip()
    cached = _resolved_ids.get("location")
    if cached and (not configured or cached == configured):
        return cached
    payload = await square_json("GET", "/v2/locations")
    chosen: dict | None = None
    for location in payload.get("locations") or []:
        if not isinstance(location, dict) or location.get("status") != "ACTIVE" or not location.get("id"):
            continue
        if configured and str(location["id"]) != configured:
            continue
        chosen = location
        break
    if chosen is None:
        if not configured:
            raise SquareAPIError(404, ["LOCATION_NOT_FOUND"])
        location_id = configured
        language = ""
    else:
        location_id = str(chosen["id"])
        language = str(chosen.get("language_code") or "")
    if not str(language).lower().startswith("ja"):
        await square_json(
            "PUT",
            f"/v2/locations/{location_id}",
            payload={"location": {"language_code": "ja-JP"}},
        )
    _resolved_ids["location"] = location_id
    return location_id


async def resolve_variation_id() -> str:
    configured = os.getenv("SQUARE_PLAN_VARIATION_ID", "").strip()
    if configured:
        return configured
    cached = _resolved_ids.get("variation")
    if cached:
        return cached
    objects: list[dict] = []
    cursor = ""
    for _ in range(20):
        params = {"types": "SUBSCRIPTION_PLAN,SUBSCRIPTION_PLAN_VARIATION"}
        if cursor:
            params["cursor"] = cursor
        page = await square_json("GET", "/v2/catalog/list", params=params)
        objects.extend(item for item in (page.get("objects") or []) if isinstance(item, dict))
        cursor = str(page.get("cursor") or "")
        if not cursor:
            break
    found = _variation_id(objects)
    if found:
        _resolved_ids["variation"] = found
        return found
    plan = await square_json(
        "POST",
        "/v2/catalog/object",
        payload={
            "idempotency_key": str(uuid.uuid4()),
            "object": {
                "type": "SUBSCRIPTION_PLAN",
                "id": "#slokyoku-plan",
                "present_at_all_locations": True,
                "subscription_plan_data": {"name": _PLAN_NAME},
            },
        },
    )
    plan_id = str((plan.get("catalog_object") or {}).get("id") or "")
    if not plan_id:
        raise SquareAPIError(502, ["PLAN_CREATE_FAILED"])
    variation = await square_json(
        "POST",
        "/v2/catalog/object",
        payload={
            "idempotency_key": str(uuid.uuid4()),
            "object": {
                "type": "SUBSCRIPTION_PLAN_VARIATION",
                "id": "#slokyoku-variation",
                "present_at_all_locations": True,
                "subscription_plan_variation_data": {
                    "name": _VARIATION_NAME,
                    "subscription_plan_id": plan_id,
                    "phases": subscription_phases(),
                },
            },
        },
    )
    variation_id = str((variation.get("catalog_object") or {}).get("id") or "")
    if not variation_id:
        raise SquareAPIError(502, ["VARIATION_CREATE_FAILED"])
    _resolved_ids["variation"] = variation_id
    return variation_id


async def upsert_customer(user_id: str, email: str) -> str:
    found = await square_json(
        "POST",
        "/v2/customers/search",
        payload={"query": {"filter": {"reference_id": {"exact": user_id}}}},
    )
    customers = [item for item in (found.get("customers") or []) if isinstance(item, dict)]
    if customers:
        customer = customers[0]
        customer_id = str(customer.get("id") or "")
        if customer.get("email_address") != email or not customer.get("given_name"):
            updated = await square_json(
                "PUT",
                f"/v2/customers/{customer_id}",
                payload={
                    "email_address": email,
                    "reference_id": user_id,
                    "given_name": "スロ極会員",
                    "version": customer.get("version"),
                },
            )
            customer_id = str((updated.get("customer") or {}).get("id") or customer_id)
        if not customer_id:
            raise SquareAPIError(502, ["CUSTOMER_UPDATE_FAILED"])
        return customer_id
    created = await square_json(
        "POST",
        "/v2/customers",
        payload={
            "idempotency_key": str(uuid.uuid4()),
            "email_address": email,
            "reference_id": user_id,
            "given_name": "スロ極会員",
        },
    )
    customer_id = str((created.get("customer") or {}).get("id") or "")
    if not customer_id:
        raise SquareAPIError(502, ["CUSTOMER_CREATE_FAILED"])
    return customer_id


async def has_open_subscription(customer_id: str, location_id: str) -> bool:
    found = await square_json(
        "POST",
        "/v2/subscriptions/search",
        payload={"query": {"filter": {"customer_ids": [customer_id], "location_ids": [location_id]}}},
    )
    for subscription in found.get("subscriptions") or []:
        if isinstance(subscription, dict) and subscription.get("status") in _OPEN_SUBSCRIPTION:
            return True
    return False


async def register_trial(user_id: str, email: str, base_url: str) -> str:
    if len(user_id) > 100:
        raise SquareAPIError(400, ["USER_ID_TOO_LONG"])
    public_base = normalize_public_base(base_url)
    location_id = await resolve_location_id()
    await resolve_variation_id()
    customer_id = await upsert_customer(user_id, email)
    line_name = await line_display_name(user_id)
    try:
        from src.dashboard.square_members import record_line_signup

        record_line_signup(
            line_user_id=user_id,
            email=email,
            square_customer_id=customer_id,
            line_name=line_name,
        )
    except Exception:
        logger.exception("member ledger update failed")
    if await has_open_subscription(customer_id, location_id):
        return _ALREADY
    token = issue_checkout_token(customer_id, email, user_id)
    logger.info("card checkout ready customer=%s", customer_id)
    return checkout_reply(checkout_page_url(public_base, token))


async def start_card_subscription(
    *,
    customer_id: str,
    email: str,
    user_id: str,
    source_id: str,
    verification_token: str = "",
) -> dict:
    if not source_id or len(source_id) > 2048 or any(char.isspace() for char in source_id):
        raise SquareAPIError(400, ["SOURCE_ID"])
    if verification_token and (len(verification_token) > 4096 or any(char.isspace() for char in verification_token)):
        raise SquareAPIError(400, ["SOURCE_ID"])
    location_id = await resolve_location_id()
    variation_id = await resolve_variation_id()
    if await has_open_subscription(customer_id, location_id):
        return {"ok": False, "code": "ALREADY"}
    card_payload: dict = {
        "idempotency_key": str(uuid.uuid4()),
        "source_id": source_id,
        "card": {"customer_id": customer_id, "cardholder_name": "スロ極会員"},
    }
    if verification_token:
        card_payload["verification_token"] = verification_token
    created_card = await square_json("POST", "/v2/cards", payload=card_payload)
    card_id = str((created_card.get("card") or {}).get("id") or "")
    if not card_id:
        raise SquareAPIError(502, ["CARD_CREATE_FAILED"])
    created = await square_json(
        "POST",
        "/v2/subscriptions",
        payload={
            "idempotency_key": str(uuid.uuid4()),
            "location_id": location_id,
            "customer_id": customer_id,
            "plan_variation_id": variation_id,
            "card_id": card_id,
            "start_date": subscription_start_date(),
        },
    )
    subscription = created.get("subscription") if isinstance(created.get("subscription"), dict) else {}
    subscription_id = str(subscription.get("id") or "")
    if not subscription_id:
        raise SquareAPIError(502, ["SUBSCRIPTION_CREATE_FAILED"])
    logger.info("card subscription ready id=%s", subscription_id)
    try:
        from src.dashboard.square_members import record_card_trial

        record_card_trial(
            line_user_id=user_id,
            email=email,
            square_customer_id=customer_id,
            subscription_id=subscription_id,
        )
    except Exception:
        logger.exception("member ledger trial update failed")
    return {"ok": True, "subscription_id": subscription_id}


async def handle_text(user_id: str, reply_token: str, text: str, base_url: str) -> None:
    if is_trial_intent(text):
        await line_reply(reply_token, _GUIDE)
        return
    if not is_email(text):
        await line_reply(reply_token, _WELCOME, quick_reply=_TRIAL_BUTTON)
        return
    try:
        message = await register_trial(user_id, text.strip(), base_url)
    except Exception:
        logger.exception("trial registration failed")
        await line_reply(reply_token, _FAILED)
        return
    await line_reply(reply_token, message)


@app.get("/health")
async def health() -> dict[str, bool]:
    return {"ok": True}


@app.post("/square-webhook")
async def square_webhook(request: Request) -> Response:
    from src.dashboard.square_members import SquareWebhookError
    from src.dashboard.square_members import handle_square_webhook

    body = await request.body()
    try:
        handle_square_webhook(body, request.headers.get("x-square-hmacsha256-signature", ""))
    except SquareWebhookError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return Response(status_code=200)


@app.post("/webhook")
async def webhook(request: Request) -> Response:
    body = await request.body()
    if len(body) > _MAX_BODY:
        raise HTTPException(status_code=413)
    secret = os.getenv("LINE_CHANNEL_SECRET", "").strip()
    signature = request.headers.get("x-line-signature", "")
    if not line_signature_valid(body, signature, secret):
        raise HTTPException(status_code=401)
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400) from None
    for event in payload.get("events") or []:
        if not isinstance(event, dict):
            continue
        source = event.get("source") or {}
        user_id = str(source.get("userId") or "") if isinstance(source, dict) else ""
        reply_token = str(event.get("replyToken") or "")
        if not user_id or not reply_token:
            continue
        if event.get("type") == "follow":
            await line_reply(reply_token, _WELCOME, quick_reply=_TRIAL_BUTTON)
            continue
        if event.get("type") != "message":
            continue
        message = event.get("message") or {}
        if not isinstance(message, dict) or message.get("type") != "text":
            continue
        await handle_text(user_id, reply_token, str(message.get("text") or ""), request_public_base(request))
    return Response(status_code=200)


_CHECKOUT_PAGE = """<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="referrer" content="origin">
  <title>__ITEM__</title>
  <style>
    :root { color-scheme: light; }
    body { margin: 0; font-family: "Hiragino Sans", "Noto Sans JP", sans-serif; background: #f4f1ea; color: #1c1915; }
    main { max-width: 28rem; margin: 0 auto; padding: 1.5rem 1rem 3rem; }
    h1 { font-size: 1.25rem; line-height: 1.5; margin: 0 0 1rem; }
    .price { background: #fff; border: 1px solid #e4dccf; border-radius: 12px; padding: 1rem 1.1rem; margin-bottom: 1rem; }
    .today { font-size: 0.85rem; margin: 0; color: #5c564c; }
    .yen { font-size: 2rem; font-weight: 700; margin: 0.15rem 0 0.6rem; }
    .note { margin: 0; line-height: 1.6; }
    form { background: #fff; border: 1px solid #e4dccf; border-radius: 12px; padding: 1rem 1.1rem 1.2rem; }
    #card-container { min-height: 5.5rem; margin: 0.8rem 0 1rem; }
    button { width: 100%; border: 0; border-radius: 999px; background: #1f6b45; color: #fff; font-size: 1rem; padding: 0.85rem 1rem; }
    button:disabled { opacity: 0.6; }
    .fine { color: #5c564c; font-size: 0.82rem; line-height: 1.6; }
    .error { color: #8d2b2b; }
    .hidden { display: none; }
  </style>
</head>
<body>
  <main>
    <h1>__ITEM__</h1>
    <section class="price">
      <p class="today">今日の支払い</p>
      <p class="yen">0円</p>
      <p class="note">月額2,980円です。最初の10日間は0円で、このページではカードを登録するだけです。11日目から、登録したカードに月額2,980円が自動で引き落とされます。10日以内に解約すれば、料金はかかりません。</p>
    </section>
    <form id="card-form" class="hidden">
      <p id="buyer-email" class="fine"></p>
      <div id="card-container"></div>
      <button id="submit" type="submit">カードを登録してはじめる</button>
      <p class="fine">カード番号は Square に直接送信されます。</p>
    </form>
    <p id="status" role="status">カードの入力欄を準備しています。</p>
  </main>
  <script>
    const statusNode = document.getElementById("status");
    const form = document.getElementById("card-form");
    const submit = document.getElementById("submit");
    const queryToken = new URLSearchParams(location.search).get("t") || "";
    const hashToken = new URLSearchParams((location.hash || "").replace(/^#/, "")).get("t") || "";
    const token = queryToken || hashToken;

    function show(message, isError) {
      statusNode.textContent = message;
      statusNode.className = isError ? "error" : "";
    }

    async function loadScript(src) {
      await new Promise((resolve, reject) => {
        const script = document.createElement("script");
        script.src = src;
        script.referrerPolicy = "origin";
        script.onload = resolve;
        script.onerror = reject;
        document.head.appendChild(script);
      });
    }

    async function start() {
      if (!token) {
        form.classList.add("hidden");
        show("この登録リンクは無効か、期限切れです。LINEにもう一度メールアドレスを送ってください。", true);
        return;
      }
      const configResponse = await fetch("/checkout/config?t=" + encodeURIComponent(token));
      const config = await configResponse.json();
      if (!configResponse.ok || !config.ok) {
        form.classList.add("hidden");
        show(config.message || "登録ページを開けませんでした。", true);
        return;
      }
      document.getElementById("buyer-email").textContent = config.email;
      await loadScript(config.scriptUrl);
      const payments = window.Square.payments(config.applicationId, config.locationId);
      const card = await payments.card({ style: { input: { fontSize: "16px" } } });
      await card.attach("#card-container");
      form.classList.remove("hidden");
      show("", false);
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        submit.disabled = true;
        show("カードを登録しています。", false);
        try {
          const tokenResult = await card.tokenize({
            intent: "STORE",
            customerInitiated: true,
            sellerKeyedIn: false,
            billingContact: { givenName: "スロ極会員", email: config.email, countryCode: "JP" }
          });
          if (tokenResult.status !== "OK" || !tokenResult.token) {
            throw new Error("card");
          }
          const saved = await fetch("/checkout/complete", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              token: token,
              sourceId: tokenResult.token,
              verificationToken: tokenResult.verificationToken || ""
            })
          });
          const body = await saved.json();
          if (!saved.ok || !body.ok) {
            show(body.message || "登録を完了できませんでした。", true);
            submit.disabled = false;
            return;
          }
          form.classList.add("hidden");
          show(body.message, false);
        } catch (error) {
          show("カードを登録できませんでした。入力内容を確認して、もう一度お試しください。", true);
          submit.disabled = false;
        }
      });
    }

    start().catch(() => {
      form.classList.add("hidden");
      show("登録ページを開けませんでした。時間をおいて、もう一度開いてください。", true);
    });
  </script>
</body>
</html>
"""


def checkout_page_html() -> str:
    return _CHECKOUT_PAGE.replace("__ITEM__", _CHECKOUT_ITEM_NAME)


@app.get("/checkout")
async def checkout_page() -> HTMLResponse:
    return HTMLResponse(checkout_page_html(), headers={"Cache-Control": "no-store"})


@app.get("/checkout/config")
async def checkout_config(t: str = "") -> JSONResponse:
    try:
        claims = read_checkout_token(t)
        location_id = await resolve_location_id()
    except SquareAPIError as exc:
        return checkout_error(400, buyer_message(exc))
    application_id = os.getenv("SQUARE_APPLICATION_ID", "").strip()
    if not application_id:
        return checkout_error(500, "カード登録の準備ができていません。")
    return JSONResponse(
        {
            "ok": True,
            "applicationId": application_id,
            "locationId": location_id,
            "scriptUrl": square_web_sdk_url(),
            "email": str(claims["email"]),
        }
    )


@app.post("/checkout/complete")
async def checkout_complete(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return checkout_error(400, "登録内容を確認できませんでした。")
    if not isinstance(body, dict):
        return checkout_error(400, "登録内容を確認できませんでした。")
    try:
        claims = read_checkout_token(str(body.get("token") or ""))
        result = await start_card_subscription(
            customer_id=str(claims["customer_id"]),
            email=str(claims["email"]),
            user_id=str(claims["user_id"]),
            source_id=str(body.get("sourceId") or "").strip(),
            verification_token=str(body.get("verificationToken") or "").strip(),
        )
    except SquareAPIError as exc:
        logger.warning("checkout failed status=%s codes=%s", exc.status, ",".join(exc.codes))
        return checkout_error(400, buyer_message(exc))
    except Exception:
        logger.exception("checkout failed")
        return checkout_error(400, "登録を完了できませんでした。時間をおいて、もう一度お試しください。")
    if not result.get("ok"):
        return checkout_error(409, "このアカウントは登録済みです。")
    return JSONResponse(
        {
            "ok": True,
            "message": "登録が完了しました。今日の支払いは0円です。11日目から、登録したカードに月額2,980円が自動で引き落とされます。",
        }
    )


register_member_admin(app)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
