from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

os.environ.setdefault("DASHBOARD_PASSWORD", "2026")
os.environ.setdefault("DASHBOARD_SESSION_DAYS", "7")

from src.dashboard.app import app
from src.dashboard.members import grant_checkout
from src.dashboard.members import grant_subscription
from src.dashboard.members import open_member_session
from src.dashboard.stripe_billing import verify_stripe_signature


def request(method: str, url: str, *, body: bytes = b"", headers: dict | None = None, cookies: dict | None = None):
    import asyncio
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    header_list = []
    for key, value in (headers or {}).items():
        header_list.append((key.lower().encode(), value.encode()))
    if cookies:
        cookie_header = "; ".join(f"{key}={value}" for key, value in cookies.items())
        header_list.append((b"cookie", cookie_header.encode()))
    header_list.append((b"content-length", str(len(body)).encode()))

    scope = {
        "type": "http",
        "asgi": {"spec_version": "2.3", "version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": parts.path or "/",
        "raw_path": (parts.path or "/").encode(),
        "query_string": parts.query.encode(),
        "headers": header_list,
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            await asyncio.Future()
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    messages = []

    async def send(message):
        messages.append(message)

    asyncio.run(app(scope, receive, send))
    start = next(message for message in messages if message["type"] == "http.response.start")
    payload = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    response_headers = {}
    set_cookies = []
    for key, value in start["headers"]:
        name = key.decode().lower()
        text = value.decode()
        if name == "set-cookie":
            set_cookies.append(text)
            continue
        response_headers[name] = text
    return start["status"], response_headers, payload, set_cookies


def absorb_cookies(jar: dict, set_cookies: list[str]) -> None:
    for raw in set_cookies:
        pair, _, rest = raw.partition(";")
        name, _, value = pair.partition("=")
        if "max-age=0" in rest.lower():
            jar.pop(name, None)
        else:
            jar[name] = value


def sign(payload: bytes, secret: str, timestamp: int | None = None) -> str:
    stamp = int(time.time() if timestamp is None else timestamp)
    signed = f"{stamp}.".encode("ascii") + payload
    signature = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"t={stamp},v1={signature}"


def checkout_event(event_id: str = "evt_checkout", created: int = 1_700_000_000) -> dict:
    return {
        "id": event_id,
        "type": "checkout.session.completed",
        "created": created,
        "data": {
            "object": {
                "id": "cs_test_abcdefgh",
                "mode": "subscription",
                "status": "complete",
                "payment_status": "paid",
                "customer": "cus_123",
                "customer_email": "member@example.com",
                "subscription": "sub_123",
            }
        },
    }


class StripeMemberTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Path(self.tmp.name) / "members.json"
        self.env = mock.patch.dict(
            os.environ,
            {
                "MEMBER_STORE_PATH": str(self.store),
                "DATABASE_URL": "",
                "STRIPE_WEBHOOK_SECRET": "whsec_test_secret",
                "STRIPE_SECRET_KEY": "",
                "STRIPE_PRICE_ID": "",
                "STRIPE_CHECKOUT_URL": "",
                "DASHBOARD_PASSWORD": "2026",
                "DASHBOARD_SESSION_DAYS": "7",
                "MEMBER_DISABLE_SHARED_PASSWORD": "",
                "SCRAPER_PROXY_PASS": "proxy-should-not-leak",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.cookies: dict[str, str] = {}

    def call(self, method: str, url: str, *, data: dict | None = None, body: bytes | None = None, headers: dict | None = None):
        headers = dict(headers or {})
        payload = b""
        if body is not None:
            payload = body
        elif data is not None:
            payload = urlencode(data).encode()
            headers["content-type"] = "application/x-www-form-urlencoded"
        status, headers_out, raw, set_cookies = request(method, url, body=payload, headers=headers, cookies=self.cookies)
        absorb_cookies(self.cookies, set_cookies)
        return status, headers_out, raw.decode(), set_cookies

    def post_webhook(self, event: dict, *, secret: str | None = None, timestamp: int | None = None):
        payload = json.dumps(event).encode("utf-8")
        header = sign(payload, secret or "whsec_test_secret", timestamp)
        return self.call(
            "POST",
            "/api/stripe/webhook",
            body=payload,
            headers={"stripe-signature": header, "content-type": "application/json"},
        )

    def license_key(self) -> str:
        saved = json.loads(self.store.read_text(encoding="utf-8"))
        return saved["members"][0]["license_key"]

    def test_rejects_bad_or_stale_signature(self) -> None:
        payload = b'{"id":"evt_x","type":"checkout.session.completed"}'
        self.assertFalse(verify_stripe_signature(payload, "t=1,v1=nope", "whsec_test_secret"))
        stale = sign(payload, "whsec_test_secret", int(time.time()) - 600)
        self.assertFalse(verify_stripe_signature(payload, stale, "whsec_test_secret"))
        status, _headers, body, _cookies = self.post_webhook(checkout_event(), secret="wrong-secret")
        self.assertEqual(status, 400)
        self.assertIn("署名を確認できません", body)
        self.assertNotIn("whsec", body)
        with mock.patch.dict(os.environ, {"STRIPE_WEBHOOK_SECRET": ""}):
            status, _headers, body, _cookies = self.post_webhook(checkout_event())
        self.assertEqual(status, 503)

    def test_checkout_enables_login_and_delete_revokes_it(self) -> None:
        status, _headers, body, _cookies = self.post_webhook(checkout_event())
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        status, _headers, body, _cookies = self.post_webhook(checkout_event())
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(self.store.read_text(encoding="utf-8"))["members"]), 1)

        key = self.license_key()
        status, headers, thanks, _cookies = self.call("GET", "/thanks?session_id=cs_test_abcdefgh")
        self.assertEqual(status, 200)
        self.assertIn(key, thanks)
        self.assertIn("member@example.com", thanks)
        self.assertNotIn("whsec_test_secret", thanks)
        self.assertNotIn("proxy-should-not-leak", thanks)
        self.assertNotIn("cus_123", thanks)

        status, headers, _body, _cookies = self.call(
            "POST",
            "/login",
            data={"password": key, "next": "/"},
            headers={"User-Agent": "DeviceOne"},
        )
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], "/")
        status, _headers, page, _cookies = self.call("GET", "/", headers={"User-Agent": "DeviceOne"})
        self.assertEqual(status, 200)
        self.assertIn("店舗別ダッシュボード", page)

        deleted = {
            "id": "evt_deleted",
            "type": "customer.subscription.deleted",
            "created": 1_700_000_100,
            "data": {"object": {"id": "sub_123", "customer": "cus_123", "status": "canceled"}},
        }
        status, _headers, _body, _cookies = self.post_webhook(deleted)
        self.assertEqual(status, 200)
        status, _headers, body, _cookies = self.call("GET", "/api/judgments", headers={"User-Agent": "DeviceOne"})
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["detail"], "利用権限がありません")
        self.cookies.clear()
        status, _headers, body, _cookies = self.call("POST", "/login", data={"password": key}, headers={"User-Agent": "DeviceOne"})
        self.assertEqual(status, 200)
        self.assertIn("利用権限がありません", body)

    def test_second_device_login_logs_out_the_first(self) -> None:
        self.post_webhook(checkout_event())
        key = self.license_key()
        self.call("POST", "/login", data={"password": key}, headers={"User-Agent": "DeviceOne"})
        first = dict(self.cookies)
        status, headers, _body, _cookies = self.call("POST", "/login", data={"password": key}, headers={"User-Agent": "DeviceTwo"})
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], "/")
        second = dict(self.cookies)
        self.cookies = first
        status, _headers, body, _cookies = self.call("GET", "/api/judgments", headers={"User-Agent": "DeviceOne"})
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["detail"], "ログインが必要です")
        self.cookies = second
        status, _headers, body, _cookies = self.call("GET", "/api/judgments", headers={"User-Agent": "DeviceTwo"})
        self.assertEqual(status, 200)
        status, _headers, body, _cookies = self.call("GET", "/api/judgments", headers={"User-Agent": "DeviceThree"})
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["detail"], "別の端末で利用中です")
        status, _headers, _body, _cookies = self.call("GET", "/api/judgments", headers={"User-Agent": "DeviceTwo"})
        self.assertEqual(status, 200)

    def test_login_from_another_address_replaces_the_session(self) -> None:
        self.post_webhook(checkout_event())
        key = self.license_key()
        self.call(
            "POST",
            "/login",
            data={"password": key},
            headers={"User-Agent": "DeviceOne", "X-Forwarded-For": "203.0.113.10"},
        )
        first = dict(self.cookies)
        status, headers, _body, _cookies = self.call(
            "POST",
            "/login",
            data={"password": key},
            headers={"User-Agent": "DeviceOne", "X-Forwarded-For": "203.0.113.20"},
        )
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], "/")
        second = dict(self.cookies)
        self.cookies = first
        status, _headers, body, _cookies = self.call(
            "GET",
            "/api/judgments",
            headers={"User-Agent": "DeviceOne", "X-Forwarded-For": "203.0.113.10"},
        )
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["detail"], "ログインが必要です")
        self.cookies = second
        status, _headers, _body, _cookies = self.call(
            "GET",
            "/api/judgments",
            headers={"User-Agent": "DeviceOne", "X-Forwarded-For": "203.0.113.20"},
        )
        self.assertEqual(status, 200)
        status, _headers, body, _cookies = self.call(
            "GET",
            "/api/judgments",
            headers={"User-Agent": "DeviceOne", "X-Forwarded-For": "203.0.113.30"},
        )
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["detail"], "別の端末で利用中です")
        status, _headers, _body, _cookies = self.call(
            "GET",
            "/api/judgments",
            headers={"User-Agent": "DeviceOne", "X-Forwarded-For": "203.0.113.20"},
        )
        self.assertEqual(status, 200)

    def test_trial_checkout_then_subscription_enables_and_cancel_stops(self) -> None:
        event = checkout_event(event_id="evt_trial_checkout")
        event["data"]["object"]["payment_status"] = "no_payment_required"
        status, _headers, body, _cookies = self.post_webhook(event)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        created = {
            "id": "evt_trial_created",
            "type": "customer.subscription.created",
            "created": 1_700_000_010,
            "data": {
                "object": {
                    "id": "sub_123",
                    "customer": "cus_123",
                    "status": "trialing",
                    "trial_end": int(time.time()) + 7 * 24 * 3600,
                }
            },
        }
        status, _headers, _body, _cookies = self.post_webhook(created)
        self.assertEqual(status, 200)
        saved = json.loads(self.store.read_text(encoding="utf-8"))
        self.assertEqual(saved["members"][0]["status"], "trialing")
        self.assertEqual(saved["members"][0]["email"], "member@example.com")
        status, headers, _body, _cookies = self.call(
            "POST",
            "/login",
            data={"password": self.license_key()},
            headers={"User-Agent": "DeviceOne"},
        )
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], "/")
        deleted = {
            "id": "evt_trial_deleted",
            "type": "customer.subscription.deleted",
            "created": 1_700_000_020,
            "data": {"object": {"id": "sub_123", "customer": "cus_123", "status": "canceled"}},
        }
        status, _headers, body, _cookies = self.post_webhook(deleted)
        self.assertEqual(status, 200)
        status, _headers, body, _cookies = self.call("GET", "/api/judgments", headers={"User-Agent": "DeviceOne"})
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["detail"], "利用権限がありません")

    def test_logout_lets_the_other_device_in(self) -> None:
        self.post_webhook(checkout_event())
        key = self.license_key()
        self.call("POST", "/login", data={"password": key}, headers={"User-Agent": "DeviceOne"})
        self.call("POST", "/logout", headers={"User-Agent": "DeviceOne"})
        status, headers, _body, _cookies = self.call("POST", "/login", data={"password": key}, headers={"User-Agent": "DeviceTwo"})
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], "/")

    def test_trial_end_blocks_without_waiting_for_another_webhook(self) -> None:
        grant_subscription(
            when=100,
            customer_id="cus_trial",
            subscription_id="sub_trial",
            status="trialing",
            trial_end=500,
            period_end=None,
        )
        key = self.license_key()
        _member_id, session_id, error = open_member_session(key, "127.0.0.1", "fp-a", now=400)
        self.assertEqual(error, "")
        self.assertTrue(session_id)
        _member_id, session_id, error = open_member_session(key, "127.0.0.1", "fp-a", now=500)
        self.assertEqual(session_id, "")
        self.assertEqual(error, "無料体験は終了しています")

    def test_late_checkout_does_not_restore_a_canceled_member(self) -> None:
        first = grant_checkout(
            when=100,
            session_id="cs_test_abcdefgh",
            customer_id="cus_123",
            subscription_id="sub_123",
            email="member@example.com",
        )
        grant_subscription(
            when=200,
            customer_id="cus_123",
            subscription_id="sub_123",
            status="canceled",
            trial_end=None,
            period_end=None,
            deleted=True,
        )
        grant_checkout(
            when=150,
            session_id="cs_test_abcdefgh",
            customer_id="cus_123",
            subscription_id="sub_123",
            email="member@example.com",
        )
        saved = json.loads(self.store.read_text(encoding="utf-8"))
        self.assertEqual(saved["members"][0]["status"], "canceled")
        self.assertEqual(saved["members"][0]["license_key"], first["license_key"])
        renewed = grant_checkout(
            when=300,
            session_id="cs_test_newcheck",
            customer_id="cus_123",
            subscription_id="sub_456",
            email="member@example.com",
        )
        self.assertEqual(renewed["status"], "active")
        self.assertEqual(renewed["license_key"], first["license_key"])
        self.assertEqual(len(json.loads(self.store.read_text(encoding="utf-8"))["members"]), 1)

    def test_unpaid_checkout_does_not_grant_access(self) -> None:
        event = checkout_event()
        event["data"]["object"]["payment_status"] = "unpaid"
        status, _headers, _body, _cookies = self.post_webhook(event)
        self.assertEqual(status, 200)
        self.assertFalse(self.store.is_file() and '"license_key"' in self.store.read_text(encoding="utf-8"))

    def test_past_due_update_removes_access(self) -> None:
        self.post_webhook(checkout_event())
        updated = {
            "id": "evt_past_due",
            "type": "customer.subscription.updated",
            "created": 1_700_000_050,
            "data": {
                "object": {
                    "id": "sub_123",
                    "customer": "cus_123",
                    "status": "past_due",
                    "current_period_end": 1_700_000_000,
                }
            },
        }
        self.post_webhook(updated)
        status, _headers, body, _cookies = self.call(
            "POST",
            "/login",
            data={"password": self.license_key()},
            headers={"User-Agent": "DeviceOne"},
        )
        self.assertEqual(status, 200)
        self.assertIn("利用権限がありません", body)

    def test_default_password_stops_working_after_stripe_is_configured(self) -> None:
        status, _headers, body, _cookies = self.call("POST", "/login", data={"password": "2026"})
        self.assertEqual(status, 200)
        self.assertIn("運営用のパスワードは、いまの設定では使えません。", body)
        with mock.patch.dict(os.environ, {"DASHBOARD_PASSWORD": "owner-only-secret"}):
            status, headers, _body, _cookies = self.call("POST", "/login", data={"password": "owner-only-secret"})
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], "/")

    def test_public_pages_do_not_receive_secrets(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "STRIPE_SECRET_KEY": "sk_test_should_not_leak_value",
                "STRIPE_WEBHOOK_SECRET": "whsec_should_not_leak_value",
            },
        ):
            for path in ("/lp", "/login", "/thanks", "/static/app.js", "/.env"):
                status, _headers, body, _cookies = self.call("GET", path)
                self.assertNotIn("sk_test_should_not_leak_value", body, path)
                self.assertNotIn("whsec_should_not_leak_value", body, path)
                self.assertNotIn("proxy-should-not-leak", body, path)
            status, _headers, body, _cookies = self.call("GET", "/.env")
            self.assertEqual(status, 404)
            status, _headers, body, _cookies = self.call("GET", "/login?notice=%3Cscript%3Ealert(1)")
            self.assertNotIn("<script>alert", body)
            status, _headers, body, _cookies = self.call("GET", "/login?notice=session")
            self.assertIn("別の端末で利用中です", body)
            status, _headers, body, _cookies = self.call("GET", "/static/app.js")
            self.assertNotIn("withinSetting6Band", body)
            self.assertNotIn("SETTING6_RELATIVE_TOLERANCE", body)
            status, _headers, body, _cookies = self.call("GET", "/src/analyzer/judgment.py")
            self.assertEqual(status, 404)
            self.assertNotIn("synthetic_probability_threshold", body)

    def test_checkout_session_points_back_to_thanks(self) -> None:
        class FakeResponse:
            status_code = 200

            def json(self):
                return {"id": "cs_test_created1", "url": "https://checkout.stripe.com/c/pay/cs_test_created1"}

        with mock.patch.dict(
            os.environ,
            {"STRIPE_SECRET_KEY": "sk_test_unit_secret", "STRIPE_PRICE_ID": "price_unit123", "STRIPE_TRIAL_DAYS": "3"},
        ):
            with mock.patch("src.dashboard.stripe_billing.httpx.post", return_value=FakeResponse()) as post:
                status, headers, body, _cookies = self.call("GET", "/api/lp-checkout")
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], "https://checkout.stripe.com/c/pay/cs_test_created1")
        self.assertNotIn("sk_test_unit_secret", body)
        from urllib.parse import unquote

        sent = unquote(post.call_args.kwargs["content"].decode())
        self.assertIn("price_unit123", sent)
        self.assertIn("/thanks?session_id={CHECKOUT_SESSION_ID}", sent)
        self.assertIn("trial_period_days", sent)
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer sk_test_unit_secret")

    def test_member_cannot_rejudge_or_edit_halls(self) -> None:
        self.post_webhook(checkout_event())
        status, _headers, _body, _cookies = self.call(
            "POST",
            "/login",
            data={"password": self.license_key(), "next": "/"},
            headers={"User-Agent": "DeviceOne"},
        )
        self.assertEqual(status, 303)
        device = {"User-Agent": "DeviceOne"}
        status, _headers, page, _cookies = self.call("GET", "/", headers=device)
        self.assertEqual(status, 200)
        self.assertIn("ログアウト", page)
        self.assertNotIn("再判定する", page)
        self.assertNotIn('href="/master-panel-x7k3q9/halls"', page)
        status, _headers, body, _cookies = self.call("POST", "/api/judge", headers=device)
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["detail"], "管理者権限が必要です")
        status, _headers, body, _cookies = self.call("GET", "/master-panel-x7k3q9/hall-rows", headers=device)
        self.assertEqual(status, 403)
        status, _headers, body, _cookies = self.call("POST", "/master-panel-x7k3q9/rejudge", headers=device)
        self.assertEqual(status, 403)
        self.assertNotIn("whsec", body)

    def test_account_subscription_sets_is_active_and_keeps_one_session(self) -> None:
        email = "player@example.com"
        password = "password123"
        status, headers, body, _cookies = self.call(
            "POST",
            "/register",
            data={"email": email, "password": password},
        )
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], "/billing")
        self.assertNotIn(password, body)
        status, _headers, page, _cookies = self.call("GET", "/billing")
        self.assertEqual(status, 200)
        self.assertIn(email, page)
        self.assertIn("まだ申し込み前です", page)
        self.assertIn("10日間無料で申し込む", page)
        self.assertNotIn(password, page)
        self.assertNotIn("pbkdf2$", page)
        status, headers, _body, _cookies = self.call("GET", "/")
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], "/billing")
        status, _headers, body, _cookies = self.call("GET", "/api/judgments")
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["detail"], "利用権限がありません")

        saved_text = self.store.read_text(encoding="utf-8")
        self.assertNotIn(password, saved_text)
        saved = json.loads(saved_text)
        self.assertEqual(len(saved["members"]), 1)
        member_id = saved["members"][0]["id"]
        self.assertFalse(saved["members"][0]["is_active"])
        self.assertTrue(str(saved["members"][0]["password_hash"]).startswith("pbkdf2$"))

        status, _headers, body, _cookies = self.call(
            "POST",
            "/register",
            data={"email": email, "password": "another-password"},
        )
        self.assertEqual(status, 200)
        self.assertIn("このメールアドレスは登録済みです", body)
        status, _headers, body, _cookies = self.call(
            "POST",
            "/register",
            data={"email": "other@example.com", "password": "short"},
        )
        self.assertEqual(status, 200)
        self.assertIn("8文字以上", body)

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"id": "cs_test_account1", "url": "https://checkout.stripe.com/c/pay/cs_test_account1"}

        with mock.patch.dict(
            os.environ,
            {"STRIPE_SECRET_KEY": "sk_test_unit_secret", "STRIPE_PRICE_ID": "price_unit123", "STRIPE_TRIAL_DAYS": ""},
        ):
            with mock.patch("src.dashboard.stripe_billing.httpx.post", return_value=FakeResponse()) as post:
                status, headers, body, _cookies = self.call("GET", "/checkout")
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], "https://checkout.stripe.com/c/pay/cs_test_account1")
        self.assertNotIn("sk_test_unit_secret", body)
        from urllib.parse import unquote

        sent = unquote(post.call_args.kwargs["content"].decode())
        self.assertIn(f"client_reference_id={member_id}", sent)
        self.assertIn(f"metadata[user_id]={member_id}", sent)
        self.assertIn(f"subscription_data[metadata][user_id]={member_id}", sent)
        self.assertIn("subscription_data[trial_period_days]=11", sent)
        self.assertIn("customer_email=player@example.com", sent)

        created = {
            "id": "evt_account_created",
            "type": "customer.subscription.created",
            "created": 1_700_000_010,
            "data": {
                "object": {
                    "id": "sub_account",
                    "customer": "cus_account",
                    "status": "trialing",
                    "trial_end": int(time.time()) + 7 * 24 * 3600,
                    "metadata": {"user_id": member_id},
                }
            },
        }
        status, _headers, _body, _cookies = self.post_webhook(created)
        self.assertEqual(status, 200)
        saved = json.loads(self.store.read_text(encoding="utf-8"))
        self.assertEqual(len(saved["members"]), 1)
        self.assertTrue(saved["members"][0]["is_active"])
        self.assertEqual(saved["members"][0]["status"], "trialing")
        self.assertEqual(saved["members"][0]["id"], member_id)
        status, _headers, page, _cookies = self.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("店舗別ダッシュボード", page)

        first = dict(self.cookies)
        self.cookies = {}
        status, headers, _body, _cookies = self.call(
            "POST",
            "/login",
            data={"email": email, "password": password},
            headers={"User-Agent": "DeviceTwo"},
        )
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], "/")
        second = dict(self.cookies)
        self.cookies = first
        status, _headers, body, _cookies = self.call("GET", "/api/judgments", headers={"User-Agent": "DeviceOne"})
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["detail"], "ログインが必要です")
        self.cookies = second
        status, _headers, _body, _cookies = self.call("GET", "/api/judgments", headers={"User-Agent": "DeviceTwo"})
        self.assertEqual(status, 200)

        updated = {
            "id": "evt_account_canceled",
            "type": "customer.subscription.updated",
            "created": 1_700_000_020,
            "data": {
                "object": {
                    "id": "sub_account",
                    "customer": "cus_account",
                    "status": "canceled",
                    "metadata": {"user_id": member_id},
                }
            },
        }
        self.post_webhook(updated)
        saved = json.loads(self.store.read_text(encoding="utf-8"))
        self.assertFalse(saved["members"][0]["is_active"])
        status, _headers, body, _cookies = self.call("GET", "/api/judgments", headers={"User-Agent": "DeviceTwo"})
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["detail"], "利用権限がありません")
        status, _headers, page, _cookies = self.call("GET", "/billing", headers={"User-Agent": "DeviceTwo"})
        self.assertEqual(status, 200)
        self.assertIn("10日間無料で申し込む", page)

        deleted = {
            "id": "evt_account_deleted",
            "type": "customer.subscription.deleted",
            "created": 1_700_000_030,
            "data": {
                "object": {
                    "id": "sub_account",
                    "customer": "cus_account",
                    "status": "canceled",
                    "metadata": {"user_id": member_id},
                }
            },
        }
        status, _headers, _body, _cookies = self.post_webhook(deleted)
        self.assertEqual(status, 200)
        saved = json.loads(self.store.read_text(encoding="utf-8"))
        self.assertFalse(saved["members"][0]["is_active"])
        self.assertEqual(saved["members"][0]["status"], "canceled")

        with mock.patch.dict(
            os.environ,
            {
                "STRIPE_SECRET_KEY": "",
                "STRIPE_PRICE_ID": "",
                "STRIPE_CHECKOUT_URL": "https://buy.stripe.com/test_link_abcdefgh",
            },
        ):
            status, headers, _body, _cookies = self.call("GET", "/checkout", headers={"User-Agent": "DeviceTwo"})
        self.assertEqual(status, 303)
        location = headers["location"]
        self.assertIn("buy.stripe.com/test_link_abcdefgh", location)
        self.assertIn(f"client_reference_id={member_id}", location)
        self.assertIn("prefilled_email=player%40example.com", location)


if __name__ == "__main__":
    unittest.main()
