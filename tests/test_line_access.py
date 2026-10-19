from __future__ import annotations

import asyncio
import base64
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
from urllib.parse import urlsplit

os.environ.setdefault("DASHBOARD_PASSWORD", "owner-secret")
os.environ.setdefault("DASHBOARD_SESSION_DAYS", "7")

from src.dashboard.app import app
from src.dashboard.line_access import issue_line_token
from src.dashboard.line_access import verify_line_id_token
from src.dashboard.line_access import line_guard_enabled
from src.dashboard.line_access import line_liff_id
from src.dashboard.square_members import SquareMemberError
from src.dashboard.square_members import apply_square_event
from src.dashboard.square_members import handle_square_webhook
from src.dashboard.square_members import record_line_signup
from src.dashboard.square_members import tool_access


LINE_USER = "U" + ("ab" * 16)
OTHER_USER = "U" + ("cd" * 16)
NOTIFY_URL = "https://billing.example.com/square-webhook"
SIGNING_KEY = "square-sign-key"
CHANNEL_ID = "1234567890"
DENIED = "ご契約中のメールアドレスを入力するか、有料プランへご登録ください"
MEMBER_EMAIL = "member@example.com"


def request(method: str, url: str, *, body: bytes = b"", headers: dict | None = None, cookies: dict | None = None):
    parts = urlsplit(url)
    header_list = []
    for key, value in (headers or {}).items():
        header_list.append((key.lower().encode(), value.encode()))
    if cookies:
        header_list.append((b"cookie", "; ".join(f"{key}={value}" for key, value in cookies.items()).encode()))
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


