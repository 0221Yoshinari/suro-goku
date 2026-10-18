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
from urllib.parse import parse_qsl
from urllib.parse import quote
from urllib.parse import urlencode
from urllib.parse import urlparse
from urllib.parse import urlunparse

import httpx
from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.responses import RedirectResponse

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


_LINK_FALLBACK_LOGGED = False


def checkout_redirect_url(request: Request, *, member_id: str = "", email: str = "") -> str:
    global _LINK_FALLBACK_LOGGED
    created = _create_checkout(request, member_id=member_id, email=email)
    if created:
        return created
    fallback = _with_account_query(payment_link_url(), member_id, email)
    if fallback and not _LINK_FALLBACK_LOGGED:
        _LINK_FALLBACK_LOGGED = True
        print("STRIPE_PRICE_ID または STRIPE_SECRET_KEY が無いため、Payment Link へ戻します", flush=True)
    return fallback


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
            name=_person_name(remote),
            member_id=_account_id_from_obj(remote),
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
            name=_person_name(obj),
            member_id=_account_id_from_obj(obj),
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
    email, name = _subscription_contact(obj)
    customer_id = _stripe_id(obj.get("customer"))
    if customer_id and (not email or not name):
        fetched_email, fetched_name = _fetch_customer_contact(customer_id)
        email = email or fetched_email
        name = name or fetched_name
    grant_subscription(
        when=when,
        customer_id=customer_id,
        subscription_id=_stripe_id(obj.get("id")),
        status=str(obj.get("status") or ""),
        trial_end=_unix(obj.get("trial_end")),
        trial_start=_unix(obj.get("trial_start")),
        period_end=_period_end(obj),
        deleted=deleted,
        member_id=_account_id_from_obj(obj),
        email=email,
        name=name,
        plan_name=_plan_name(obj),
        started_at=_unix(obj.get("start_date")) or _unix(obj.get("created")),
    )


def _checkout_payable(obj: dict) -> bool:
    if obj.get("status") != "complete":
        return False
    if obj.get("payment_status") not in {"paid", "no_payment_required"}:
        return False
    mode = str(obj.get("mode") or "subscription")
    return mode == "subscription" or bool(obj.get("subscription"))


def _create_checkout(request: Request, *, member_id: str = "", email: str = "") -> str:
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
        ("cancel_url", f"{base}/billing" if _account_id(member_id) else f"{base}/lp"),
        ("locale", "ja"),
        ("allow_promotion_codes", "true"),
    ]
    account_id = _account_id(member_id)
    if account_id:
        items.append(("client_reference_id", account_id))
        items.append(("metadata[user_id]", account_id))
        items.append(("subscription_data[metadata][user_id]", account_id))
    cleaned_email = (email or "").strip()
    if cleaned_email and "@" in cleaned_email and len(cleaned_email) <= 200:
        items.append(("customer_email", cleaned_email))
    trial_days = _trial_days()
    if 0 < trial_days <= 60:
        items.append(("subscription_data[trial_period_days]", str(trial_days)))
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


def start_member_checkout(request: Request) -> HTMLResponse | RedirectResponse:
    """ログイン中の会員を、自システムのユーザーID付きで Stripe へ送る。"""
    member_id, email = account_from_request(request)
    if not member_id:
        return RedirectResponse("/login?next=/checkout", status_code=303)
    url = checkout_redirect_url(request, member_id=member_id, email=email)
    if not url:
        body = "<p>決済画面は準備中です。</p>" '<p class="login-links"><a href="/billing">戻る</a></p>'
        return _page("決済画面は準備中です", "しばらくしてから、もう一度お試しください。", body, "", 503)
    return RedirectResponse(url, status_code=303)


