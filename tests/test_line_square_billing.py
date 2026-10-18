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

    def test_checkout_url_accepts_square_link_only(self) -> None:
        url = "https://sandbox.square.link/u/abc123"
        self.assertEqual(main.checkout_url({"payment_link": {"url": url}}), url)
        with self.assertRaises(main.SquareAPIError):
            main.checkout_url({"payment_link": {"url": "https://example.com/pay"}})
        with self.assertRaises(main.SquareAPIError):
            main.checkout_url({"payment_link": {"url": "http://square.link/u/abc"}})

    def test_phases_are_free_then_monthly(self) -> None:
        phases = main.subscription_phases()
        self.assertEqual(phases[0]["pricing"]["price"]["amount"], 0)
        self.assertEqual(phases[1]["pricing"]["price"]["amount"], 2980)
        self.assertEqual(phases[1]["pricing"]["price"]["currency"], "JPY")

    def test_docs_are_disabled(self) -> None:
        self.assertIsNone(main.app.docs_url)
        self.assertIsNone(main.app.redoc_url)
        self.assertIsNone(main.app.openapi_url)
        client = TestClient(main.app)
        self.assertEqual(client.get("/docs").status_code, 404)
        health = client.get("/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json(), {"ok": True})


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
        reply.assert_awaited_once_with("reply-1", main._ASK_EMAIL)

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

        async def fake_register(user_id: str, email: str) -> str:
            self.assertEqual(user_id, "U999")
            self.assertEqual(email, "member@example.com")
            return checkout

        with mock.patch.object(main, "register_trial", side_effect=fake_register), mock.patch.object(
            main, "line_reply", new=mock.AsyncMock()
        ) as reply:
            response = self.client.post("/webhook", content=body, headers={"x-line-signature": _sign(body)})
        self.assertEqual(response.status_code, 200)
        reply.assert_awaited_once_with("reply-2", checkout)


class SquareFlowTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        main._resolved_ids.clear()

    async def test_creates_customer_and_replies_with_payment_link(self) -> None:
        calls: list[tuple[str, str, dict | None]] = []

        async def fake_square(method: str, path: str, *, params=None, payload=None):
            calls.append((method, path, payload))
            if path == "/v2/locations":
                return {"locations": [{"id": "LOC1", "status": "ACTIVE"}]}
            if path == "/v2/catalog/list":
                return {"objects": []}
            if path == "/v2/catalog/object":
                kind = payload["object"]["type"]
                if kind == "SUBSCRIPTION_PLAN":
                    return {"catalog_object": {"id": "PLAN1"}}
                phases = payload["object"]["subscription_plan_variation_data"]["phases"]
                self.assertEqual(phases[0]["pricing"]["price"]["amount"], 0)
                self.assertEqual(phases[1]["pricing"]["price"]["amount"], 2980)
                return {"catalog_object": {"id": "VAR1"}}
            if path == "/v2/customers/search":
                return {"customers": []}
            if path == "/v2/customers":
                return {"customer": {"id": "CUST1"}}
            if path == "/v2/subscriptions/search":
                return {"subscriptions": []}
            if path == "/v2/online-checkout/payment-links":
                self.assertEqual(payload["quick_pay"]["location_id"], "LOC1")
                self.assertEqual(payload["quick_pay"]["price_money"]["amount"], 0)
                self.assertEqual(payload["quick_pay"]["price_money"]["currency"], "JPY")
                self.assertEqual(payload["checkout_options"]["subscription_plan_id"], "VAR1")
                self.assertEqual(payload["pre_populated_data"]["buyer_email"], "member@example.com")
                self.assertEqual(payload["payment_note"], "LINE:Uline123")
                return {"payment_link": {"id": "LINK1", "url": "https://sandbox.square.link/u/member"}}
            raise AssertionError(path)

        with mock.patch.object(main, "square_json", side_effect=fake_square):
            message = await main.register_trial("Uline123", "member@example.com")

        self.assertEqual(message, main.checkout_reply("https://sandbox.square.link/u/member"))
        self.assertFalse(any(method == "POST" and path == "/v2/subscriptions" for method, path, _payload in calls))
        customer = next(payload for method, path, payload in calls if path == "/v2/customers" and method == "POST")
        self.assertEqual(customer["reference_id"], "Uline123")
        self.assertEqual(customer["email_address"], "member@example.com")


if __name__ == "__main__":
    unittest.main()