def _sign(body: bytes) -> str:
    digest = hmac.new(SIGNING_KEY.encode("utf-8"), NOTIFY_URL.encode("utf-8") + body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def _subscription(
    event_id: str,
    status: str = "ACTIVE",
    charged_through: str = "2099-01-01",
    created_at: str = "2099-01-01T00:00:00Z",
) -> dict:
    return {
        "event_id": event_id,
        "type": "subscription.updated" if status != "ACTIVE" else "subscription.created",
        "created_at": created_at,
        "data": {
            "type": "subscription",
            "id": event_id,
            "object": {
                "subscription": {
                    "id": "SUB1",
                    "customer_id": "CUST1",
                    "status": status,
                    "start_date": "2099-01-01",
                    "charged_through_date": charged_through,
                }
            },
        },
    }


def _invoice(event_id: str, amount: int, event_type: str = "invoice.payment_made") -> dict:
    return {
        "event_id": event_id,
        "type": event_type,
        "created_at": "2099-01-02T00:00:00Z",
        "data": {
            "type": "invoice",
            "id": event_id,
            "object": {
                "invoice": {
                    "id": event_id,
                    "subscription_id": "SUB1",
                    "status": "PAID" if "failed" not in event_type else "UNPAID",
                    "primary_recipient": {"customer_id": "CUST1", "email_address": "member@example.com"},
                    "payment_requests": [{"computed_amount_money": {"amount": amount, "currency": "JPY"}}],
                }
            },
        },
    }


class LineAccessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(
            os.environ,
            {
                "SQUARE_MEMBER_DB": str(Path(self.tmp.name) / "square.sqlite"),
                "DATABASE_URL": "",
                "SQUARE_WEBHOOK_SIGNATURE_KEY": SIGNING_KEY,
                "SQUARE_WEBHOOK_NOTIFICATION_URL": NOTIFY_URL,
                "DASHBOARD_PASSWORD": "owner-secret",
                "DASHBOARD_SESSION_SECRET": "line-test-secret",
                "MEMBER_DISABLE_SHARED_PASSWORD": "",
                "STRIPE_WEBHOOK_SECRET": "",
                "DASHBOARD_OPEN": "",
                "SURO_PUBLIC_DEMO": "",
                "LINE_ACCESS_GUARD": "1",
                "LINE_LIFF_ID": "",
                "LINE_LOGIN_CHANNEL_ID": CHANNEL_ID,
                "LINE_LOGIN_CHANNEL_SECRET": "",
                "LINE_LOGIN_REDIRECT_URL": "",
                "LINE_OFFICIAL_URL": "",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.cookies: dict[str, str] = {}

    def call(self, method: str, url: str, *, body: bytes = b"", headers: dict | None = None):
        status, headers_out, raw, set_cookies = request(method, url, body=body, headers=headers, cookies=self.cookies)
        for raw_cookie in set_cookies:
            pair, _, rest = raw_cookie.partition(";")
            name, _, value = pair.partition("=")
            if "max-age=0" in rest.lower():
                self.cookies.pop(name, None)
            else:
                self.cookies[name] = value
        return status, headers_out, raw

    def line_cookie(self, user_id: str = LINE_USER) -> None:
        self.cookies["line_member"] = issue_line_token(user_id)

    def login_email(self, email: str = MEMBER_EMAIL, password: str = ""):
        body = urlencode({"email": email, "password": password, "next": "/"}).encode()
        return self.call(
            "POST",
            "/login",
            body=body,
            headers={"content-type": "application/x-www-form-urlencoded"},
        )

    def test_anonymous_tool_is_hidden(self) -> None:
        status, headers, body = self.call("GET", "/")
        self.assertEqual(status, 303)
        self.assertIn("/login", headers["location"])
        self.assertNotIn("店舗別ダッシュボード", body.decode())
        status, headers, body = self.call("GET", "/?line_user_id=" + LINE_USER)
        self.assertEqual(status, 303)
        self.assertIn("/login", headers["location"])
        status, _headers, page = self.call("GET", "/login")
        text = page.decode()
        self.assertIn('autocomplete="username"', text)
        self.assertNotIn("パスワード", text)
        self.assertNotIn('type="password"', text)
        self.assertIn("メールアドレス", text)
        status, _headers, body = self.call("GET", "/api/judgments")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["detail"], "ログインが必要です")
        self.assertNotIn(b"machines", body)

    def test_operator_can_still_open_the_tool(self) -> None:
        status, _headers, _body = self.call(
            "POST",
            "/login",
            body=b"password=owner-secret",
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(status, 303)
        status, _headers, body = self.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("店舗別ダッシュボード", body.decode())

    def test_liff_page_does_not_render_the_tool(self) -> None:
        with mock.patch.dict(os.environ, {"LINE_LIFF_ID": "1234567890-Abcd1234"}):
            status, _headers, body = self.call("GET", "/line")
        text = body.decode()
        self.assertEqual(status, 200)
        self.assertIn("liff.init", text)
        self.assertIn("1234567890-Abcd1234", text)
        self.assertIn("/api/line/session", text)
        self.assertNotIn("店舗別ダッシュボード", text)

    def test_missing_line_login_does_not_open_the_tool(self) -> None:
        with mock.patch.dict(os.environ, {"LINE_LOGIN_CHANNEL_ID": "", "LINE_LIFF_ID": ""}):
            status, _headers, body = self.call("GET", "/line")
        text = body.decode()
        self.assertEqual(status, 200)
        self.assertIn("設定が未完了", text)
        self.assertNotIn("店舗別ダッシュボード", text)

    def test_oauth_start_and_bad_state(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "LINE_LOGIN_CHANNEL_SECRET": "login-secret",
                "LINE_LOGIN_REDIRECT_URL": "https://tool.example.com/line/callback",
            },
        ):
            status, headers, _body = self.call("GET", "/line")
            self.assertEqual(status, 303)
            self.assertTrue(headers["location"].startswith("https://access.line.me/oauth2/v2.1/authorize?"))
            self.assertIn(f"client_id={CHANNEL_ID}", headers["location"])
            with mock.patch("src.dashboard.line_access.exchange_line_code") as exchange:
                status, headers, _body = self.call("GET", "/line/callback?code=abc&state=nope")
            exchange.assert_not_called()
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], "/line")

    def test_raw_line_id_is_rejected(self) -> None:
        status, _headers, body = self.call(
            "POST",
            "/api/line/session",
            body=json.dumps({"line_user_id": LINE_USER}).encode(),
            headers={"content-type": "application/json"},
        )
        self.assertEqual(status, 401)
        self.assertNotIn("line_member", self.cookies)
        self.assertEqual(json.loads(body)["detail"], "LINEの確認ができません")

    def test_trial_and_active_can_open_until_webhook_stops_them(self) -> None:
        record_line_signup(line_user_id=LINE_USER, email=MEMBER_EMAIL, square_customer_id="CUST1", line_name="極子")
        status, _headers, body = self.login_email()
        self.assertEqual(status, 200)
        self.assertIn(DENIED, body.decode())
        self.assertNotIn("dashboard_session", self.cookies)
        self.assertNotIn("店舗別ダッシュボード", body.decode())

        apply_square_event(_subscription("evt-sub"))
        status, headers, _body = self.login_email(password=MEMBER_EMAIL)
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], "/")
        self.assertIn("dashboard_session", self.cookies)
        status, _headers, body = self.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("店舗別ダッシュボード", body.decode())
        status, _headers, body = self.call("GET", "/api/judgments")
        self.assertEqual(status, 200)
        self.assertIn("machines", json.loads(body))

        failed = json.dumps(_invoice("evt-fail", 2980, "invoice.payment_failed")).encode()
        self.assertEqual(handle_square_webhook(failed, _sign(failed))["result"], "updated")
        status, _headers, body = self.call("GET", "/")
        self.assertEqual(status, 403)
        self.assertIn(DENIED, body.decode())
        self.assertNotIn("店舗別ダッシュボード", body.decode())
        self.assertNotIn("dashboard_session", self.cookies)
        status, _headers, denied = self.login_email()
        self.assertEqual(status, 200)
        self.assertIn(DENIED, denied.decode())

        paid = json.dumps(_invoice("evt-paid", 2980)).encode()
        self.assertEqual(handle_square_webhook(paid, _sign(paid))["result"], "updated")
        status, headers, _body = self.login_email()
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], "/")
        status, _headers, body = self.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("店舗別ダッシュボード", body.decode())

        canceled = json.dumps(_subscription("evt-cancel", "CANCELED", created_at="2099-01-03T00:00:00Z")).encode()
        self.assertEqual(handle_square_webhook(canceled, _sign(canceled))["result"], "updated")
        status, _headers, body = self.call("GET", "/api/hall-catalog")
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["detail"], DENIED)
        self.assertNotIn(b"halls", body)
        self.assertNotIn(b"machines", body)

    def test_billing_webhook_stops_the_tool_on_the_shared_ledger(self) -> None:
        import main
        from fastapi.testclient import TestClient

        record_line_signup(line_user_id=LINE_USER, email=MEMBER_EMAIL, square_customer_id="CUST1")
        apply_square_event(_subscription("evt-sub-main"))
        status, headers, _body = self.login_email()
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], "/")
        status, _headers, body = self.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("店舗別ダッシュボード", body.decode())

        client = TestClient(main.app)
        body = json.dumps(_subscription("evt-cancel-main", "CANCELED")).encode()
        response = client.post(
            "/square-webhook",
            content=body,
            headers={"x-square-hmacsha256-signature": _sign(body)},
        )
        self.assertEqual(response.status_code, 200)
        status, _headers, page = self.call("GET", "/")
        self.assertEqual(status, 403)
        self.assertIn(DENIED, page.decode())
        self.assertNotIn("店舗別ダッシュボード", page.decode())

    def test_pending_suspended_unknown_and_expired_trial_are_blocked(self) -> None:
        record_line_signup(line_user_id=LINE_USER, email=MEMBER_EMAIL, square_customer_id="CUST1")
        status, _headers, body = self.login_email()
        self.assertEqual(status, 200)
        self.assertIn(DENIED, body.decode())
        self.assertNotIn("店舗別ダッシュボード", body.decode())

        apply_square_event(_subscription("evt-old", charged_through="2020-01-01"))
        self.assertEqual(tool_access(LINE_USER)["reason"], "trial_ended")
        status, _headers, body = self.login_email()
        self.assertEqual(status, 200)
        self.assertIn(DENIED, body.decode())

        apply_square_event(_invoice("evt-paid-hold", 2980))
        from src.dashboard.square_members import apply_member_action
        from src.dashboard.square_members import list_members

        member_id = list_members()["members"][0]["id"]
        apply_member_action(member_id, "suspend")
        status, _headers, body = self.login_email()
        self.assertEqual(status, 200)
        self.assertIn(DENIED, body.decode())
        self.assertNotIn("dashboard_session", self.cookies)

        status, _headers, body = self.login_email("missing@example.com")
        self.assertEqual(status, 200)
        self.assertIn(DENIED, body.decode())
        self.assertNotIn("店舗別ダッシュボード", body.decode())

    def test_quoted_line_env_enables_the_guard(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "LINE_ACCESS_GUARD": "",
                "LINE_LOGIN_CHANNEL_ID": "",
                "LINE_LIFF_ID": '"1234567890-Abcd1234"',
            },
        ):
            self.assertEqual(line_liff_id(), "1234567890-Abcd1234")
            self.assertTrue(line_guard_enabled())

    def test_trailing_slash_uses_the_same_guard(self) -> None:
        with mock.patch.dict(os.environ, {"LINE_LIFF_ID": "1234567890-Abcd1234"}):
            status, headers, body = self.call("GET", "/line/")
        self.assertEqual(status, 307)
        self.assertTrue(headers["location"].endswith("/line"))
        self.assertNotIn("店舗別ダッシュボード", body.decode())
        status, headers, body = self.call("GET", "/line/callback/")
        self.assertNotIn("店舗別ダッシュボード", body.decode())
        self.assertNotEqual(status, 500)
        status, _headers, body = self.call("GET", "/api/judgments/")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["detail"], "ログインが必要です")
        self.assertNotIn(b"machines", body)

    def test_unreadable_ledger_keeps_an_existing_session(self) -> None:
        record_line_signup(line_user_id=LINE_USER, email=MEMBER_EMAIL, square_customer_id="CUST1")
        apply_square_event(_subscription("evt-sub-read"))
        self.login_email()
        with mock.patch("src.dashboard.auth.email_access", side_effect=SquareMemberError("会員台帳を読み取れません")):
            status, _headers, body = self.call("GET", "/")
            self.assertEqual(status, 200)
            self.assertIn("店舗別ダッシュボード", body.decode())
        self.login_email()
        with mock.patch("src.dashboard.auth.email_access", side_effect=SquareMemberError("会員台帳を読み取れません")):
            status, _headers, body = self.call("GET", "/api/judgments")
        self.assertEqual(status, 200)
        self.assertIn("machines", json.loads(body))

    def test_forged_cookie_is_anonymous(self) -> None:
        self.cookies["dashboard_session"] = "e1.bm9wZQ.1.deadbeef"
        self.cookies["line_member"] = f"l1.{LINE_USER}.{int(time.time())}.deadbeef"
        status, headers, body = self.call("GET", "/")
        self.assertEqual(status, 303)
        self.assertIn("/login", headers["location"])
        self.assertNotIn("店舗別ダッシュボード", body.decode())

    def test_verify_line_id_token_checks_audience_and_subject(self) -> None:
        payload = {"sub": LINE_USER, "aud": CHANNEL_ID, "exp": int(time.time()) + 60}
        response = mock.Mock(status_code=200)
        response.json.return_value = payload
        with mock.patch("httpx.post", return_value=response) as post:
            self.assertEqual(verify_line_id_token("header.payload.sig"), LINE_USER)
        post.assert_called_once()
        sent = post.call_args.kwargs["data"]
        self.assertEqual(sent["client_id"], CHANNEL_ID)
        self.assertNotIn("line_user_id", sent)

        mismatch = mock.Mock(status_code=200)
        mismatch.json.return_value = {"sub": LINE_USER, "aud": "999", "exp": int(time.time()) + 60}
        with mock.patch("httpx.post", return_value=mismatch):
            with self.assertRaises(Exception):
                verify_line_id_token("header.payload.sig")
        failed = mock.Mock(status_code=400)
        with mock.patch("httpx.post", return_value=failed):
            with self.assertRaises(Exception):
                verify_line_id_token("header.payload.sig")


if __name__ == "__main__":
    unittest.main()