def account_from_request(request: Request) -> tuple[str, str]:
    from src.dashboard.auth import COOKIE_NAME
    from src.dashboard.auth import client_fp
    from src.dashboard.auth import client_ip
    from src.dashboard.auth import member_token_claims
    from src.dashboard.members import member_session_live

    claims = member_token_claims(request.cookies.get(COOKIE_NAME))
    if not claims or not member_session_live(claims[0], claims[1], client_fp(request), client_ip(request)):
        return "", ""
    member = present_member(claims[0]) or {}
    return str(member.get("id") or ""), str(member.get("email") or "")


def _with_account_query(url: str, member_id: str, email: str) -> str:
    """Payment Link でも、ログイン中のユーザーIDを決済に載せる。"""
    account = _account_id(member_id)
    if not url or not account:
        return url
    parsed = urlparse(url)
    pairs = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key not in {"client_reference_id", "prefilled_email"}]
    pairs.append(("client_reference_id", account))
    cleaned = (email or "").strip()
    if cleaned and "@" in cleaned and len(cleaned) <= 200:
        pairs.append(("prefilled_email", cleaned))
    return urlunparse(parsed._replace(query=urlencode(pairs)))


def _trial_days() -> int:
    raw = os.environ.get("STRIPE_TRIAL_DAYS")
    if raw is None or not str(raw).strip():
        return 7
    text = str(raw).strip()
    if text.isdigit():
        return int(text)
    return 0


def _account_id(value: object) -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"m_[0-9a-f]{16}", text):
        return text
    return ""


def _account_id_from_obj(obj: dict) -> str:
    direct = _account_id(obj.get("client_reference_id"))
    if direct:
        return direct
    meta = obj.get("metadata")
    if isinstance(meta, dict):
        return _account_id(meta.get("user_id"))
    return ""


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
        found = str(details.get("email") or "").strip()
        if found:
            return found
    customer = obj.get("customer")
    if isinstance(customer, dict):
        return str(customer.get("email") or "").strip()
    return ""


def _person_name(obj: dict) -> str:
    details = obj.get("customer_details")
    if isinstance(details, dict):
        found = str(details.get("name") or "").strip()
        if found:
            return found
    customer = obj.get("customer")
    if isinstance(customer, dict):
        return str(customer.get("name") or "").strip()
    return ""


def _subscription_contact(obj: dict) -> tuple[str, str]:
    email = ""
    name = ""
    customer = obj.get("customer")
    if isinstance(customer, dict):
        email = str(customer.get("email") or "").strip()
        name = str(customer.get("name") or "").strip()
    if not email:
        email = str(obj.get("customer_email") or "").strip()
    if not name:
        name = _person_name(obj)
    return email, name


def _plan_name(obj: dict) -> str:
    price = _subscription_price(obj)
    if price is None:
        return ""
    nickname = str(price.get("nickname") or "").strip()
    if nickname:
        return nickname
    product = price.get("product")
    if isinstance(product, dict):
        return str(product.get("name") or "").strip()
    return ""


def _subscription_price(obj: dict) -> dict | None:
    items = obj.get("items")
    rows = items.get("data") if isinstance(items, dict) else None
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            price = row.get("price") if isinstance(row.get("price"), dict) else row.get("plan")
            if isinstance(price, dict):
                return price
    plan = obj.get("plan")
    if isinstance(plan, dict):
        return plan
    return None


def _fetch_customer_contact(customer_id: str) -> tuple[str, str]:
    if not re.fullmatch(r"cus_[A-Za-z0-9]+", customer_id or ""):
        return "", ""
    secret = _stripe_secret()
    if not secret:
        return "", ""
    try:
        response = httpx.get(
            f"https://api.stripe.com/v1/customers/{customer_id}",
            headers={"Authorization": f"Bearer {secret}"},
            timeout=8.0,
        )
    except httpx.HTTPError:
        return "", ""
    if response.status_code != 200:
        return "", ""
    try:
        payload = response.json()
    except json.JSONDecodeError:
        return "", ""
    if not isinstance(payload, dict):
        return "", ""
    return str(payload.get("email") or "").strip(), str(payload.get("name") or "").strip()


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
            '<p class="login-note">別の端末でログインすると、こちらの端末はログアウトされます。</p>'
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
