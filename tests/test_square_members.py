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
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

os.environ.setdefault("DASHBOARD_PASSWORD", "owner-secret")
os.environ.setdefault("MASTER_PANEL_TOKEN", "panel-token-value")

from src.dashboard.app import app
from src.dashboard.square_members import SquareMemberError
from src.dashboard.square_members import apply_member_action
from src.dashboard.square_members import apply_square_event
from src.dashboard.square_members import describe_backend
from src.dashboard.square_members import email_access
from src.dashboard.square_members import list_members
from src.dashboard.square_members import record_line_signup
from src.dashboard.square_members import restore_members_from_square
from src.dashboard.square_sync import ensure_square_members
from src.dashboard.square_sync import pull_square_members

import main


JST = timezone(timedelta(hours=9))
PANEL = "/master-panel-x7k3q9"
NOTIFY_URL = "https://billing.example.com/square-webhook"
SIGNING_KEY = "square-sign-key"


def _sign(body: bytes) -> str:
    digest = hmac.new(SIGNING_KEY.encode("utf-8"), NOTIFY_URL.encode("utf-8") + body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def _iso(when: int) -> str:
    return datetime.fromtimestamp(when, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _invoice(event_id: str, when: int, amount: int, event_type: str = "invoice.payment_made") -> dict:
    return {
        "event_id": event_id,
        "type": event_type,
        "created_at": _iso(when),
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


def _subscription(event_id: str, when: int, status: str = "ACTIVE") -> dict:
    return {
        "event_id": event_id,
        "type": "subscription.created" if status == "ACTIVE" else "subscription.updated",
        "created_at": _iso(when),
        "data": {
            "type": "subscription",
            "id": "SUB1",
            "object": {
                "subscription": {
                    "id": "SUB1",
                    "customer_id": "CUST1",
                    "status": status,
                    "start_date": "2026-10-19",
                    "charged_through_date": "2026-10-25",
                }
            },
        },
    }


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
    response_headers = {key.decode().lower(): value.decode() for key, value in start["headers"] if key.decode().lower() != "set-cookie"}
    set_cookies = [value.decode() for key, value in start["headers"] if key.decode().lower() == "set-cookie"]
    return start["status"], response_headers, payload, set_cookies


class SquareLedgerTest(unittest.TestCase):
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
                "MASTER_PANEL_TOKEN": "panel-token-value",
                "DASHBOARD_PASSWORD": "owner-secret",
                "MEMBER_DISABLE_SHARED_PASSWORD": "",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.cookies: dict[str, str] = {}
        self.base = int(time.time())

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

    def test_postgres_url_is_shared_ahead_of_a_sqlite_path(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "DATABASE_URL": "postgresql://user:pass@db.internal:5432/suro",
                "SQUARE_MEMBER_DB": str(Path(self.tmp.name) / "ignored.sqlite"),
            },
        ):
            self.assertEqual(describe_backend(), "postgres")
        with mock.patch.dict(
            os.environ,
            {
                "DATABASE_URL": '"postgres://user:pass@db.internal:5432/suro"',
                "SQUARE_MEMBER_DB": str(Path(self.tmp.name) / "ignored.sqlite"),
            },
        ):
            self.assertEqual(describe_backend(), "postgres")
        self.assertEqual(describe_backend(), "sqlite")

    def test_paid_invoice_converts_trial_and_zero_yen_does_not(self) -> None:
        record_line_signup(
            line_user_id="Uline123",
            email="member@example.com",
            square_customer_id="CUST1",
            line_name="極子",
            when=self.base,
        )
        self.assertEqual(apply_square_event(_subscription("evt-sub", self.base + 10)), "updated")
        trial = list_members(now=self.base + 10)
        self.assertEqual(trial["members"][0]["status"], "trialing")
        self.assertEqual(trial["members"][0]["status_label"], "無料体験中")
        self.assertEqual(trial["members"][0]["square_subscription_id"], "SUB1")
        self.assertEqual(trial["members"][0]["trial_end_jst"], "2026-10-26 00:00")
        self.assertEqual(trial["summary"]["cvr"], 0.0)
        apply_square_event(_invoice("evt-free", self.base + 20, 0))
        self.assertEqual(list_members()["members"][0]["status"], "trialing")
        self.assertIsNone(list_members()["members"][0]["paid_at"])
        self.assertEqual(apply_square_event(_invoice("evt-paid", self.base + 30, 2980)), "updated")
        paid = list_members(now=self.base + 30)
        self.assertEqual(paid["members"][0]["status_label"], "有料会員")
        self.assertEqual(paid["summary"]["active"], 1)
        self.assertEqual(paid["summary"]["trialing"], 0)
        self.assertEqual(paid["summary"]["converted"], 1)
        self.assertEqual(paid["summary"]["cvr"], 100.0)
        self.assertEqual(apply_square_event(_invoice("evt-paid", self.base + 30, 2980)), "duplicate")
        self.assertEqual(paid["summary"]["total"], 1)

    def test_failure_cancel_and_manual_actions(self) -> None:
        record_line_signup(line_user_id="U2", email="other@example.com", square_customer_id="CUST1", when=self.base)
        apply_square_event(_subscription("evt-sub", self.base + 5))
        apply_square_event(_invoice("evt-paid", self.base + 10, 2980))
        member_id = list_members()["members"][0]["id"]
        suspended = apply_member_action(member_id, "suspend", now=self.base + 40)
        self.assertEqual(suspended["member"]["status_label"], "アカウント停止")
        apply_square_event(_invoice("evt-late-pay", self.base + 50, 2980))
        self.assertEqual(list_members()["members"][0]["status"], "suspended")
        extended = apply_member_action(member_id, "extend_trial", days=14, now=self.base + 60)
        self.assertEqual(extended["member"]["status"], "trialing")
        self.assertGreater(extended["member"]["trial_end"], self.base + 60)
        apply_square_event(_subscription("evt-echo", self.base + 70, "ACTIVE"))
        self.assertEqual(list_members()["members"][0]["status"], "trialing")
        apply_square_event(_invoice("evt-fail", self.base + 80, 2980, "invoice.payment_failed"))
        self.assertEqual(list_members()["members"][0]["status_label"], "決済エラー/一時停止")
        apply_square_event(_subscription("evt-cancel", self.base + 90, "CANCELED"))
        self.assertEqual(list_members()["members"][0]["status_label"], "解約済み")
        forced = apply_member_action(member_id, "force_active", now=self.base + 100)
        self.assertEqual(forced["member"]["status"], "active")
        held = list_members(query="other@example.com", status="hold")
        self.assertEqual(held["members"], [])
        visible = list_members(query="U2", status="active")
        self.assertEqual(len(visible["members"]), 1)

    def test_search_sort_and_payment_note(self) -> None:
        record_line_signup(line_user_id="Ua", email="a@example.com", line_name="エー", when=self.base)
        record_line_signup(line_user_id="Ub", email="b@example.com", line_name="ビー", when=self.base + 5)
        ordered = list_members(sort="email", order="asc")
        self.assertEqual([item["email"] for item in ordered["members"]], ["a@example.com", "b@example.com"])
        found = list_members(query="ビー")
        self.assertEqual(len(found["members"]), 1)
        payment = {
            "event_id": "evt-note",
            "type": "payment.updated",
            "created_at": _iso(self.base + 10),
            "data": {
                "object": {
                    "payment": {
                        "id": "PAY1",
                        "status": "COMPLETED",
                        "customer_id": "CUST9",
                        "note": "LINE:Unew",
                        "amount_money": {"amount": 2980, "currency": "JPY"},
                    }
                }
            },
        }
        self.assertEqual(apply_square_event(payment), "updated")
        created = list_members(query="Unew")
        self.assertEqual(created["members"][0]["status"], "active")
        self.assertEqual(created["members"][0]["square_customer_id"], "CUST9")

    def test_webhook_and_admin_page_are_protected(self) -> None:
        body = json.dumps(_invoice("evt-http", self.base + 10, 2980)).encode("utf-8")
        status, _headers, raw = self.call(
            "POST",
            "/square-webhook",
            body=body,
            headers={"x-square-hmacsha256-signature": "nope", "content-type": "application/json"},
        )
        self.assertEqual(status, 401)
        status, _headers, raw = self.call(
            "POST",
            "/api/square/webhook",
            body=body,
            headers={"x-square-hmacsha256-signature": _sign(body), "content-type": "application/json"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["result"], "updated")
        self.assertEqual(list_members()["summary"]["active"], 1)

        status, _headers, raw = self.call("GET", "/static/members.js")
        self.assertEqual(status, 404)
        status, _headers, raw = self.call("GET", f"{PANEL}/members/rows", headers={"accept": "application/json"})
        self.assertEqual(status, 401)
        status, headers, page = self.call("GET", f"{PANEL}/members")
        self.assertEqual(status, 303)
        self.assertIn("/login", headers["location"])

        self.call("POST", "/login", body=b"password=owner-secret", headers={"content-type": "application/x-www-form-urlencoded"})
        status, _headers, page = self.call("GET", f"{PANEL}/members")
        self.assertEqual(status, 200)
        self.assertIn("管理者トークン", page.decode())
        self.assertNotIn("無料から有料への転換率", page.decode())
        status, headers, _page = self.call(
            "POST",
            f"{PANEL}/unlock",
            body=f"master_token=panel-token-value&next={PANEL}/members".encode(),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], f"{PANEL}/members")
        status, _headers, page = self.call("GET", f"{PANEL}/members")
        self.assertEqual(status, 200)
        self.assertIn("無料から有料への転換率", page.decode())
        self.assertIn("無料期間終了日", page.decode())
        status, _headers, raw = self.call("GET", f"{PANEL}/members/rows?q=member@example.com")
        payload = json.loads(raw)
        self.assertEqual(payload["summary"]["active"], 1)
        self.assertEqual(payload["members"][0]["email"], "member@example.com")
        member_id = payload["members"][0]["id"]
        status, _headers, raw = self.call(
            "POST",
            f"{PANEL}/members/{member_id}/action",
            body=json.dumps({"action": "cancel", "days": 7}).encode(),
            headers={"content-type": "application/json", "accept": "application/json"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["member"]["status"], "canceled")
        status, _headers, raw = self.call("GET", f"{PANEL}/members/rows?status=nope")
        self.assertEqual(status, 400)

        client_body = json.dumps(_invoice("evt-main", self.base + 15, 0)).encode("utf-8")
        from fastapi.testclient import TestClient

        client = TestClient(main.app)
        denied = client.post("/square-webhook", content=client_body, headers={"x-square-hmacsha256-signature": "nope"})
        self.assertEqual(denied.status_code, 401)
        accepted = client.post(
            "/square-webhook",
            content=client_body,
            headers={"x-square-hmacsha256-signature": _sign(client_body)},
        )
        self.assertEqual(accepted.status_code, 200)

    def test_square_subscription_restores_trial_login(self) -> None:
        restored = restore_members_from_square(
            [
                {
                    "email": "paid@example.com",
                    "line_user_id": "U1",
                    "customer_id": "CUST9",
                    "subscription_id": "SUB9",
                    "status": "ACTIVE",
                    "start_date": "2026-10-19",
                }
            ],
            now=self.base,
        )
        self.assertEqual(restored, 1)
        access = email_access("paid@example.com", now=self.base)
        self.assertTrue(access["allowed"])
        self.assertEqual(access["status"], "trialing")
        self.assertEqual(list_members()["summary"]["trialing"], 1)

    def test_billing_service_does_not_ask_itself(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "RENDER": "true",
                "RENDER_EXTERNAL_URL": "https://suro-goku-billing.onrender.com",
                "SQUARE_ACCESS_TOKEN": "",
            },
        ):
            with mock.patch("urllib.request.urlopen") as opened:
                access = email_access("missing@example.com", now=self.base)
        opened.assert_not_called()
        self.assertFalse(access["allowed"])

    def test_tool_uses_billing_ledger_when_local_is_empty(self) -> None:
        payload = json.dumps(
            {
                "allowed": True,
                "reason": "ok",
                "status": "trialing",
                "email": "remote@example.com",
                "line_user_id": "U9",
            }
        ).encode()

        class _Body:
            def __enter__(self):
                return self

            def __exit__(self, *_exc: object) -> bool:
                return False

            def read(self) -> bytes:
                return payload

        with mock.patch.dict(
            os.environ,
            {
                "RENDER": "true",
                "RENDER_EXTERNAL_URL": "https://suro-goku.onrender.com",
                "MEMBER_LEDGER_SOURCE": "",
            },
        ):
            with mock.patch("urllib.request.urlopen", return_value=_Body()):
                access = email_access("remote@example.com", now=self.base)
        self.assertTrue(access["allowed"])
        self.assertEqual(access["status"], "trialing")

    def test_operator_status_survives_a_restart(self) -> None:
        note = "SURO1:" + json.dumps(
            {
                "status": "suspended",
                "status_source": "operator",
                "trial_end": self.base + 20 * 86400,
                "email": "paid@example.com",
                "line_user_id": "U1",
            }
        )
        restore_members_from_square(
            [
                {
                    "email": "paid@example.com",
                    "line_user_id": "U1",
                    "customer_id": "CUST9",
                    "subscription_id": "SUB9",
                    "status": "ACTIVE",
                    "start_date": "2026-10-19",
                    "note": note,
                }
            ],
            now=self.base,
        )
        access = email_access("paid@example.com", now=self.base)
        self.assertFalse(access["allowed"])
        self.assertEqual(access["status"], "suspended")

    def test_unpaid_cancel_keeps_the_free_period(self) -> None:
        from src.dashboard.square_members import email_access

        record_line_signup(
            line_user_id="Uline123",
            email="member@example.com",
            square_customer_id="CUST1",
            when=self.base,
        )
        apply_square_event(_subscription("evt-sub", self.base + 10))
        trial_end = list_members(now=self.base + 10)["members"][0]["trial_end"]
        early = dict(_subscription("evt-cancel-early", self.base + 20, "CANCELED"))
        early["data"]["object"]["subscription"]["charged_through_date"] = "2026-10-19"
        self.assertEqual(apply_square_event(early), "updated")
        kept = list_members(now=self.base + 20)["members"][0]
        self.assertEqual(kept["status"], "canceled")
        self.assertEqual(kept["trial_end"], trial_end)
        self.assertTrue(email_access("member@example.com", now=self.base + 20)["allowed"])
        self.assertFalse(email_access("member@example.com", now=int(trial_end))["allowed"])

    def test_square_cancel_overrides_a_saved_trial(self) -> None:
        note = "SURO1:" + json.dumps({"status": "trialing", "status_source": "square", "email": "paid@example.com"})
        restore_members_from_square(
            [
                {
                    "email": "paid@example.com",
                    "line_user_id": "U1",
                    "customer_id": "CUST9",
                    "subscription_id": "SUB9",
                    "status": "CANCELED",
                    "note": note,
                }
            ],
            now=self.base,
        )
        access = email_access("paid@example.com", now=self.base)
        self.assertFalse(access["allowed"])
        self.assertEqual(access["status"], "canceled")

    def test_subscription_restore_continues_when_customer_search_fails(self) -> None:
        pages = {
            "/v2/subscriptions/search": {
                "subscriptions": [
                    {
                        "id": "SUB9",
                        "customer_id": "CUST9",
                        "status": "ACTIVE",
                        "start_date": "2026-10-19",
                    }
                ]
            },
            "/v2/customers/bulk-retrieve": {
                "responses": {
                    "CUST9": {
                        "customer": {
                            "id": "CUST9",
                            "email_address": "paid@example.com",
                            "reference_id": "U1",
                        }
                    }
                }
            },
        }

        def fake(_token: str, _method: str, path: str, _payload: dict | None = None) -> dict:
            if path == "/v2/customers/search":
                raise RuntimeError("bad filter")
            if path not in pages:
                raise AssertionError(path)
            return pages[path]

        with mock.patch.dict(
            os.environ,
            {
                "SQUARE_ACCESS_TOKEN": "token",
                "SQUARE_APPLICATION_ID": "sq0idp-test",
                "SQUARE_ENVIRONMENT": "production",
            },
        ):
            with mock.patch("src.dashboard.square_sync._square_json", side_effect=fake):
                restored = pull_square_members()
        self.assertEqual(restored, 1)
        self.assertTrue(email_access("paid@example.com", now=self.base)["allowed"])

    def test_local_ledger_does_not_call_square(self) -> None:
        with mock.patch("src.dashboard.square_sync.pull_square_members") as pulled:
            self.assertTrue(ensure_square_members())
        pulled.assert_not_called()

    def test_tool_does_not_treat_a_billing_outage_as_non_member(self) -> None:
        from urllib.error import URLError

        with mock.patch.dict(
            os.environ,
            {
                "RENDER": "true",
                "RENDER_EXTERNAL_URL": "https://suro-goku.onrender.com",
                "MEMBER_LEDGER_SOURCE": "",
            },
        ):
            with mock.patch("urllib.request.urlopen", side_effect=URLError("down")):
                with mock.patch("src.dashboard.square_members.time.sleep"):
                    with self.assertRaises(SquareMemberError):
                        email_access("offline@example.com", now=self.base)

    def test_member_list_is_not_shown_as_empty_when_square_cannot_be_read(self) -> None:
        from fastapi.testclient import TestClient

        with mock.patch.dict(os.environ, {"RENDER": "true", "SQUARE_ACCESS_TOKEN": "token", "MEMBER_ADMIN_OPEN": "1"}):
            with mock.patch("src.dashboard.square_sync.ensure_square_members", return_value=False):
                client = TestClient(main.app)
                response = client.get("/master-panel-x7k3q9/members/rows")
        self.assertEqual(response.status_code, 503)
        self.assertIn("読み込めませんでした", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
