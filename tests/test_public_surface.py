from __future__ import annotations

import os
import unittest

from src.dashboard.auth import COOKIE_NAME
from src.dashboard.auth import api_docs_enabled
from src.dashboard.auth import issue_token
from src.dashboard.events import member_event
from src.dashboard.events import present_member_payload
from src.dashboard.events import public_hall_id
from src.notify.discord import send_webhook
from src.scraper.capsolver import _post
from src.scraper.log_safe import failure_reason
from src.scraper.log_safe import redact
from src.scraper.pace import GENERIC_HTTP_USER_AGENT
from tests.test_dashboard_auth import request


class MemberPayloadTest(unittest.TestCase):
    def test_external_hall_prefix_is_replaced(self) -> None:
        hidden = public_hall_id("daidata-100494")
        self.assertTrue(hidden.startswith("hall-"))
        self.assertNotIn("daidata", hidden)
        self.assertNotIn("100494", hidden)
        self.assertEqual(public_hall_id("plaza-honten2"), "plaza-honten2")
        self.assertEqual(public_hall_id("maruhan-hakozaki"), "maruhan-hakozaki")

    def test_member_event_drops_source_fields(self) -> None:
        event = member_event(
            {
                "id": "evt-1",
                "hall_id": "daidata-100494",
                "target_label": "エスパス daidata-100494",
                "source": "x:dechau-tokai",
                "source_key": "x:dechau-tokai:1",
                "source_url": "https://www.dechau.com/",
                "event_title": "ほしまみ",
            }
        )
        self.assertNotIn("source", event)
        self.assertNotIn("source_url", event)
        self.assertNotIn("source_key", event)
        self.assertNotIn("daidata", event["hall_id"])
        self.assertNotIn("daidata-100494", event["target_label"])
        self.assertEqual(event["event_title"], "ほしまみ")

    def test_judgment_payload_drops_internal_paths(self) -> None:
        payload = present_member_payload(
            {
                "source": "site7",
                "masters_path": "/Users/kobakeinari/Projects/tamaya-slot-scraper/config/machines.json",
                "results": [{"source_file": "/tmp/secret.json"}],
                "events": [{"hall_id": "teramoba-202", "source_url": "https://example.com/a", "event_title": "取材"}],
                "machine_count": 1,
            }
        )
        self.assertNotIn("source", payload)
        self.assertNotIn("masters_path", payload)
        self.assertNotIn("results", payload)
        self.assertEqual(payload["machine_count"], 1)
        self.assertNotIn("source_url", payload["events"][0])
        self.assertNotIn("teramoba", payload["events"][0]["hall_id"])


class LogRedactionTest(unittest.TestCase):
    def test_urls_paths_and_cookie_values_are_removed(self) -> None:
        text = redact("開いた https://daidata.goraggio.com/100494 Cookie=sessionid path=/Users/kobakeinari/secret")
        self.assertNotIn("daidata.goraggio.com", text)
        self.assertNotIn("/Users/kobakeinari", text)
        self.assertNotIn("sessionid", text)
        self.assertIn("（URL）", text)
        self.assertIn("（パス）", text)

    def test_exception_text_becomes_a_fixed_reason(self) -> None:
        reason = failure_reason(RuntimeError("timed out https://example.com/hall Traceback (most recent call last): File x"))
        self.assertNotIn("example.com", reason)
        self.assertNotIn("Traceback", reason)
        self.assertNotIn("File", reason)


class DocsAndStaticGateTest(unittest.TestCase):
    def test_docs_follow_the_env_flag(self) -> None:
        previous = os.environ.get("DASHBOARD_API_DOCS")
        render = os.environ.get("RENDER")
        try:
            os.environ["DASHBOARD_API_DOCS"] = "0"
            os.environ["RENDER"] = "true"
            self.assertFalse(api_docs_enabled())
            os.environ["DASHBOARD_API_DOCS"] = "1"
            self.assertTrue(api_docs_enabled())
        finally:
            if previous is None:
                os.environ.pop("DASHBOARD_API_DOCS", None)
            else:
                os.environ["DASHBOARD_API_DOCS"] = previous
            if render is None:
                os.environ.pop("RENDER", None)
            else:
                os.environ["RENDER"] = render

    def test_docs_are_hidden_when_disabled(self) -> None:
        previous = os.environ.get("DASHBOARD_API_DOCS")
        os.environ["DASHBOARD_API_DOCS"] = "0"
        try:
            status, _headers, body, _cookies = request("GET", "/openapi.json")
            self.assertEqual(status, 404)
            self.assertNotIn(b"openapi", body.lower())
            status, _headers, body, _cookies = request("GET", "/docs")
            self.assertEqual(status, 404)
        finally:
            if previous is None:
                os.environ.pop("DASHBOARD_API_DOCS", None)
            else:
                os.environ["DASHBOARD_API_DOCS"] = previous

    def test_operator_static_requires_staff(self) -> None:
        status, _headers, body, _cookies = request("GET", "/static/admin.js")
        self.assertEqual(status, 404)
        self.assertNotIn(b"master-panel", body)
        status, _headers, body, _cookies = request("GET", "/static/halls.js")
        self.assertEqual(status, 404)
        status, _headers, body, _cookies = request("GET", "/static/app.js")
        self.assertEqual(status, 200)
        self.assertIn(b"function", body)
        status, _headers, body, _cookies = request(
            "GET",
            "/static/admin.js",
            cookies={COOKIE_NAME: issue_token()},
        )
        self.assertEqual(status, 200)
        self.assertIn(b"panelBase", body)


class OutboundUserAgentTest(unittest.TestCase):
    def test_generic_agent_has_no_repo_name(self) -> None:
        self.assertNotIn("tamaya", GENERIC_HTTP_USER_AGENT.lower())
        self.assertNotIn("slot-scraper", GENERIC_HTTP_USER_AGENT.lower())
        self.assertIn("Mozilla", GENERIC_HTTP_USER_AGENT)

    def test_discord_and_capsolver_use_the_generic_agent(self) -> None:
        import urllib.request

        seen: list[str] = []

        class _Response:
            status = 204

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b"{}"

        def _open(request, timeout=0):
            seen.append(request.get_header("User-agent"))
            return _Response()

        original = urllib.request.urlopen
        urllib.request.urlopen = _open
        try:
            os.environ["DISCORD_WEBHOOK_URL"] = "https://discord.example/webhook"
            send_webhook("hello")
            os.environ["CAPSOLVER_API_KEY"] = "test-key"
            try:
                _post("https://api.capsolver.com/createTask", {"task": {}})
            except Exception:
                pass
        finally:
            urllib.request.urlopen = original
            os.environ.pop("DISCORD_WEBHOOK_URL", None)
        self.assertTrue(seen)
        for agent in seen:
            self.assertEqual(agent, GENERIC_HTTP_USER_AGENT)
            self.assertNotIn("tamaya", (agent or "").lower())
