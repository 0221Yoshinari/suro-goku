from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

os.environ.setdefault("DASHBOARD_PASSWORD", "2026")

from src.dashboard.events import EventError
from src.dashboard.events import canonical_group
from src.dashboard.events import canonical_hall_id
from src.dashboard.events import create_event
from src.dashboard.events import delete_event
from src.dashboard.events import event_badge
from src.dashboard.events import group_joint_picks
from src.dashboard.events import today_jst
from src.dashboard.events import update_event
from src.scraper.halls import halls_in_group
from tests.test_dashboard_auth import absorb_cookies
from tests.test_dashboard_auth import request


def api(method: str, url: str, *, json_body: dict | None = None, cookies: dict | None = None):
    body = b""
    headers = {}
    if json_body is not None:
        body = json.dumps(json_body).encode()
        headers["content-type"] = "application/json"
    status, headers_out, payload, set_cookies = request(method, url, body=body, headers=headers, cookies=cookies)
    return status, headers_out, payload.decode(), set_cookies


class EventStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.path = Path(self.tmp.name) / "todays_events.json"
        self.patch = mock.patch("src.dashboard.events.EVENTS_PATH", self.path)
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()
        self.tmp.cleanup()

    def test_aliases_and_badges(self) -> None:
        self.assertEqual(canonical_group("wonderland_group"), "wonderland")
        self.assertEqual(canonical_hall_id("plaza_honten2"), "plaza-honten2")
        self.assertEqual(event_badge("合同", "ワンダーランド全店合同"), "🔥【合同】ワンダーランド全店合同")
        self.assertEqual(event_badge("取材", "スロパチ取材"), "📸 スロパチ取材")
        self.assertEqual(event_badge("特定日", "スロパチアワード景品"), "📌【特定日】スロパチアワード景品")

    def test_create_update_delete(self) -> None:
        created = create_event(
            {
                "date": "2026-10-01",
                "target": "group:wonderland",
                "event_type": "合同",
                "event_title": "ワンダーランド全店合同",
            }
        )
        self.assertEqual(created["group"], "wonderland")
        self.assertEqual(created["hall_id"], "")
        self.assertIn("ワンダーランド香椎本館", created["hall_names"])
        self.assertTrue(created["badge"].startswith("🔥【合同】"))

        updated = update_event(
            created["id"],
            {
                "date": "2026-10-02",
                "hall_id": "plaza_honten2",
                "event_type": "取材",
                "event_title": "スロパチ取材",
            },
        )
        self.assertEqual(updated["date"], "2026-10-02")
        self.assertEqual(updated["hall_id"], "plaza-honten2")
        self.assertEqual(updated["group"], "")
        self.assertEqual(updated["badge"], "📸 スロパチ取材")

        delete_event(created["id"])
        with self.assertRaises(EventError):
            delete_event(created["id"])

    def test_group_joint_picks_need_two_halls(self) -> None:
        halls = halls_in_group("wonderland")
        self.assertGreaterEqual(len(halls), 2)
        event = {
            "id": "evt-test",
            "date": "2026-10-01",
            "group": "wonderland",
            "hall_id": "",
            "event_type": "合同",
            "event_title": "ワンダーランド全店合同",
            "badge": "🔥【合同】ワンダーランド全店合同",
        }
        machines = [
            {"hall_name": halls[0], "machine_name": "L戦国乙女5", "status": "ALL_HIGH"},
            {"hall_name": halls[1], "machine_name": "スマスロ戦国乙女5", "status": "HIGH_56"},
            {"hall_name": halls[0], "machine_name": "単独機種", "status": "ALL_HIGH"},
            {"hall_name": halls[2], "machine_name": "通常機", "status": "HIGH_BASE"},
        ]
        picks = group_joint_picks(machines, [event])
        self.assertEqual(len(picks), 1)
        self.assertEqual(picks[0]["machine_name"], "L戦国乙女5")
        self.assertEqual(picks[0]["hall_count"], 2)
        self.assertEqual(picks[0]["all_high_count"], 1)
        self.assertEqual(picks[0]["high_56_count"], 1)

        hall_only = dict(event, group="", hall_id="plaza-honten2", event_type="取材")
        self.assertEqual(group_joint_picks(machines, [hall_only]), [])


class EventApiTest(unittest.TestCase):
    def setUp(self) -> None:
        from urllib.parse import urlencode

        self.tmp = TemporaryDirectory()
        self.path = Path(self.tmp.name) / "todays_events.json"
        self.patch = mock.patch("src.dashboard.events.EVENTS_PATH", self.path)
        self.patch.start()
        self.cookies: dict[str, str] = {}
        body = urlencode({"password": "2026", "next": "/"}).encode()
        status, _headers, _payload, set_cookies = request(
            "POST",
            "/login",
            body=body,
            headers={"content-type": "application/x-www-form-urlencoded"},
            cookies=self.cookies,
        )
        absorb_cookies(self.cookies, set_cookies)
        self.assertEqual(status, 303)
        self.token_patch = mock.patch.dict(os.environ, {"MASTER_PANEL_TOKEN": "event-test-token"})
        self.token_patch.start()
        unlock = urlencode({"master_token": "event-test-token", "next": "/master-panel-x7k3q9"}).encode()
        status, _headers, _payload, set_cookies = request(
            "POST",
            "/master-panel-x7k3q9/unlock",
            body=unlock,
            headers={"content-type": "application/x-www-form-urlencoded"},
            cookies=self.cookies,
        )
        absorb_cookies(self.cookies, set_cookies)
        self.assertEqual(status, 303)

    def tearDown(self) -> None:
        self.token_patch.stop()
        self.patch.stop()
        self.tmp.cleanup()

    def test_admin_event_roundtrip_and_dashboard_payload(self) -> None:
        today = today_jst()
        status, _headers, body, _cookies = api(
            "POST",
            "/master-panel-x7k3q9/events",
            json_body={
                "date": today,
                "target": "wonderland_group",
                "event_type": "合同",
                "event_title": "ワンダーランド全店合同",
            },
            cookies=self.cookies,
        )
        self.assertEqual(status, 200, body)
        created = json.loads(body)
        self.assertEqual(created["group"], "wonderland")

        status, _headers, body, _cookies = api("GET", "/master-panel-x7k3q9/events", cookies=self.cookies)
        self.assertEqual(status, 200)
        listed = json.loads(body)
        self.assertEqual(len(listed["events"]), 1)
        self.assertTrue(any(item["id"] == "wonderland" for item in listed["targets"]["groups"]))

        status, _headers, body, _cookies = api(
            "POST",
            "/master-panel-x7k3q9/events",
            json_body={"date": today, "event_type": "取材", "event_title": "名前だけ"},
            cookies=self.cookies,
        )
        self.assertEqual(status, 400)

        status, _headers, body, _cookies = api("GET", "/api/judgments", cookies=self.cookies)
        self.assertEqual(status, 200)
        payload = json.loads(body)
        titles = [item["event_title"] for item in payload["events"]]
        self.assertIn("ワンダーランド全店合同", titles)
        self.assertIn("group_joint_picks", payload)
        self.assertTrue(payload["events_updated_at"])

        status, _headers, body, _cookies = api(
            "DELETE",
            f"/master-panel-x7k3q9/events/{created['id']}",
            cookies=self.cookies,
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["ok"], True)
