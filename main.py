"""LINE のメールアドレス受信から、Square の定期購入を作る。

起動: uvicorn main:app --host 0.0.0.0 --port 8000

登録日の10日後に定期購入を開始する。最初の請求期間は 0 円、その後は月額 2,980 円。
カード未登録の顧客には、Square が請求メールを送る。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import uuid
from datetime import date
from datetime import datetime
from datetime import timedelta
from datetime import timezone

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Request
from fastapi.responses import Response

load_dotenv()

logger = logging.getLogger("surogoku.billing")

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

_JST = timezone(timedelta(hours=9))
_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)+$"
)
_MAX_BODY = 1_000_000
_TRIAL_DAYS = 10
_INITIAL_AMOUNT_JPY = 0
_MONTHLY_AMOUNT_JPY = 2980
_SQUARE_VERSION = "2026-09-16"
_PLAN_NAME = "スロ極"
_VARIATION_NAME = "スロ極 初回0円・以降月額2980円"
_OPEN_SUBSCRIPTION = {"PENDING", "ACTIVE"}
_ASK_EMAIL = "利用を開始するには、メールアドレスをこのトークに送ってください。"
_TRIAL_SENT = "10日間の無料トライアル案内メールを送信しました。メールをご確認ください。"
_ALREADY = "このLINEアカウントは登録済みです。案内メールをご確認ください。"
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


def line_signature_valid(body: bytes, signature: str, secret: str) -> bool:
    if not body or not signature or not secret:
        return False
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("ascii")
    return hmac.compare_digest(expected, signature.strip())


def subscription_start_date(on: date | None = None) -> str:
    base = on or datetime.now(_JST).date()
    return (base + timedelta(days=_TRIAL_DAYS)).isoformat()


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


def build_subscription_payload(
    *,
    location_id: str,
    plan_variation_id: str,
    customer_id: str,
    start_date: str,
    idempotency_key: str,
) -> dict:
    return {
        "idempotency_key": idempotency_key,
        "location_id": location_id,
        "plan_variation_id": plan_variation_id,
        "customer_id": customer_id,
        "start_date": start_date,
        "timezone": "Asia/Tokyo",
        "source": {"name": "スロ極"},
    }


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


async def line_reply(reply_token: str, text: str) -> None:
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    if not token or not reply_token:
        raise SquareAPIError(500, ["MISSING_LINE_TOKEN"])
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            "https://api.line.me/v2/bot/message/reply",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"replyToken": reply_token, "messages": [{"type": "text", "text": text}]},
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
    if await has_open_subscription(customer_id, location_id):
        return _ALREADY
    start_date = subscription_start_date()
    created = await square_json(
        "POST",
        "/v2/subscriptions",
        payload=build_subscription_payload(
            location_id=location_id,
            plan_variation_id=variation_id,
            customer_id=customer_id,
            start_date=start_date,
            idempotency_key=str(uuid.uuid4()),
        ),
    )
    subscription_id = str((created.get("subscription") or {}).get("id") or "")
    logger.info("square subscription ready id=%s start=%s", subscription_id, start_date)
    return _TRIAL_SENT


async def handle_text(user_id: str, reply_token: str, text: str) -> None:
    if not is_email(text):
        await line_reply(reply_token, _ASK_EMAIL)
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
        if not isinstance(event, dict) or event.get("type") != "message":
            continue
        message = event.get("message") or {}
        if not isinstance(message, dict) or message.get("type") != "text":
            continue
        source = event.get("source") or {}
        user_id = str(source.get("userId") or "") if isinstance(source, dict) else ""
        reply_token = str(event.get("replyToken") or "")
        if not user_id or not reply_token:
            continue
        await handle_text(user_id, reply_token, str(message.get("text") or ""))
    return Response(status_code=200)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
