"""LINE のメールアドレス受信から、Square の決済リンクを返す。

起動: uvicorn main:app --host 0.0.0.0 --port 8000

定期購入は初回 0 円、その後月額 2,980 円。決済 URL は LINE のトークへ送る。
/square-webhook は会員台帳のステータスを更新する。ツール本体は同じ台帳を見て、
無料体験中と有料会員以外をその次のアクセスから止める。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import uuid
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Request
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
_SQUARE_VERSION = "2026-09-16"
_PLAN_NAME = "スロ極"
_VARIATION_NAME = "スロ極 初回0円・以降月額2980円"
_OPEN_SUBSCRIPTION = {"PENDING", "ACTIVE"}
_TRIAL_BUTTON = "🚀 7日間無料でスロ極を試す"
_WELCOME = "スロ極へようこそ。7日間、全機能を無料で試せます。\n下のボタンから、1分で登録を始められます。"
_GUIDE = (
    "スロ極の7日間無料トライアルへようこそ！✨\n"
    "\n"
    "このチャットに【普段お使いのメールアドレス】を送信してください。すぐに専用の決済リンクをお届けします！\n"
    "\n"
    "※7日間の無料期間内に解約すれば、料金は一切かかりません。"
)
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
    return normalized.replace("🚀", "").strip() == "7日間無料でスロ極を試す"


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


def subscription_phases() -> list[dict]:
    return [
        {
            "ordinal": 0,
            "cadence": "MONTHLY",
            "periods": 1,
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


def build_payment_link_payload(
    *,
    location_id: str,
    plan_variation_id: str,
    email: str,
    user_id: str,
    idempotency_key: str,
) -> dict:
    return {
        "idempotency_key": idempotency_key,
        "description": "スロ極 月額プラン",
        "quick_pay": {
            "name": "スロ極 月額",
            "price_money": {"amount": _INITIAL_AMOUNT_JPY, "currency": "JPY"},
            "location_id": location_id,
        },
        "checkout_options": {
            "subscription_plan_id": plan_variation_id,
            "ask_for_shipping_address": False,
        },
        "pre_populated_data": {"buyer_email": email},
        "payment_note": f"LINE:{user_id}"[:500],
    }


def checkout_url(payload: dict) -> str:
    link = payload.get("payment_link")
    raw = str(link.get("url") or "").strip() if isinstance(link, dict) else ""
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (host == "square.link" or host.endswith(".square.link")):
        raise SquareAPIError(502, ["PAYMENT_LINK_URL"])
    return raw


def checkout_reply(url: str) -> str:
    return f"決済ページを開いて、登録を完了してください。\n{url}"


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
    if configured:
        return configured
    cached = _resolved_ids.get("location")
    if cached:
        return cached
    payload = await square_json("GET", "/v2/locations")
    for location in payload.get("locations") or []:
        if isinstance(location, dict) and location.get("status") == "ACTIVE" and location.get("id"):
            location_id = str(location["id"])
            _resolved_ids["location"] = location_id
            return location_id
    raise SquareAPIError(404, ["LOCATION_NOT_FOUND"])


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


async def register_trial(user_id: str, email: str) -> str:
    if len(user_id) > 100:
        raise SquareAPIError(400, ["USER_ID_TOO_LONG"])
    location_id = await resolve_location_id()
    variation_id = await resolve_variation_id()
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
    created = await square_json(
        "POST",
        "/v2/online-checkout/payment-links",
        payload=build_payment_link_payload(
            location_id=location_id,
            plan_variation_id=variation_id,
            email=email,
            user_id=user_id,
            idempotency_key=str(uuid.uuid4()),
        ),
    )
    url = checkout_url(created)
    link_id = str((created.get("payment_link") or {}).get("id") or "")
    logger.info("square payment link ready id=%s", link_id)
    return checkout_reply(url)


async def handle_text(user_id: str, reply_token: str, text: str) -> None:
    if is_trial_intent(text):
        await line_reply(reply_token, _GUIDE)
        return
    if not is_email(text):
        await line_reply(reply_token, _WELCOME, quick_reply=_TRIAL_BUTTON)
        return
    try:
        message = await register_trial(user_id, text.strip())
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
        await handle_text(user_id, reply_token, str(message.get("text") or ""))
    return Response(status_code=200)


register_member_admin(app)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
