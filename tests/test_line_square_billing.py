from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import unittest
from unittest import mock

os.environ["LINE_CHANNEL_SECRET"] = "test-secret"
os.environ["LINE_CHANNEL_ACCESS_TOKEN"] = "test-token"
os.environ["SQUARE_ACCESS_TOKEN"] = "test-square-token"
os.environ["SQUARE_APPLICATION_ID"] = "sandbox-sq0idb-test"
os.environ["SQUARE_ENVIRONMENT"] = "sandbox"

import main
from fastapi.testclient import TestClient


def _sign(body: bytes) -> str:
    digest = hmac.new(b"test-secret", body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


class BillingRulesTest(unittest.TestCase):
    def test_email_pattern(self) -> None:
        self.assertTrue(main.is_email("user@example.com"))
        self.assertFalse(main.is_email("こんにちは"))
        self.assertFalse(main.is_email("user@example"))

    def test_checkout_token_round_trip(self) -> None:
        token = main.issue_checkout_token("CUST1", "member@example.com", "U1", now=1_700_000_000)
        claims = main.read_checkout_token(token, now=1_700_000_000)
        self.assertEqual(claims["customer_id"], "CUST1")
        self.assertEqual(claims["email"], "member@example.com")
        url = main.checkout_page_url("https://suro-goku-billing.onrender.com", token)
        self.assertTrue(url.startswith("https://suro-goku-billing.onrender.com/checkout?t="))
        self.assertNotIn("square.link", url)
        with self.assertRaises(main.SquareAPIError):
            main.normalize_public_base("http://example.com")
        with self.assertRaises(main.SquareAPIError):
            main.read_checkout_token(token, now=1_700_000_000 + main._CHECKOUT_TTL + 1)
        page = TestClient(main.app).get("/checkout")
        self.assertEqual(page.status_code, 200)
        self.assertIn('lang="ja"', page.text)
        self.assertIn(main._CHECKOUT_ITEM_NAME, page.text)
        self.assertIn("今日の支払い", page.text)
        self.assertIn("最初の10日間は0円", page.text)
        self.assertIn("11日目から", page.text)

    def test_production_uses_live_square_endpoints(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "SQUARE_ENVIRONMENT": "production",
                "SQUARE_APPLICATION_ID": "sq0idp-live",
                "SQUARE_ACCESS_TOKEN": "live-token",
            },
        ):
            self.assertEqual(main.square_environment(), "production")
            self.assertEqual(main.square_web_sdk_url(), "https://web.squarecdn.com/v1/square.js")
            self.assertEqual(main.square_base_url(), "https://connect.squareup.com")
            self.assertEqual(main.ensure_square_credentials(), "production")

    def test_sandbox_application_keeps_sandbox_when_env_says_production(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "SQUARE_ENVIRONMENT": "production",
                "SQUARE_APPLICATION_ID": "sandbox-sq0idb-test",
                "SQUARE_ACCESS_TOKEN": "sandbox-token",
            },
        ):
            self.assertEqual(main.square_environment(), "sandbox")
            self.assertEqual(main.square_web_sdk_url(), "https://sandbox.web.squarecdn.com/v1/square.js")
            self.assertEqual(main.square_base_url(), "https://connect.squareupsandbox.com")
            self.assertEqual(main.ensure_square_credentials(), "sandbox")

    def test_phases_are_free_then_monthly(self) -> None:
        phases = main.subscription_phases()
        self.assertEqual(phases[0]["cadence"], "DAILY")
        self.assertEqual(phases[0]["periods"], 11)
        self.assertEqual(phases[0]["pricing"]["price"]["amount"], 0)
        self.assertEqual(phases[1]["cadence"], "MONTHLY")
        self.assertEqual(phases[1]["pricing"]["price"]["amount"], 2980)
        self.assertEqual(phases[1]["pricing"]["price"]["currency"], "JPY")

    def test_trial_button_fits_line_quick_reply(self) -> None:
        self.assertLessEqual(len(main._TRIAL_BUTTON), 20)
        message = main.line_text_message(main._WELCOME, main._TRIAL_BUTTON)
        action = message["quickReply"]["items"][0]["action"]
        self.assertEqual(action["type"], "message")
        self.assertEqual(action["label"], main._TRIAL_BUTTON)
        self.assertEqual(action["text"], main._TRIAL_BUTTON)
        self.assertTrue(main.is_trial_intent(main._TRIAL_BUTTON))
        self.assertTrue(main.is_trial_intent("10日間無料でスロ極を試す"))
        self.assertTrue(main.is_trial_intent("7日間無料でスロ極を試す"))
        self.assertIn("10日間、全機能を無料で試せます", main._WELCOME)
        self.assertIn("【普段お使いのメールアドレス】", main._GUIDE)
        self.assertIn("料金は一切かかりません", main._GUIDE)
        self.assertNotIn("**", main._GUIDE)

    def test_docs_are_disabled(self) -> None:
        self.assertIsNone(main.app.docs_url)
        self.assertIsNone(main.app.redoc_url)
        self.assertIsNone(main.app.openapi_url)
        client = TestClient(main.app)
        self.assertEqual(client.get("/docs").status_code, 404)
        health = client.get("/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["ok"], True)
        self.assertEqual(health.json()["squareEnvironment"], "sandbox")
        self.assertEqual(health.json()["squareApplication"], "sandbox")


class BillingMemberAdminTest(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(
            os.environ,
            {
                "MASTER_PANEL_TOKEN": "panel-token-value",
                "MEMBER_ADMIN_OPEN": "0",
                "SQUARE_MEMBER_DB": str(Path(self.tmp.name) / "square.sqlite"),
                "DATABASE_URL": "",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client = TestClient(main.app)

    def test_members_page_is_on_the_billing_app(self) -> None:
        missing = self.client.get("/master-panel-x7k3q9/members")
        self.assertEqual(missing.status_code, 200)
        self.assertIn("管理者トークン", missing.text)
        self.assertNotIn("無料から有料への転換率", missing.text)
        rows = self.client.get("/master-panel-x7k3q9/members/rows")
        self.assertEqual(rows.status_code, 401)
        hidden = self.client.get("/static/members.js")
        self.assertEqual(hidden.status_code, 404)
        styles = self.client.get("/static/styles.css")
        self.assertEqual(styles.status_code, 200)

        headers = {"x-master-token": "panel-token-value"}
        page = self.client.get("/master-panel-x7k3q9/members", headers=headers)
        self.assertEqual(page.status_code, 200)
        self.assertIn("会員管理", page.text)
        self.assertIn("無料から有料への転換率", page.text)
        script = self.client.get("/static/members.js", headers=headers)
        self.assertEqual(script.status_code, 200)
        self.assertIn("member-rows", script.text)
        listed = self.client.get("/master-panel-x7k3q9/members/rows", headers=headers)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["summary"]["total"], 0)

    def test_open_mode_skips_the_token_screen_and_prints_the_url(self) -> None:
        from src.dashboard.member_admin import members_dashboard_url

        with mock.patch.dict(
            os.environ,
            {
                "MEMBER_ADMIN_OPEN": "1",
                "RENDER_EXTERNAL_URL": "https://suro-goku-billing.onrender.com",
            },
        ):
            self.assertEqual(
                members_dashboard_url(),
                "https://suro-goku-billing.onrender.com/master-panel-x7k3q9/members",
            )
            page = self.client.get("/master-panel-x7k3q9/members")
            self.assertEqual(page.status_code, 200)
            self.assertIn("無料から有料への転換率", page.text)
            self.assertNotIn("管理者トークンを入力してください", page.text)
            rows = self.client.get("/master-panel-x7k3q9/members/rows")
            self.assertEqual(rows.status_code, 200)
            script = self.client.get("/static/members.js")
            self.assertEqual(script.status_code, 200)


class WebhookTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(main.app)

    def test_rejects_bad_signature(self) -> None:
        response = self.client.post("/webhook", content=b"{}", headers={"x-line-signature": "nope"})
        self.assertEqual(response.status_code, 401)

    def test_non_email_asks_for_address(self) -> None:
        body = json.dumps(
            {
                "events": [
                    {
                        "type": "message",
                        "replyToken": "reply-1",
                        "source": {"userId": "U123", "type": "user"},
                        "message": {"type": "text", "text": "始めたい"},
                    }
                ]
            }
        ).encode()
        with mock.patch.object(main, "line_reply", new=mock.AsyncMock()) as reply:
            response = self.client.post("/webhook", content=body, headers={"x-line-signature": _sign(body)})
        self.assertEqual(response.status_code, 200)
        reply.assert_awaited_once_with("reply-1", main._WELCOME, quick_reply=main._TRIAL_BUTTON)

    def test_follow_shows_trial_button(self) -> None:
        body = json.dumps(
            {
                "events": [
                    {
                        "type": "follow",
                        "replyToken": "reply-follow",
                        "source": {"userId": "U123", "type": "user"},
                    }
                ]
            }
        ).encode()
        with mock.patch.object(main, "line_reply", new=mock.AsyncMock()) as reply:
            response = self.client.post("/webhook", content=body, headers={"x-line-signature": _sign(body)})
        self.assertEqual(response.status_code, 200)
        reply.assert_awaited_once_with("reply-follow", main._WELCOME, quick_reply=main._TRIAL_BUTTON)

    def test_trial_button_asks_for_email(self) -> None:
        body = json.dumps(
            {
                "events": [
                    {
                        "type": "message",
                        "replyToken": "reply-cta",
                        "source": {"userId": "U123", "type": "user"},
                        "message": {"type": "text", "text": main._TRIAL_BUTTON},
                    }
                ]
            }
        ).encode()
        with mock.patch.object(main, "line_reply", new=mock.AsyncMock()) as reply, mock.patch.object(
            main, "register_trial", new=mock.AsyncMock()
        ) as register:
            response = self.client.post("/webhook", content=body, headers={"x-line-signature": _sign(body)})
        self.assertEqual(response.status_code, 200)
        register.assert_not_awaited()
        reply.assert_awaited_once_with("reply-cta", main._GUIDE)

    def test_email_registers_trial(self) -> None:
        body = json.dumps(
            {
                "events": [
                    {
                        "type": "message",
                        "replyToken": "reply-2",
                        "source": {"userId": "U999", "type": "user"},
                        "message": {"type": "text", "text": "member@example.com"},
                    }
                ]
            }
        ).encode()

        checkout = main.checkout_reply("https://sandbox.square.link/u/member")

        async def fake_register(user_id: str, email: str, base_url: str) -> str:
            self.assertEqual(user_id, "U999")
            self.assertEqual(email, "member@example.com")
            self.assertEqual(base_url, "http://testserver")
            return checkout

        with mock.patch.object(main, "register_trial", side_effect=fake_register), mock.patch.object(
            main, "line_reply", new=mock.AsyncMock()
        ) as reply:
            response = self.client.post("/webhook", content=body, headers={"x-line-signature": _sign(body)})
        self.assertEqual(response.status_code, 200)
        reply.assert_awaited_once_with("reply-2", checkout)


class SquareFlowTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        main._resolved_ids.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(
            os.environ,
            {"SQUARE_MEMBER_DB": str(Path(self.tmp.name) / "square.sqlite"), "DATABASE_URL": ""},
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    async def test_creates_customer_and_replies_with_card_link(self) -> None:
        calls: list[tuple[str, str, dict | None]] = []

        async def fake_square(method: str, path: str, *, params=None, payload=None):
            calls.append((method, path, payload))
            if path == "/v2/locations" and method == "GET":
                return {"locations": [{"id": "LOC1", "status": "ACTIVE", "language_code": "en"}]}
            if method == "PUT" and path == "/v2/locations/LOC1":
                self.assertEqual(payload["location"]["language_code"], "ja-JP")
                return {"location": {"id": "LOC1", "language_code": "ja-JP"}}
            if path == "/v2/catalog/list":
                return {"objects": []}
            if path == "/v2/catalog/object":
                kind = payload["object"]["type"]
                if kind == "SUBSCRIPTION_PLAN":
                    return {"catalog_object": {"id": "PLAN1"}}
                phases = payload["object"]["subscription_plan_variation_data"]["phases"]
                self.assertEqual(phases[0]["cadence"], "DAILY")
                self.assertEqual(phases[0]["periods"], 11)
                self.assertEqual(phases[0]["pricing"]["price"]["amount"], 0)
                self.assertEqual(phases[1]["pricing"]["price"]["amount"], 2980)
                return {"catalog_object": {"id": "VAR1"}}
            if path == "/v2/customers/search":
                return {"customers": []}
            if path == "/v2/customers":
                return {"customer": {"id": "CUST1"}}
            if path == "/v2/subscriptions/search":
                return {"subscriptions": []}
            raise AssertionError(path)

        with mock.patch.object(main, "square_json", side_effect=fake_square), mock.patch.object(
            main, "line_display_name", new=mock.AsyncMock(return_value="極子")
        ):
            message = await main.register_trial(
                "Uline123",
                "member@example.com",
                "https://suro-goku-billing.onrender.com",
            )

        self.assertIn("最初の10日間は0円", message)
        self.assertIn("11日目から", message)
        self.assertIn("自動で引き落とされます", message)
        self.assertNotIn("square.link", message)
        self.assertIn(
            "https://suro-goku-billing.onrender.com/register?email=member%40example.com",
            message,
        )
        client = TestClient(main.app)
        opened = client.get("/register?email=member@example.com", follow_redirects=False)
        self.assertEqual(opened.status_code, 302)
        token = opened.headers["location"].rsplit("/checkout?t=", 1)[1]
        claims = main.read_checkout_token(token)
        self.assertEqual(claims["customer_id"], "CUST1")
        self.assertEqual(claims["user_id"], "Uline123")
        from src.dashboard.square_members import list_members

        listed = list_members()
        self.assertEqual(listed["summary"]["total"], 1)
        self.assertEqual(listed["members"][0]["status"], "pending")
        self.assertEqual(listed["members"][0]["line_name"], "極子")
        self.assertEqual(listed["members"][0]["square_customer_id"], "CUST1")
        self.assertFalse(any(method == "POST" and path == "/v2/subscriptions" for method, path, _payload in calls))
        customer = next(payload for method, path, payload in calls if path == "/v2/customers" and method == "POST")
        self.assertEqual(customer["reference_id"], "Uline123")
        self.assertEqual(customer["email_address"], "member@example.com")

    def test_complete_stores_card_and_starts_subscription(self) -> None:
        calls: list[tuple[str, str, dict | None]] = []

        async def fake_square(method: str, path: str, *, params=None, payload=None):
            calls.append((method, path, payload))
            if path == "/v2/locations" and method == "GET":
                return {"locations": [{"id": "LOC1", "status": "ACTIVE", "language_code": "ja"}]}
            if path == "/v2/catalog/list":
                return {
                    "objects": [
                        {
                            "type": "SUBSCRIPTION_PLAN_VARIATION",
                            "id": "VAR1",
                            "subscription_plan_variation_data": {"name": main._VARIATION_NAME},
                        }
                    ]
                }
            if path == "/v2/subscriptions/search":
                return {"subscriptions": []}
            if path == "/v2/cards":
                self.assertEqual(payload["source_id"], "cnon:card-nonce-ok")
                self.assertEqual(payload["card"]["customer_id"], "CUST1")
                self.assertNotIn("start_date", payload)
                return {"card": {"id": "CARD1", "customer_id": "CUST1"}}
            if path == "/v2/subscriptions" and method == "POST":
                self.assertEqual(payload["card_id"], "CARD1")
                self.assertEqual(payload["plan_variation_id"], "VAR1")
                self.assertEqual(payload["customer_id"], "CUST1")
                self.assertEqual(payload["location_id"], "LOC1")
                self.assertEqual(payload["start_date"], main.subscription_start_date())
                return {"subscription": {"id": "SUB1", "card_id": "CARD1", "status": "ACTIVE"}}
            raise AssertionError(path)

        token = main.issue_checkout_token("CUST1", "member@example.com", "Uline123")
        from src.dashboard.square_members import record_line_signup

        record_line_signup(
            line_user_id="Uline123",
            email="member@example.com",
            square_customer_id="CUST1",
        )
        with mock.patch.object(main, "square_json", side_effect=fake_square):
            client = TestClient(main.app)
            response = client.post(
                "/checkout/complete",
                json={"token": token, "sourceId": "cnon:card-nonce-ok"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("今日の支払いは0円", response.json()["message"])
        self.assertIn("11日目から", response.json()["message"])
        self.assertIn("2,980円", response.json()["message"])
        self.assertFalse(any(path == "/v2/online-checkout/payment-links" for _method, path, _payload in calls))
        from src.dashboard.square_members import list_members

        listed = list_members()
        self.assertEqual(listed["members"][0]["status"], "trialing")
        self.assertEqual(listed["members"][0]["square_subscription_id"], "SUB1")

    async def test_cancel_waits_for_confirmation(self) -> None:
        from src.dashboard.square_members import list_members
        from src.dashboard.square_members import record_card_trial
        from src.dashboard.square_members import record_line_signup

        main._cancel_pending.clear()
        record_line_signup(line_user_id="Uline123", email="member@example.com", square_customer_id="CUST1")
        record_card_trial(
            line_user_id="Uline123",
            email="member@example.com",
            square_customer_id="CUST1",
            subscription_id="SUB1",
        )
        calls: list[str] = []

        async def fake_square(method: str, path: str, *, params=None, payload=None):
            calls.append(f"{method} {path}")
            if method == "POST" and path == "/v2/subscriptions/SUB1/cancel":
                return {"subscription": {"id": "SUB1", "status": "ACTIVE", "canceled_date": "2026-11-19"}}
            raise AssertionError(path)

        replies: list[str] = []

        async def fake_reply(token: str, text: str, quick_reply=None) -> None:
            replies.append(text)
            if quick_reply is not None:
                message = main.line_text_message(text, quick_reply)
                labels = [item["action"]["label"] for item in message["quickReply"]["items"]]
                self.assertEqual(labels, ["解約を確定する", "キャンセル（やめる）"])

        with mock.patch.object(main, "square_json", side_effect=fake_square), mock.patch.object(
            main, "line_reply", side_effect=fake_reply
        ):
            await main.handle_text("Uline123", "reply-cancel", "解約", "https://example.test")
            self.assertEqual(calls, [])
            self.assertIn("本当に解約しますか？", replies[-1])
            await main.handle_text("Uline123", "reply-keep", "キャンセル（やめる）", "https://example.test")
            self.assertEqual(calls, [])
            self.assertIn("解約を中止しました", replies[-1])
            await main.handle_text("Uline123", "reply-again", "キャンセル", "https://example.test")
            await main.handle_text("Uline123", "reply-done", "解約を確定する", "https://example.test")

        self.assertEqual(calls, ["POST /v2/subscriptions/SUB1/cancel"])
        self.assertIn("解約手続きが完了しました", replies[-1])
        saved = list_members()["members"][0]
        self.assertEqual(saved["status"], "canceled")
        from src.dashboard.square_members import tool_access

        self.assertTrue(tool_access("Uline123")["allowed"])


if __name__ == "__main__":
    unittest.main()
