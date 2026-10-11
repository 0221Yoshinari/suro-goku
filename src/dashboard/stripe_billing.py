"""Stripe の決済開始と Webhook。

秘密鍵と Webhook 秘密はサーバの環境変数だけに置く。
ブラウザへ返すのは、Stripe 自身の決済画面へのリダイレクトと、その人のライセンスキーだけ。
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import re
import time
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from urllib.parse import quote
from urllib.parse import urlparse

import httpx
from fastapi import Request
from fastapi.responses import HTMLResponse

from src.dashboard.members import MembersStoreError
from src.dashboard.members import grant_checkout
from src.dashboard.members import grant_subscription
from src.dashboard.members import event_seen
from src.dashboard.members import member_for_checkout
from src.dashboard.members import present_member
from src.dashboard.members import remember_event

_TEMPLATES = Path(__file__).resolve().parent / "templates"
_THANKS_TEMPLATE: str | None = None
_SESSION_RE = re.compile(r"^cs_[A-Za-z0-9_]{8,200}$")
_PRICE_RE = re.compile(r"^price_[A-Za-z0-9]+$")
_JST = timezone(timedelta(hours=9))
_CHECKOUT_EVENTS = {"checkout.session.completed", "checkout.session.async_payment_succeeded"}
_SUBSCRIPTION_EVENTS = {"customer.subscription.created", "customer.subscription.updated"}
_STATUS_LABEL = {
    "active": "利用できます",
    "trialing": "無料体験中です",
    "trial_expired": "無料体験は終了しています",
    "expired": "利用期限が切れています",
    "canceled": "解約済みのため、いまは利用できません",
    "past_due": "お支払いが確認できないため停止しています",
    "unpaid": "お支払いが確認できないため停止しています",
    "incomplete": "決済が完了していません",
    "incomplete_expired": "決済が完了していません",
    "paused": "利用を停止しています",
}


class WebhookError(Exception):
    def __init__(self, status_code: int, public_detail: str) -> None:
        super().__init__(public_detail)
        self.status_code = status_code
        self.public_detail = public_detail


def verify_stripe_signature(
    payload: bytes,
    header: str,
    secret: str,
    now: float | None = None,
    tolerance: int = 300,
) -> bool:
    if not payload or not header or not secret:
        return False
    timestamp = None
    signatures: list[str] = []
    for item in header.split(","):
        key, _, value = item.strip().partition("=")
        key = key.strip()
        value = value.strip()
        if key == "t" and value.isdigit():
            timestamp = int(value)
        elif key == "v1" and value:
            signatures.append(value)
    if timestamp is None or not signatures:
        return False
    current = int(now if now is not None else time.time())
    if abs(current - timestamp) > tolerance:
        return False
    signed = f"{timestamp}.".encode("ascii") + payload
    expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, candidate) for candidate in signatures)


def handle_webhook(payload: bytes, header: str) -> str:
    secret = (os.environ.get("STRIPE_WEBHOOK_SECRET") or "").strip()
    if not secret:
        raise WebhookError(503, "決済通知の受付は準備中です")
    if len(payload) > 1_000_000 or not verify_stripe_signature(payload, header, secret):
        raise WebhookError(400, "署名を確認できません")
    try:
        event = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebhookError(400, "通知を確認できません") from exc
    if not isinstance(event, dict):
        raise WebhookError(400, "通知を確認できません")
    event_id = str(event.get("id") or "").strip()
    if not event_id or len(event_id) > 128:
        raise WebhookError(400, "通知を確認できません")
    try:
        if event_seen(event_id):
            return "duplicate"
        result = _dispatch(event)
        remember_event(event_id)
    except MembersStoreError as exc:
        raise WebhookError(500, "処理に失敗しました。しばらくしてから再度お試しください。") from exc
    kind = str(event.get("type") or "")
    print(f"stripe webhook: {kind} {result}", flush=True)
    return result


def checkout_redirect_url(request: Request) -> str:
    created = _create_checkout(request)
    if created:
        return created
    return payment_link_url()


def payment_link_url() -> str:
    raw = (os.environ.get("STRIPE_CHECKOUT_URL") or "").strip()
    return _safe_stripe_url(raw)


def render_thanks(session_id: str) -> HTMLResponse:
    session_id = (session_id or "").strip()
    if not session_id:
        body = _links("決済が終わると、Stripe からこのページへ戻ります。ライセンスキーはここに表示します。")
        return _page("お申し込み完了", "戻ってきたこの画面で、ツールへ進みます。", body, "", 200)
    if not _SESSION_RE.fullmatch(session_id):
        body = _links("紹介ページから決済をやり直してください。")
        return _page("ページを開けません", "決済番号を確認できません。", body, "", 400)
    member = lookup_checkout_member(session_id)
    if member is None:
        body = _links("しばらくしても表示されない場合は、時間をおいて開き直してください。")
        head = '<meta http-equiv="refresh" content="4" />'
        return _page(
            "決済を確認しています",
            "反映まで数秒かかることがあります。このページは自動で更新されます。",
            body,
            head,
            200,
        )
    return _page(
        "お申し込みを確認しました" if member.get("entitled") else "現在は利用できません",
        "ライセンスキーはこの画面で確認できます。ほかの人へ渡さないでください。",
        _member_block(member),
        "",
        200,
    )


def lookup_checkout_member(session_id: str) -> dict | None:
    if not _SESSION_RE.fullmatch(session_id or ""):
        return None
    found = member_for_checkout(session_id)
    if found is None:
        remote = _fetch_checkout(session_id)
        if remote is None or not _checkout_payable(remote):
            return None
        when = _unix(remote.get("created")) or 0
        grant_checkout(
            when=when,
            session_id=session_id,
            customer_id=_stripe_id(remote.get("customer")),
            subscription_id=_stripe_id(remote.get("subscription")),
            email=_email(remote),
        )
        subscription = remote.get("subscription")
        if isinstance(subscription, dict):
            _apply_subscription(subscription, _unix(subscription.get("created")) or when, deleted=False)
        found = member_for_checkout(session_id)
    if not found:
        return None
    return present_member(str(found.get("id") or ""))


def public_base_url(request: Request) -> str:
    configured = (os.environ.get("DASHBOARD_URL") or "").strip().rstrip("/")
    if configured.startswith("https://") or configured.startswith("http://"):
        parsed = urlparse(configured)
        if parsed.hostname:
            return configured
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "http").split(",")[0].strip()
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip()
    if not host:
        server = request.scope.get("server") or ("", 0)
        name = str(server[0] or "")
        port = server[1] if len(server) > 1 else ""
        if name:
            host = f"{name}:{port}" if port else name
    if not host or proto not in {"http", "https"}:
        return ""
    return f"{proto}://{host}"


def _dispatch(event: dict) -> str:
    kind = str(event.get("type") or "")
    data = event.get("data")
    obj = data.get("object") if isinstance(data, dict) else None
    if not isinstance(obj, dict):
        return "ignored"
    when = _unix(event.get("created")) or _unix(obj.get("created")) or 0
    if kind in _CHECKOUT_EVENTS:
        if not _checkout_payable(obj):
            return "ignored"
        grant_checkout(
            when=when,
            session_id=_stripe_id(obj.get("id")) or str(obj.get("id") or ""),
            customer_id=_stripe_id(obj.get("customer")),
            subscription_id=_stripe_id(obj.get("subscription")),
            email=_email(obj),
        )
        return "ok"
    if kind in _SUBSCRIPTION_EVENTS:
        _apply_subscription(obj, when, deleted=False)
        return "ok"
    if kind == "customer.subscription.deleted":
        _apply_subscription(obj, when, deleted=True)
        return "ok"
    return "ignored"


def _apply_subscription(obj: dict, when: int, *, deleted: bool) -> None:
    grant_subscription(
        when=when,
        customer_id=_stripe_id(obj.get("customer")),
        subscription_id=_stripe_id(obj.get("id")),
        status=str(obj.get("status") or ""),
        trial_end=_unix(obj.get("trial_end")),
        period_end=_period_end(obj),
        deleted=deleted,
    )


def _checkout_payable(obj: dict) -> bool:
    if obj.get("status") != "complete":
        return False
    if obj.get("payment_status") not in {"paid", "no_payment_required"}:
        return False
    mode = str(obj.get("mode") or "subscription")
    return mode == "subscription" or bool(obj.get("subscription"))


def _create_checkout(request: Request) -> str:
    secret = _stripe_secret()
    price = (os.environ.get("STRIPE_PRICE_ID") or "").strip()
    if not secret or not _PRICE_RE.fullmatch(price):
        return ""
    base = public_base_url(request)
    if not base:
        return ""
    items = [
        ("mode", "subscription"),
        ("line_items[0][price]", price),
        ("line_items[0][quantity]", "1"),
        ("success_url", f"{base}/thanks?session_id={{CHECKOUT_SESSION_ID}}"),
        ("cancel_url", f"{base}/lp"),
        ("locale", "ja"),
        ("allow_promotion_codes", "true"),
    ]
    trial = (os.environ.get("STRIPE_TRIAL_DAYS") or "").strip()
    if trial.isdigit() and 0 < int(trial) <= 60:
        items.append(("subscription_data[trial_period_days]", str(int(trial))))
    try:
        response = httpx.post(
            "https://api.stripe.com/v1/checkout/sessions",
            headers={"Authorization": f"Bearer {secret}"},
            content=_form(items),
            timeout=15.0,
        )
    except httpx.HTTPError:
        print("stripe checkout create failed", flush=True)
        return ""
    if response.status_code != 200:
        print(f"stripe checkout create failed: {response.status_code}", flush=True)
        return ""
    try:
        payload = response.json()
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict):
        return ""
    return _safe_stripe_url(str(payload.get("url") or ""))


def _fetch_checkout(session_id: str) -> dict | None:
    secret = _stripe_secret()
    if not secret:
        return None
    try:
        response = httpx.get(
            f"https://api.stripe.com/v1/checkout/sessions/{session_id}",
            headers={"Authorization": f"Bearer {secret}"},
            params=[("expand[]", "subscription")],
            timeout=15.0,
        )
    except httpx.HTTPError:
        print("stripe checkout lookup failed", flush=True)
        return None
    if response.status_code != 200:
        print(f"stripe checkout lookup failed: {response.status_code}", flush=True)
        return None
    try:
        payload = response.json()
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or payload.get("id") != session_id:
        return None
    return payload


def _stripe_secret() -> str:
    raw = (os.environ.get("STRIPE_SECRET_KEY") or "").strip()
    if raw.startswith(("sk_live_", "sk_test_", "rk_live_", "rk_test_")):
        return raw
    return ""


def _safe_stripe_url(raw: str) -> str:
    parsed = urlparse((raw or "").strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "https" and host.endswith("stripe.com"):
        return raw.strip()
    return ""


def _form(items: list[tuple[str, str]]) -> bytes:
    parts: list[str] = []
    for key, value in items:
        safe = "{}" if "{CHECKOUT_SESSION_ID}" in value else ""
        parts.append(quote(key, safe="[]") + "=" + quote(value, safe=safe))
    return "&".join(parts).encode("utf-8")


def _stripe_id(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return str(value.get("id") or "").strip()
    return ""


def _email(obj: dict) -> str:
    direct = str(obj.get("customer_email") or "").strip()
    if direct:
        return direct
    details = obj.get("customer_details")
    if isinstance(details, dict):
        return str(details.get("email") or "").strip()
    return ""


def _unix(value: object) -> int | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _period_end(obj: dict) -> int | None:
    direct = _unix(obj.get("current_period_end"))
    if direct:
        return direct
    items = obj.get("items")
    rows = items.get("data") if isinstance(items, dict) else None
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        return _unix(rows[0].get("current_period_end"))
    return None


def _member_block(member: dict) -> str:
    status = str(member.get("status") or "")
    label = member.get("message") or _STATUS_LABEL.get(status) or "状態を確認できません"
    rows = [f"<p>{html.escape(str(label))}</p>"]
    email = str(member.get("email") or "")
    if email:
        rows.append(f'<p class="login-note">登録メール: {html.escape(email)}</p>')
    trial_end = member.get("trial_end")
    if status == "trialing" and trial_end:
        rows.append(f'<p class="login-note">無料体験の終了: {html.escape(_format_jst(trial_end))}</p>')
    key = str(member.get("license_key") or "")
    if key:
        rows.append(
            '<label class="login-label" for="license-key">ライセンスキー</label>'
            f'<input id="license-key" class="license-key" readonly value="{html.escape(key, quote=True)}" />'
        )
    if member.get("entitled") and key:
        rows.append(
            '<form method="post" action="/login" class="login-form">'
            f'<input type="hidden" name="password" value="{html.escape(key, quote=True)}" />'
            '<input type="hidden" name="next" value="/" />'
            '<button type="submit" class="primary">この端末でツールを開く</button>'
            "</form>"
            '<p class="login-note">同時に使えるのは1台です。端末を変えるときは、先にログアウトしてください。</p>'
        )
    rows.append('<p class="login-links"><a href="/login">ログイン画面</a> · <a href="/lp">紹介ページ</a></p>')
    return "".join(rows)


def _links(text: str) -> str:
    return f"<p>{html.escape(text)}</p>" '<p class="login-links"><a href="/lp">紹介ページ</a> · <a href="/login">ログイン画面</a></p>'


def _format_jst(stamp: object) -> str:
    try:
        moment = datetime.fromtimestamp(int(stamp), _JST)
    except (TypeError, ValueError, OSError):
        return ""
    return moment.strftime("%Y-%m-%d %H:%M")


def _page(title: str, lead: str, body: str, head: str, status_code: int) -> HTMLResponse:
    template = _thanks_template()
    html_body = (
        template.replace("__HEAD__", head)
        .replace("__TITLE__", html.escape(title))
        .replace("__LEAD__", html.escape(lead))
        .replace("__BODY__", body)
    )
    return HTMLResponse(html_body, status_code=status_code, headers={"Cache-Control": "no-store"})


def _thanks_template() -> str:
    global _THANKS_TEMPLATE
    if _THANKS_TEMPLATE is None:
        _THANKS_TEMPLATE = (_TEMPLATES / "thanks.html").read_text(encoding="utf-8")
    return _THANKS_TEMPLATE
