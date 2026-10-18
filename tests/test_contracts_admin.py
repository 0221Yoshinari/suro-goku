from __future__ import annotations

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
from urllib.parse import urlencode

os.environ.setdefault("DASHBOARD_PASSWORD", "owner-secret")
os.environ.setdefault("DASHBOARD_SESSION_DAYS", "7")

from src.dashboard.app import app
from src.dashboard.contracts import list_contracts
from src.dashboard.members import grant_subscription


JST = timezone(timedelta(hours=9))
PANEL = "/master-panel-x7k3q9"


def request(method: str, url: str, *, body: bytes = b"", headers: dict | None = None, cookies: dict | None = None):
    import asyncio
    from urllib.parse import urlsplit

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


def absorb_cookies(jar: dict, set_cookies: list[str]) -> None:
    for raw in set_cookies:
        pair, _, rest = raw.partition(";")
        name, _, value = pair.partition("=")
        if "max-age=0" in rest.lower():
            jar.pop(name, None)
        else:
            jar[name] = value


def sign(payload: bytes, secret: str, timestamp: int | None = None) -> str:
    import hashlib
    import hmac

    stamp = int(time.time() if timestamp is None else timestamp)
    signed = f"{stamp}.".encode("ascii") + payload
    signature = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"t={stamp},v1={signature}"


def stamp(*parts: int) -> int:
    year, month, day, hour, minute = parts
    return int(datetime(year, month, day, hour, minute, tzinfo=JST).timestamp())


