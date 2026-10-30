"""運営グループへのデイリーレポート。送信先以外には何も返さない。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

os.environ["LINE_CHANNEL_SECRET"] = "test-secret"
os.environ["LINE_CHANNEL_ACCESS_TOKEN"] = "test-token"

import main
from fastapi.testclient import TestClient
from src.marketing.daily_report import IngestResult
from src.marketing.daily_report import ReportDeliveryError
from src.marketing.daily_report import build_report
from src.marketing.daily_report import collect_metrics
from src.marketing.daily_report import ingest_line_event
from src.marketing.daily_report import is_target_group
from src.marketing.daily_report import notify_ops_of_x_post
from src.marketing.daily_report import parse_impressions
from src.marketing.daily_report import push_text
from src.marketing.daily_report import x_post_notice
from src.marketing.daily_report import remember_group_id
from src.marketing.daily_report import daily_reports_enabled
from src.marketing.daily_report import report_loop_enabled
from src.marketing.daily_report import scheduled_end
from src.marketing.daily_report import send_daily_report
from src.marketing.daily_report import send_scheduled
from src.marketing import daily_report

JST = ZoneInfo("Asia/Tokyo")
AT = datetime(2026, 10, 28, 23, 50, tzinfo=JST)
OPS = "C" + "a" * 32
OTHER = "C" + "b" * 32


def _sign(body: bytes) -> str:
    digest = hmac.new(b"test-secret", body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


class ReportFormatTest(unittest.TestCase):
    def test_example_layout_and_cvr(self) -> None:
        text = build_report(
            {
                "impressions": 350_000,
                "impressions_prev": 300_000,
                "follows": 120,
                "follows_prev": 105,
                "blocks": 8,
                "blocks_prev": 10,
                "emails": 45,
                "emails_prev": 37,
                "paid": 12,
                "paid_prev": 9,
            },
            AT,
        )
        self.assertEqual(
            text,
            "\n".join(
                [
                    "［スロ極 デイリーマーケティングレポート］",
                    "📅 日付: 2026/10/28",
                    "------------------------------",
                    "■ X (Twitter)",
                    "・インプレッション数: 350,000 (前日比: +50,000)",
                    "",
                    "■ LINE",
                    "・新規友だち追加: 120人 (前日比: +15人)",
                    "・ブロック数: 8人 (前日比: -2人)",
                    "",
                    "■ Webアプリ (スロ極)",
                    "・メアド登録数: 45人 (前日比: +8人)",
                    "・カード決済(課金): 12人 (前日比: +3人)",
                    "------------------------------",
                    "■ ステップ別",
                    "・LINE → メアド登録率: 37.5%",
                    "・LINE → メアド登録の離脱率: 62.5%",
                    "・メアド登録 → 決済率: 26.7%",
                    "・メアド登録 → 決済の離脱率: 73.3%",
                    "------------------------------",
                    "■ 診断",
                    "✅ LINEからメアド登録への転換率は37.5%です。",
                    "✅ メアド登録からの決済率は26.7%で好調です。",
                    "------------------------------",
                    "本日もお疲れ様でした！",
                ]
            ),
        )

    def test_missing_impressions_and_zero_friends_hide_a_rate(self) -> None:
        text = build_report(
            {
                "impressions": None,
                "impressions_prev": None,
                "follows": 0,
                "follows_prev": 0,
                "blocks": 0,
                "blocks_prev": 1,
                "emails": 0,
                "emails_prev": 0,
                "paid": 0,
                "paid_prev": 0,
            },
            AT,
        )
        self.assertIn("・インプレッション数: 未入力", text)
        self.assertNotIn("未取得", text)
        self.assertIn("・カード決済(課金): 0人 (前日比: +0人)", text)
        self.assertIn("・LINE → メアド登録率: —", text)
        self.assertIn("・メアド登録 → 決済率: —", text)
        self.assertIn("ステップ診断は見送りました。", text)
        self.assertIn("・ブロック数: 0人 (前日比: -1人)", text)

    def test_low_registration_and_weak_payment_get_warnings(self) -> None:
        text = build_report(
            {
                "impressions": None,
                "impressions_prev": None,
                "follows": 100,
                "follows_prev": 80,
                "blocks": 4,
                "blocks_prev": 4,
                "emails": 20,
                "emails_prev": 40,
                "paid": 2,
                "paid_prev": 8,
            },
            AT,
        )
        self.assertIn("・インプレッション数: 未入力", text)
        self.assertIn("・LINE → メアド登録率: 20.0%", text)
        self.assertIn("・LINE → メアド登録の離脱率: 80.0%", text)
        self.assertIn("・メアド登録 → 決済率: 10.0%", text)
        self.assertIn("・メアド登録 → 決済の離脱率: 90.0%", text)
        self.assertIn("⚠️ LINEからアプリ登録への離脱率が80.0%と高めです。LINE案内文言の改善を推奨します。", text)
        self.assertIn("⚠️ メアド登録からの決済離脱率は90.0%です。登録後の決済案内を見直してください。", text)

    def test_impression_command_accepts_commas(self) -> None:
        self.assertEqual(parse_impressions("インプ 350000"), 350_000)
        self.assertEqual(parse_impressions("インプ　350,000"), 350_000)
        self.assertEqual(parse_impressions("インプレッション: 12"), 12)
        self.assertIsNone(parse_impressions("今日のインプ 350000"))
        self.assertIsNone(parse_impressions("インプ"))

    def test_dispatch_window_is_2350_through_early_morning(self) -> None:
        self.assertIsNone(scheduled_end(AT - timedelta(minutes=1)))
        self.assertEqual(scheduled_end(AT), AT)
        caught_up = scheduled_end(datetime(2026, 10, 29, 2, 0, tzinfo=JST))
        self.assertEqual(caught_up, AT)
        self.assertIsNone(scheduled_end(datetime(2026, 10, 29, 6, 0, tzinfo=JST)))

    def test_loop_stays_off_while_pytest_is_loaded(self) -> None:
        self.assertFalse(daily_reports_enabled())
        self.assertFalse(report_loop_enabled())

    def test_scheduled_send_stays_off_until_enabled(self) -> None:
        os.environ.pop("DAILY_REPORT_ENABLED", None)
        with mock.patch("src.marketing.daily_report.httpx.post") as post:
            self.assertFalse(send_scheduled(AT))
        post.assert_not_called()

    def test_x_post_notice_uses_periods_and_no_emoji(self) -> None:
        url = "https://x.com/i/web/status/1234567890"
        text = x_post_notice(url)
        self.assertEqual(
            text,
            "\n".join(
                [
                    "［X自動投稿完了のお知らせ］",
                    "",
                    "新しいXの投稿が完了しました。",
                    "以下のリンクより引用投稿および拡散のご協力をお願いします。",
                    "",
                    "投稿リンク:",
                    url,
                ]
            ),
        )
        self.assertNotIn("!", text)
        self.assertNotIn("！", text)
        self.assertNotRegex(text, r"[\U0001F300-\U0001FAFF]")
        with self.assertRaises(ReportDeliveryError):
            x_post_notice("https://example.com/status/1")


class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "square.sqlite")
        self.env = mock.patch.dict(
            os.environ,
            {"DATABASE_URL": "", "SQUARE_MEMBER_DB": self.path, "TARGET_LINE_GROUP_ID": "", "LINE_CHANNEL_ACCESS_TOKEN": "test-token"},
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        daily_report._READY.clear()
        daily_report._SENT.clear()
        daily_report._SKIP_LOGGED.clear()

    def test_join_records_the_group_without_replying(self) -> None:
        result = ingest_line_event(
            {"type": "join", "replyToken": "reply-join", "source": {"type": "group", "groupId": OPS}},
            now=AT,
        )
        self.assertEqual(result, IngestResult(consume=True, reply=""))
        self.assertTrue(is_target_group(OPS))

    def test_first_group_locks_and_a_later_group_cannot_replace_it(self) -> None:
        self.assertTrue(remember_group_id(OPS))
        self.assertFalse(remember_group_id(OTHER))
        os.environ.pop("TARGET_LINE_GROUP_ID", None)
        self.assertTrue(is_target_group(OPS))
        self.assertFalse(is_target_group(OTHER))
        self.assertFalse(remember_group_id("U" + "a" * 32))

    def test_env_group_is_not_replaced_by_webhook(self) -> None:
        os.environ["TARGET_LINE_GROUP_ID"] = OPS
        event = _group_message(OTHER, "インプ 999")
        result = ingest_line_event(event, now=AT)
        self.assertEqual(result, IngestResult(consume=True, reply=""))
        self.assertEqual(os.environ["TARGET_LINE_GROUP_ID"], OPS)
        self.assertFalse(is_target_group(OTHER))
        self.assertIsNone(collect_metrics(AT)["impressions"])

    def test_impression_command_is_kept_only_for_the_target_group(self) -> None:
        locked = ingest_line_event(_group_message(OPS, "スタンプの代わり", "text"), now=AT)
        self.assertEqual(locked.reply, "")
        stored = ingest_line_event(_group_message(OPS, "インプ 350,000", "text", _ms(AT)), now=AT)
        self.assertEqual(stored.reply, "")
        ignored = ingest_line_event(_group_message(OTHER, "インプ 1", "text", _ms(AT)), now=AT)
        self.assertEqual(ignored.reply, "")
        self.assertTrue(ignored.consume)
        metrics = collect_metrics(AT)
        self.assertEqual(metrics["impressions"], 350_000)

    def test_follow_block_email_and_payment_use_the_previous_24_hours(self) -> None:
        os.environ["TARGET_LINE_GROUP_ID"] = OPS
        ingest_line_event(_audience("follow", "U1", _ms(AT - timedelta(hours=1))), now=AT)
        ingest_line_event(_audience("follow", "U1", _ms(AT - timedelta(hours=1))), now=AT)
        ingest_line_event(_audience("follow", "U2", _ms(AT - timedelta(hours=30))), now=AT)
        ingest_line_event(_audience("unfollow", "U3", _ms(AT - timedelta(hours=2))), now=AT)
        ingest_line_event(_audience("unfollow", "U4", _ms(AT - timedelta(hours=26))), now=AT)
        self._member("sm_aaaaaaaaaaaaaaaa", "new@example.com", int((AT - timedelta(hours=3)).timestamp()), None, "")
        self._member(
            "sm_bbbbbbbbbbbbbbbb",
            "paid@example.com",
            int((AT - timedelta(hours=4)).timestamp()),
            int((AT - timedelta(hours=4)).timestamp()),
            "square",
        )
        self._member(
            "sm_cccccccccccccccc",
            "operator@example.com",
            int((AT - timedelta(hours=4)).timestamp()),
            int((AT - timedelta(hours=4)).timestamp()),
            "operator",
        )
        self._member("sm_dddddddddddddddd", "old@example.com", int((AT - timedelta(hours=40)).timestamp()), None, "")
        self._stripe_member("paid-stripe@example.com", int((AT - timedelta(hours=5)).timestamp()))
        metrics = collect_metrics(AT)
        self.assertEqual(metrics["follows"], 1)
        self.assertEqual(metrics["follows_prev"], 1)
        self.assertEqual(metrics["blocks"], 1)
        self.assertEqual(metrics["blocks_prev"], 1)
        self.assertEqual(metrics["emails"], 4)
        self.assertEqual(metrics["emails_prev"], 1)
        self.assertEqual(metrics["paid"], 2)
        self.assertEqual(metrics["paid_prev"], 0)

    def test_push_refuses_any_group_other_than_the_target(self) -> None:
        os.environ["TARGET_LINE_GROUP_ID"] = OPS
        with mock.patch("src.marketing.daily_report.httpx.post") as post:
            with self.assertRaises(ReportDeliveryError):
                push_text(OTHER, "秘密の数値")
            post.assert_not_called()
            post.return_value.status_code = 200
            self.assertEqual(push_text(OPS, "本文"), 200)
        sent = post.call_args.kwargs["json"]
        self.assertEqual(sent["to"], OPS)
        self.assertNotIn(OTHER, json.dumps(sent))

    def test_x_post_notice_is_pushed_only_to_the_ops_group(self) -> None:
        os.environ["TARGET_LINE_GROUP_ID"] = OPS
        url = "https://x.com/i/web/status/99"
        with mock.patch("src.marketing.daily_report.httpx.post") as post:
            post.return_value.status_code = 200
            self.assertEqual(notify_ops_of_x_post(url), 200)
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["to"], OPS)
        self.assertIn(url, body["messages"][0]["text"])
        self.assertEqual(post.call_count, 1)

    def test_invalid_env_group_uses_the_ledger_group(self) -> None:
        self.assertTrue(remember_group_id(OPS))
        os.environ["TARGET_LINE_GROUP_ID"] = "not-a-group"
        with mock.patch("src.marketing.daily_report.httpx.post") as post:
            post.return_value.status_code = 200
            self.assertTrue(send_daily_report(AT, mark=True))
        self.assertEqual(post.call_args.kwargs["json"]["to"], OPS)

    def test_stale_send_claim_is_taken_over(self) -> None:
        os.environ["TARGET_LINE_GROUP_ID"] = OPS
        daily_report._put_setting("report:2026-10-28", f"sending:{int(time.time()) - 1000}")
        with mock.patch("src.marketing.daily_report.httpx.post") as post:
            post.return_value.status_code = 200
            self.assertTrue(send_daily_report(AT, mark=True))
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs["json"]["to"], OPS)

    def test_x_post_notice_is_not_sent_without_a_target_group(self) -> None:
        with mock.patch("src.marketing.daily_report.httpx.post") as post:
            with self.assertRaises(ReportDeliveryError):
                notify_ops_of_x_post("https://x.com/i/web/status/99")
        post.assert_not_called()

    def test_scheduled_send_goes_out_once(self) -> None:
        os.environ["TARGET_LINE_GROUP_ID"] = OPS
        with mock.patch("src.marketing.daily_report.httpx.post") as post:
            post.return_value.status_code = 200
            self.assertTrue(send_daily_report(AT, mark=True))
            self.assertFalse(send_daily_report(AT, mark=True))
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs["json"]["to"], OPS)

    def test_missing_impressions_still_sends_the_automatic_counts(self) -> None:
        os.environ["TARGET_LINE_GROUP_ID"] = OPS
        with mock.patch("src.marketing.daily_report.httpx.post") as post:
            post.return_value.status_code = 200
            self.assertTrue(send_daily_report(AT, mark=True))
        body = post.call_args.kwargs["json"]["messages"][0]["text"]
        self.assertIn("・インプレッション数: 未入力", body)
        self.assertIn("・新規友だち追加:", body)
        self.assertIn("・メアド登録数:", body)
        self.assertIn("・カード決済(課金):", body)
        self.assertEqual(post.call_args.kwargs["json"]["to"], OPS)

    def _member(self, member_id: str, email: str, registered_at: int, paid_at: int | None, source: str) -> None:
        daily_report.target_group_id()
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                """
                INSERT INTO square_members (id, email, registered_at, paid_at, status_source)
                VALUES (?, ?, ?, ?, ?)
                """,
                (member_id, email, registered_at, paid_at, source),
            )
            connection.commit()
        finally:
            connection.close()

    def _stripe_member(self, email: str, when: int) -> None:
        daily_report.target_group_id()
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS members (
                  id TEXT PRIMARY KEY,
                  email TEXT NOT NULL DEFAULT '',
                  stripe_subscription_id TEXT NOT NULL DEFAULT '',
                  status TEXT NOT NULL DEFAULT '',
                  status_source TEXT NOT NULL DEFAULT '',
                  status_event_at BIGINT NOT NULL DEFAULT 0,
                  trial_end BIGINT,
                  created_at BIGINT NOT NULL DEFAULT 0
                )
                """
            )
            connection.execute(
                """
                INSERT INTO members (
                  id, email, stripe_subscription_id, status, status_source, status_event_at, trial_end, created_at
                ) VALUES ('m1', ?, 'sub_123', 'active', 'checkout', ?, NULL, ?)
                """,
                (email, when, when),
            )
            connection.commit()
        finally:
            connection.close()


class WebhookSecurityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(
            os.environ,
            {
                "DATABASE_URL": "",
                "SQUARE_MEMBER_DB": str(Path(self.tmp.name) / "square.sqlite"),
                "TARGET_LINE_GROUP_ID": "",
                "LINE_CHANNEL_SECRET": "test-secret",
                "LINE_CHANNEL_ACCESS_TOKEN": "test-token",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        daily_report._READY.clear()
        daily_report._SENT.clear()
        daily_report._SKIP_LOGGED.clear()
        self.client = TestClient(main.app)

    def test_sticker_captures_the_group_and_a_foreign_group_gets_silence(self) -> None:
        with mock.patch.object(main, "line_reply", new=mock.AsyncMock()) as reply:
            captured = _post(self.client, _group_message(OPS, "", "sticker"))
            foreign = _post(self.client, _group_message(OTHER, "インプ 999", "text"))
        self.assertEqual(captured.status_code, 200)
        self.assertEqual(foreign.status_code, 200)
        reply.assert_not_awaited()
        self.assertTrue(is_target_group(OPS))
        self.assertFalse(is_target_group(OTHER))

    def test_group_and_room_chats_never_receive_the_welcome(self) -> None:
        room = "R" + "c" * 32
        with mock.patch.object(main, "line_reply", new=mock.AsyncMock()) as reply:
            group = _post(self.client, _group_message(OPS, "こんにちは"))
            joined = _post(
                self.client,
                {
                    "type": "join",
                    "replyToken": "reply-join",
                    "source": {"type": "group", "groupId": OTHER},
                },
            )
            room_chat = _post(
                self.client,
                {
                    "type": "message",
                    "replyToken": "reply-room",
                    "source": {"type": "room", "roomId": room, "userId": "U1"},
                    "message": {"type": "text", "text": "はじめまして"},
                },
            )
        self.assertEqual(group.status_code, 200)
        self.assertEqual(joined.status_code, 200)
        self.assertEqual(room_chat.status_code, 200)
        reply.assert_not_awaited()
        self.assertNotIn(main._WELCOME, group.text + room_chat.text)

    def test_cron_trigger_requires_the_shared_secret(self) -> None:
        os.environ["REPORT_CRON_SECRET"] = "cron-secret"
        os.environ.pop("DAILY_REPORT_ENABLED", None)
        denied = self.client.post("/internal/daily-report", headers={"Authorization": "Bearer nope"})
        self.assertEqual(denied.status_code, 401)
        paused = self.client.post(
            "/internal/daily-report",
            headers={"Authorization": "Bearer cron-secret"},
        )
        self.assertEqual(paused.status_code, 200)
        self.assertTrue(paused.json()["skipped"])
        os.environ["DAILY_REPORT_ENABLED"] = "1"
        with mock.patch("src.marketing.daily_report.send_scheduled", return_value=True) as send:
            allowed = self.client.post(
                "/internal/daily-report",
                headers={"Authorization": "Bearer cron-secret"},
            )
        self.assertEqual(allowed.status_code, 200)
        self.assertTrue(allowed.json()["ok"])
        send.assert_called_once_with()
        os.environ.pop("DAILY_REPORT_ENABLED", None)

    def test_direct_impression_command_does_not_enter_the_ledger(self) -> None:
        os.environ["TARGET_LINE_GROUP_ID"] = OPS
        with mock.patch.object(main, "line_reply", new=mock.AsyncMock()) as reply:
            response = _post(
                self.client,
                {
                    "type": "message",
                    "replyToken": "reply-dm",
                    "timestamp": _ms(AT),
                    "source": {"type": "user", "userId": "U123"},
                    "message": {"type": "text", "text": "インプ 350000"},
                },
            )
        self.assertEqual(response.status_code, 200)
        reply.assert_awaited_once_with("reply-dm", main._WELCOME, quick_reply=main._TRIAL_BUTTON)
        self.assertIsNone(collect_metrics(AT)["impressions"])

    def test_unfollow_is_counted_without_a_reply_token(self) -> None:
        with mock.patch.object(main, "line_reply", new=mock.AsyncMock()) as reply:
            response = _post(self.client, _audience("unfollow", "U9", _ms(AT - timedelta(minutes=5))))
        self.assertEqual(response.status_code, 200)
        reply.assert_not_awaited()
        self.assertEqual(collect_metrics(AT)["blocks"], 1)


def _ms(at: datetime) -> int:
    return int(at.timestamp() * 1000)


def _post(client: TestClient, event: dict):
    body = json.dumps({"events": [event]}).encode()
    return client.post("/webhook", content=body, headers={"x-line-signature": _sign(body)})


def _group_message(group_id: str, text: str, message_type: str = "text", timestamp: int | None = None) -> dict:
    message: dict = {"type": message_type, "id": "1"}
    if message_type == "text":
        message["text"] = text
    event = {
        "type": "message",
        "replyToken": f"reply-{group_id[-4:]}-{message_type}",
        "source": {"type": "group", "groupId": group_id, "userId": "Uops"},
        "message": message,
    }
    if timestamp is not None:
        event["timestamp"] = timestamp
    return event


def _audience(kind: str, user_id: str, timestamp: int) -> dict:
    return {
        "type": kind,
        "timestamp": timestamp,
        "webhookEventId": f"{kind}-{user_id}-{timestamp}",
        "replyToken": "reply-follow" if kind == "follow" else "",
        "source": {"type": "user", "userId": user_id},
    }