class ContractAdminTest(unittest.TestCase):
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
                "MASTER_PANEL_TOKEN": "panel-token-value",
                "DASHBOARD_PASSWORD": "owner-secret",
                "DASHBOARD_SESSION_DAYS": "7",
                "MEMBER_DISABLE_SHARED_PASSWORD": "",
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

    def post_webhook(self, event: dict):
        payload = json.dumps(event).encode("utf-8")
        return self.call(
            "POST",
            "/api/stripe/webhook",
            body=payload,
            headers={"stripe-signature": sign(payload, "whsec_test_secret"), "content-type": "application/json"},
        )

    def unlock(self) -> None:
        self.call("POST", "/login", data={"password": "owner-secret"})
        status, headers, _body, _cookies = self.call(
            "POST",
            f"{PANEL}/unlock",
            data={"master_token": "panel-token-value", "next": f"{PANEL}/contracts"},
        )
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], f"{PANEL}/contracts")

    def test_subscription_events_sync_trial_window_in_jst(self) -> None:
        trial_start = stamp(2026, 10, 17, 9, 0)
        trial_end = stamp(2026, 10, 20, 9, 0)
        started = stamp(2026, 10, 17, 9, 5)
        created = {
            "id": "evt_contract_created",
            "type": "customer.subscription.created",
            "created": started + 10,
            "data": {
                "object": {
                    "id": "sub_contract",
                    "customer": {"id": "cus_contract", "email": "Kei@Example.com", "name": "慶也"},
                    "status": "trialing",
                    "created": started,
                    "start_date": started,
                    "trial_start": trial_start,
                    "trial_end": trial_end,
                    "current_period_end": trial_end,
                    "items": {"data": [{"price": {"nickname": "月額プラン"}}]},
                }
            },
        }
        status, _headers, _body, _cookies = self.post_webhook(created)
        self.assertEqual(status, 200)
        saved = json.loads(self.store.read_text(encoding="utf-8"))["members"][0]
        self.assertEqual(saved["email"], "kei@example.com")
        self.assertEqual(saved["name"], "慶也")
        self.assertEqual(saved["status"], "trialing")
        self.assertEqual(saved["trial_start"], trial_start)
        self.assertEqual(saved["trial_end"], trial_end)
        self.assertEqual(saved["plan_name"], "月額プラン")
        self.assertEqual(saved["subscription_started_at"], started)
        self.assertNotIn("license_key", json.dumps(list_contracts(now=stamp(2026, 10, 17, 21, 0))))
        with mock.patch.dict(os.environ, {"STRIPE_SECRET_KEY": "sk_test_contract_dashboard"}):
            listed = list_contracts(now=stamp(2026, 10, 17, 21, 0))
        row = listed["members"][0]
        self.assertEqual(row["created_at_jst"], "2026-10-17 09:05")
        self.assertEqual(row["trial_start_jst"], "2026-10-17 09:00")
        self.assertEqual(row["trial_end_jst"], "2026-10-20 09:00")
        self.assertEqual(row["trial_remaining_label"], "あと3日")
        self.assertEqual(row["phase"], "無料")
        self.assertEqual(row["stripe_url"], "https://dashboard.stripe.com/test/customers/cus_contract")
        self.assertFalse(row["ending_this_week"])
        self.assertEqual(listed["summary"]["trialing"], 1)
        self.assertEqual(listed["summary"]["ending_this_week"], 0)

        updated = {
            "id": "evt_contract_updated",
            "type": "customer.subscription.updated",
            "created": started + 30,
            "data": {
                "object": {
                    "id": "sub_contract",
                    "customer": {"id": "cus_contract", "email": "kei@example.com", "name": "慶也"},
                    "status": "active",
                    "created": started,
                    "trial_start": trial_start,
                    "trial_end": trial_end,
                    "current_period_end": stamp(2026, 11, 20, 9, 0),
                    "items": {"data": [{"price": {"nickname": "月額プラン"}}]},
                }
            },
        }
        with mock.patch("src.dashboard.stripe_billing.httpx.get") as fetch:
            self.post_webhook(updated)
            fetch.assert_not_called()
        active = list_contracts(now=stamp(2026, 10, 21, 9, 0))["members"][0]
        self.assertEqual(active["status"], "active")
        self.assertEqual(active["phase"], "有料")
        self.assertEqual(active["next_billing_jst"], "2026-11-20 09:00")
        self.assertEqual(active["trial_remaining_label"], "終了")

        deleted = {
            "id": "evt_contract_deleted",
            "type": "customer.subscription.deleted",
            "created": started + 40,
            "data": {"object": {"id": "sub_contract", "customer": "cus_contract", "status": "canceled"}},
        }
        self.post_webhook(deleted)
        canceled = list_contracts()["members"][0]
        self.assertEqual(canceled["status"], "canceled")
        self.assertEqual(canceled["status_label"], "解約済")
        self.assertEqual(canceled["badge"], "canceled")
        self.assertEqual(canceled["trial_end_jst"], "2026-10-20 09:00")
        self.assertEqual(canceled["next_billing_jst"], "")

    def test_missing_customer_profile_is_loaded_from_stripe(self) -> None:
        event = {
            "id": "evt_fetch_customer",
            "type": "customer.subscription.created",
            "created": 1_700_000_100,
            "data": {
                "object": {
                    "id": "sub_fetch",
                    "customer": "cus_fetch123",
                    "status": "trialing",
                    "trial_start": 1_700_000_000,
                    "trial_end": 1_700_000_000 + 7 * 24 * 3600,
                }
            },
        }

        class Response:
            status_code = 200

            def json(self):
                return {"id": "cus_fetch123", "email": "fetch@example.com", "name": "取得 太郎"}

        with mock.patch.dict(os.environ, {"STRIPE_SECRET_KEY": "sk_test_contract_dashboard"}):
            with mock.patch("src.dashboard.stripe_billing.httpx.get", return_value=Response()) as fetch:
                status, _headers, _body, _cookies = self.post_webhook(event)
        self.assertEqual(status, 200)
        fetch.assert_called_once()
        self.assertIn("/v1/customers/cus_fetch123", fetch.call_args.args[0])
        saved = json.loads(self.store.read_text(encoding="utf-8"))["members"][0]
        self.assertEqual(saved["email"], "fetch@example.com")
        self.assertEqual(saved["name"], "取得 太郎")

    def test_contract_api_requires_the_master_token(self) -> None:
        grant_subscription(
            when=100,
            customer_id="cus_hidden",
            subscription_id="sub_hidden",
            status="past_due",
            trial_end=None,
            period_end=stamp(2026, 11, 1, 0, 0),
            email="hidden@example.com",
            name="非公開",
        )
        status, headers, _body, _cookies = self.call("GET", f"{PANEL}/contracts/rows", headers={"accept": "application/json"})
        self.assertEqual(status, 401)
        self.assertNotIn("hidden@example.com", _body)
        status, _headers, body, _cookies = self.call("GET", "/static/contracts.js")
        self.assertEqual(status, 404)
        self.assertNotIn("contract-rows", body)
        self.call("POST", "/login", data={"password": "owner-secret"})
        status, _headers, body, _cookies = self.call("GET", f"{PANEL}/contracts")
        self.assertEqual(status, 200)
        self.assertIn("管理者トークン", body)
        self.assertIn(f'value="{PANEL}/contracts"', body)
        self.assertNotIn("ユーザー契約", body)
        status, _headers, body, _cookies = self.call("GET", f"{PANEL}/contracts/rows", headers={"accept": "application/json"})
        self.assertEqual(status, 401)
        self.assertNotIn("hidden@example.com", body)
        self.unlock()
        status, _headers, body, _cookies = self.call("GET", f"{PANEL}/contracts")
        self.assertEqual(status, 200)
        self.assertIn("ユーザー契約", body)
        self.assertIn("今週体験終了", body)
        status, _headers, body, _cookies = self.call("GET", f"{PANEL}/contracts/rows?status=past_due")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["members"][0]["email"], "hidden@example.com")
        self.assertEqual(payload["members"][0]["status_label"], "決済失敗")
        self.assertNotIn("license_key", body)
        self.assertNotIn("password_hash", body)
        status, _headers, body, _cookies = self.call("GET", f"{PANEL}/contracts/rows?q=missing-person")
        self.assertEqual(json.loads(body)["members"], [])
        self.assertEqual(json.loads(body)["summary"]["total"], 1)
        status, _headers, body, _cookies = self.call("GET", f"{PANEL}/contracts/rows?status=nope")
        self.assertEqual(status, 400)

    def test_member_session_cannot_open_contracts(self) -> None:
        grant_subscription(
            when=100,
            customer_id="cus_member",
            subscription_id="sub_member",
            status="active",
            trial_end=None,
            period_end=stamp(2026, 12, 1, 0, 0),
            email="member@example.com",
        )
        key = json.loads(self.store.read_text(encoding="utf-8"))["members"][0]["license_key"]
        status, headers, _body, _cookies = self.call(
            "POST",
            "/login",
            data={"password": key},
            headers={"User-Agent": "MemberPhone"},
        )
        self.assertEqual(status, 303)
        status, _headers, body, _cookies = self.call("GET", f"{PANEL}/contracts", headers={"User-Agent": "MemberPhone"})
        self.assertEqual(status, 404)
        self.assertNotIn("ユーザー契約", body)
        status, _headers, body, _cookies = self.call(
            "GET",
            f"{PANEL}/contracts/rows",
            headers={"User-Agent": "MemberPhone", "accept": "application/json"},
        )
        self.assertEqual(status, 404)
        self.assertNotIn("member@example.com", body)

    def test_search_matches_customer_id(self) -> None:
        grant_subscription(
            when=stamp(2026, 10, 1, 10, 0),
            customer_id="cus_searchme",
            subscription_id="sub_search",
            status="trialing",
            trial_start=stamp(2026, 10, 1, 10, 0),
            trial_end=stamp(2026, 10, 18, 18, 0),
            period_end=stamp(2026, 10, 18, 18, 0),
            email="search@example.com",
            name="検索花子",
            plan_name="月額",
            started_at=stamp(2026, 10, 1, 10, 0),
        )
        found = list_contracts(query="CUS_search", status="trialing", now=stamp(2026, 10, 15, 18, 0))
        self.assertEqual(found["members"][0]["name"], "検索花子")
        self.assertEqual(found["members"][0]["trial_remaining_label"], "あと3日")
        self.assertEqual(found["summary"]["ending_this_week"], 1)
        outside = list_contracts(status="ending_this_week", now=stamp(2026, 10, 20, 12, 0))
        self.assertEqual(outside["members"], [])


if __name__ == "__main__":
    unittest.main()
