from __future__ import annotations

import unittest
from datetime import date
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock
from zoneinfo import ZoneInfo

from src.dashboard.events import apply_auto_events
from src.dashboard.events import delete_event
from src.dashboard.events import list_events
from src.dashboard.events import public_event
from src.scraper.halls import Hall
from src.scraper.halls import load_halls
from src.scraper.x_monitor import account_prefectures
from src.scraper.x_monitor import enabled_accounts
from src.scraper.x_monitor import event_date_from_post
from src.scraper.x_monitor import is_announcement
from src.scraper.x_monitor import match_hall_id
from src.scraper.x_monitor import parse_announcement
from src.scraper.x_monitor import parse_dechau_html
from src.scraper.x_monitor import posts_from_feed_xml
from src.scraper.x_monitor import posts_to_events

JST = ZoneInfo("Asia/Tokyo")

SAMPLE = """
明日のオシホール
【1がつく日】

ワンダーランド全店合同

ワンデー取材

ワンダーランド香椎本館

スロパチアワード景品

玉屋∞本店

プラザ本店Ⅱ

3×3STAR取材
"""


class XMonitorParseTest(unittest.TestCase):
    def test_oshihall_post_becomes_hall_and_group_events(self) -> None:
        published = datetime(2026, 9, 30, 18, 11, tzinfo=JST)
        title = "2026年10月1日明日のオシホール"
        self.assertTrue(is_announcement(title, SAMPLE))
        self.assertEqual(event_date_from_post(title, SAMPLE, published), "2026-10-01")
        self.assertEqual(match_hall_id("プラザ本店II"), "plaza-honten2")
        events = parse_announcement(SAMPLE, event_date="2026-10-01", handle="PatiSlodays", source_url="https://x.com/PatiSlodays")
        by_key = {item["source_key"]: item for item in events}
        group = by_key["x:PatiSlodays:2026-10-01:group:wonderland"]
        self.assertEqual(group["event_type"], "合同")
        self.assertEqual(group["event_title"], "ワンデー取材")
        cover = by_key["x:PatiSlodays:2026-10-01:plaza-honten2"]
        self.assertEqual(cover["event_type"], "取材")
        self.assertEqual(cover["event_title"], "3×3STAR取材")
        kashii = next(item for item in events if item["hall_id"] == "wl-kashii-honkan")
        self.assertEqual(kashii["event_type"], "取材")
        self.assertIn("スロパチアワード景品", kashii["event_title"])
        honten = next(item for item in events if item["hall_id"] == "tamaya-honten")
        self.assertEqual(honten["event_type"], "特定日")
        self.assertEqual(honten["event_title"], "1がつく日")

    def test_colon_line_extracts_hall_and_coverage(self) -> None:
        events = parse_announcement(
            "プラザ本店II：3x3STAR取材",
            event_date="2026-10-01",
            handle="PatiSlodays",
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["hall_id"], "plaza-honten2")
        self.assertEqual(events[0]["event_type"], "取材")
        self.assertEqual(events[0]["event_title"], "3x3STAR取材")

    def test_result_roundup_is_not_an_announcement(self) -> None:
        title = "9/29(火) テキサス1111小倉結果まとめ"
        body = "総差枚\n178,700枚"
        self.assertFalse(is_announcement(title, body))

    def test_enabled_halls_match_outside_fukuoka(self) -> None:
        self.assertEqual(match_hall_id("エスパス日拓新宿歌舞伎町店"), "daidata-100949")
        self.assertEqual(match_hall_id("がちゃぽん菱江店", prefectures=["fukuoka"]), "daidata-101298")
        with mock.patch("src.scraper.x_monitor.load_halls", return_value=[]) as load:
            self.assertIsNone(match_hall_id("プラザ本店II"))
        load.assert_called_with(include_disabled=False)

    def test_equal_length_tie_prefers_source_prefecture(self) -> None:
        osaka = Hall(id="osaka-shop", name="マルハン本店", aliases=("マルハン本店",), enabled=True, prefecture="osaka")
        tokyo = Hall(id="tokyo-shop", name="マルハン本店", aliases=("マルハン本店",), enabled=True, prefecture="tokyo")
        longer = Hall(id="long-shop", name="マルハン本店店", aliases=("マルハン本店店",), enabled=True, prefecture="osaka")
        with mock.patch("src.scraper.x_monitor.load_halls", return_value=[osaka, tokyo]):
            self.assertEqual(match_hall_id("マルハン本店", prefectures=["tokyo"]), "tokyo-shop")
            self.assertEqual(match_hall_id("マルハン本店", prefectures=["osaka"]), "osaka-shop")
        with mock.patch("src.scraper.x_monitor.load_halls", return_value=[tokyo, longer]):
            self.assertEqual(match_hall_id("マルハン本店", prefectures=["tokyo"]), "long-shop")

    def test_new_machine_section_does_not_attach_other_halls_as_coverage(self) -> None:
        events = parse_announcement(
            """
            【6がつく日】
            プラザ本店
            👉でちゃう取材(ダイヤ)
            【新台入替🆕】
            123博多
            HINODE志免
            テンガイ八女
            玉屋トリアーダ宗像
            ワンダーランド香椎Ⅱ
            """,
            event_date="2026-10-06",
            handle="PatiSlodays",
            prefectures=["fukuoka"],
        )
        by_hall = {item["hall_id"]: item for item in events}
        hakata = by_hall["123-hakata"]
        self.assertEqual(hakata["event_title"], "新台入替")
        self.assertNotIn("HINODE", hakata["event_title"])
        self.assertNotIn("テンガイ", hakata["event_title"])
        self.assertEqual(by_hall["tamaya-munakata"]["event_title"], "新台入替")
        self.assertEqual(by_hall["wl-kashii-2"]["event_title"], "新台入替")
        plaza = next(item for item in events if item["hall_id"] == "plaza-honten")
        self.assertIn("でちゃう", plaza["event_title"])

    def test_group_event_stays_inside_source_prefectures(self) -> None:
        events = parse_announcement(
            "プラザ全店\n合同取材",
            event_date="2026-10-07",
            handle="PatiSlodays",
            prefectures=["fukuoka"],
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["group"], "plaza")
        self.assertEqual(events[0]["prefectures"], ["fukuoka"])
        names = public_event({**events[0], "id": "evt-plaza"})["hall_names"]
        self.assertIn("プラザ 本店", names)
        self.assertNotIn("スカイプラザ富士見店", names)


class XMonitorStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.path = Path(self.tmp.name) / "todays_events.json"
        self.patch = mock.patch("src.dashboard.events.EVENTS_PATH", self.path)
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()
        self.tmp.cleanup()

    def test_auto_events_update_without_duplicating_or_returning_after_delete(self) -> None:
        incoming = parse_announcement(
            "プラザ本店II：3x3STAR取材",
            event_date="2026-10-01",
            handle="PatiSlodays",
            source_url="https://x.com/PatiSlodays/status/1",
        )
        first = apply_auto_events(incoming)
        self.assertEqual(first["added"], 1)
        second = apply_auto_events(incoming)
        self.assertEqual(second["added"], 0)
        self.assertEqual(second["updated"], 0)
        self.assertEqual(len(list_events()), 1)
        delete_event(list_events()[0]["id"])
        third = apply_auto_events(incoming)
        self.assertEqual(third["added"], 0)
        self.assertEqual(list_events(), [])

    def test_one_source_does_not_erase_another_regions_events(self) -> None:
        fukuoka = parse_announcement(
            "プラザ本店II：3x3STAR取材",
            event_date="2026-10-01",
            handle="PatiSlodays",
            prefectures=["fukuoka"],
        )
        tokyo = parse_announcement(
            "エスパス日拓新宿歌舞伎町店：スロパチ取材",
            event_date="2026-10-01",
            handle="KantoNews",
            prefectures=["tokyo"],
        )
        apply_auto_events(fukuoka + tokyo)
        self.assertEqual(len(list_events()), 2)
        apply_auto_events(fukuoka, refreshed_sources=["x:PatiSlodays"])
        kept = list_events()
        self.assertEqual(len(kept), 2)
        self.assertEqual({item["hall_id"] for item in kept}, {"plaza-honten2", "daidata-100949"})

    def test_disabled_hall_is_not_stored(self) -> None:
        result = apply_auto_events(
            [
                {
                    "date": "2026-10-01",
                    "hall_id": "missing-hall",
                    "group": "",
                    "event_type": "取材",
                    "event_title": "取材",
                    "source": "x:KantoNews",
                    "source_key": "x:KantoNews:2026-10-01:missing-hall",
                }
            ]
        )
        self.assertEqual(result["added"], 0)
        self.assertEqual(list_events(), [])


SCHEDULE = {"hour": 18, "minute": 30, "retry_minutes": 60, "last_hour": 23}


class XMonitorScheduleTest(unittest.TestCase):
    def _patched(self, status: dict):
        return mock.patch.multiple(
            "src.scraper.x_monitor",
            read_daily_status=mock.Mock(return_value=status),
            schedule_settings=mock.Mock(return_value=SCHEDULE),
        )

    def test_before_first_slot_waits_until_1830(self) -> None:
        from src.scraper.x_monitor import attempt_due
        from src.scraper.x_monitor import next_attempt_at

        now = datetime(2026, 10, 1, 10, 0, tzinfo=JST)
        with self._patched({}):
            nxt = next_attempt_at(now)
            self.assertEqual(nxt.date().isoformat(), "2026-10-01")
            self.assertEqual((nxt.hour, nxt.minute), (18, 30))
            self.assertFalse(attempt_due(now))

    def test_missed_post_retries_on_the_next_hour(self) -> None:
        from src.scraper.x_monitor import attempt_due
        from src.scraper.x_monitor import next_attempt_at

        status = {"last_attempt_at": "2026-10-01T18:31:00+09:00"}
        early = datetime(2026, 10, 1, 19, 10, tzinfo=JST)
        due = datetime(2026, 10, 1, 19, 30, tzinfo=JST)
        with self._patched(status):
            nxt = next_attempt_at(early)
            self.assertEqual((nxt.hour, nxt.minute), (19, 30))
            self.assertFalse(attempt_due(early))
            self.assertTrue(attempt_due(due))

    def test_completed_day_waits_until_next_1830(self) -> None:
        from src.scraper.x_monitor import attempt_due
        from src.scraper.x_monitor import next_attempt_at

        status = {
            "completed_on": "2026-10-01",
            "last_attempt_at": "2026-10-01T19:40:00+09:00",
        }
        now = datetime(2026, 10, 1, 23, 30, tzinfo=JST)
        with self._patched(status):
            nxt = next_attempt_at(now)
            self.assertEqual(nxt.date().isoformat(), "2026-10-02")
            self.assertEqual((nxt.hour, nxt.minute), (18, 30))
            self.assertFalse(attempt_due(now))

    def test_sync_does_not_fetch_after_completion(self) -> None:
        from src.scraper.x_monitor import sync_x_events

        status = {"completed_on": datetime.now(JST).date().isoformat()}
        with TemporaryDirectory() as folder:
            lock = Path(folder) / "x.lock"
            with (
                self._patched(status),
                mock.patch("src.scraper.x_monitor.LOCK_PATH", lock),
                mock.patch("src.scraper.x_monitor.fetch_account_posts") as fetch,
            ):
                result = sync_x_events()
        self.assertTrue(result.get("skipped"))
        fetch.assert_not_called()


class XMonitorNationwideTest(unittest.TestCase):
    def test_region_prefectures_fill_in_when_the_account_omits_them(self) -> None:
        config = {
            "regions": [{"id": "kanto", "prefectures": ["tokyo", "saitama"]}],
            "accounts": [
                {"handle": "PatiSlodays", "enabled": True, "prefectures": ["fukuoka"]},
                {"handle": "KantoNews", "enabled": True, "region": "kanto"},
                {"handle": "", "enabled": True, "region": "kansai"},
                {"handle": "Off", "enabled": False},
            ],
        }
        self.assertEqual(account_prefectures(config["accounts"][0], config), ["fukuoka"])
        self.assertEqual(account_prefectures(config["accounts"][1], config), ["tokyo", "saitama"])
        self.assertEqual([item["handle"] for item in enabled_accounts(config)], ["PatiSlodays", "KantoNews"])

    def test_pending_account_is_skipped_after_it_captured_tomorrow(self) -> None:
        from src.scraper.x_monitor import _done_handles

        now = datetime(2026, 10, 6, 19, 0, tzinfo=JST)
        config = {
            "accounts": [
                {"handle": "PatiSlodays", "enabled": True},
                {"handle": "KantoNews", "enabled": True},
            ]
        }
        status = {"accounts_done": {"date": "2026-10-06", "handles": ["PatiSlodays"], "counts": {"PatiSlodays": 3}}}
        with mock.patch("src.scraper.x_monitor.read_daily_status", return_value=status):
            done = _done_handles(now)
        pending = [item for item in enabled_accounts(config) if item["handle"] not in done]
        self.assertEqual([item["handle"] for item in pending], ["KantoNews"])

    def test_sync_pauses_between_accounts(self) -> None:
        from src.scraper.x_monitor import sync_x_events

        config = {
            "interval_min": 60,
            "schedule": SCHEDULE,
            "regions": [],
            "fetch": {"gap_min_sec": 30, "gap_max_sec": 180},
            "accounts": [
                {"handle": "PatiSlodays", "enabled": True, "prefectures": ["fukuoka"], "feed_url": "https://example.test/a"},
                {"handle": "KantoNews", "enabled": True, "region": "kanto", "feed_url": "https://example.test/b"},
            ],
        }
        with TemporaryDirectory() as folder:
            root = Path(folder)
            with (
                mock.patch("src.scraper.x_monitor.load_monitor_config", return_value=config),
                mock.patch("src.scraper.x_monitor.LOCK_PATH", root / "x.lock"),
                mock.patch("src.scraper.x_monitor.DAILY_STATUS_PATH", root / "status.json"),
                mock.patch("src.scraper.x_monitor.STATE_PATH", root / "state.json"),
                mock.patch("src.scraper.x_monitor.LOG_PATH", root / "x.log"),
                mock.patch("src.scraper.x_monitor.fetch_account_posts", return_value=[]) as fetch,
                mock.patch("src.scraper.x_monitor.apply_auto_events", return_value={"added": 0, "updated": 0, "removed": 0}),
                mock.patch("src.scraper.x_monitor.pause_between_accounts", return_value=30) as pause,
                mock.patch("src.scraper.x_monitor._notify_capture", return_value=False),
            ):
                result = sync_x_events(force=True)
        self.assertEqual(fetch.call_count, 2)
        pause.assert_called_once()
        self.assertFalse(result.get("completed"))
        self.assertEqual(result.get("accounts"), ["PatiSlodays", "KantoNews"])

    def test_same_feed_url_is_not_paused_twice(self) -> None:
        from src.scraper.x_monitor import sync_x_events

        config = {
            "interval_min": 60,
            "schedule": SCHEDULE,
            "regions": [],
            "fetch": {"gap_min_sec": 30, "gap_max_sec": 180},
            "accounts": [
                {"handle": "dechau-tokai", "enabled": True, "feed_url": "https://www.dechau.com/", "parser": "dechau"},
                {"handle": "dechau-tohoku", "enabled": True, "feed_url": "https://www.dechau.com/", "parser": "dechau"},
            ],
        }
        with TemporaryDirectory() as folder:
            root = Path(folder)
            with (
                mock.patch("src.scraper.x_monitor.load_monitor_config", return_value=config),
                mock.patch("src.scraper.x_monitor.LOCK_PATH", root / "x.lock"),
                mock.patch("src.scraper.x_monitor.DAILY_STATUS_PATH", root / "status.json"),
                mock.patch("src.scraper.x_monitor.STATE_PATH", root / "state.json"),
                mock.patch("src.scraper.x_monitor.LOG_PATH", root / "x.log"),
                mock.patch("src.scraper.x_monitor.fetch_account_posts", return_value=[]) as fetch,
                mock.patch("src.scraper.x_monitor.apply_auto_events", return_value={"added": 0, "updated": 0, "removed": 0}),
                mock.patch("src.scraper.x_monitor.pause_between_accounts", return_value=30) as pause,
                mock.patch("src.scraper.x_monitor._notify_capture", return_value=False),
            ):
                sync_x_events(force=True)
        self.assertEqual(fetch.call_count, 2)
        pause.assert_not_called()


class XMonitorSourceParseTest(unittest.TestCase):
    def test_abbreviation_matches_only_inside_the_source_prefecture(self) -> None:
        halls = {hall.name: hall.id for hall in load_halls()}
        self.assertEqual(match_hall_id("エスパス西武新宿", ["tokyo"]), halls["エスパス日拓西武新宿駅前店"])
        self.assertEqual(match_hall_id("ジュラク王子", ["tokyo"]), halls["ジュラク王子店"])
        self.assertIsNone(match_hall_id("ジュラク2王子", ["tokyo"]))
        self.assertIsNone(match_hall_id("エスパス西武新宿"))

    def test_slash_date_and_sloeve_are_announcements(self) -> None:
        published = datetime(2026, 10, 6, 18, 0, tzinfo=JST)
        self.assertTrue(is_announcement("10月6日 明日の大阪すろいべ", "がちゃぽん菱江店"))
        self.assertFalse(is_announcement("10月6日 大阪すろいべ結果まとめ", "差枚"))
        self.assertEqual(event_date_from_post("10/7(水)東京都", "取材一覧", published), "2026-10-07")

    def test_east_slo_bulletin_links_digit_and_coverage(self) -> None:
        xml = """
        <feed xmlns="http://purl.org/atom/ns#">
          <entry>
            <title>10/7(水)東京都</title>
            <link href="https://example.test/tokyo"/>
            <issued>2026-10-06T12:00:00+09:00</issued>
            <summary>短い要約</summary>
            <content type="html"><![CDATA[★10/7(水)の取材一覧<br>【7日】<br>ピーアーク北綾瀬駅前<br>【スロパチ】<br>☆スロパチ来店取材<br>・ジュラク王子]]></content>
          </entry>
        </feed>
        """
        posts = posts_from_feed_xml(xml)
        events = posts_to_events(posts, "touslot_777", prefectures=["tokyo"])
        by_hall = {item["hall_id"]: item for item in events}
        halls = {hall.name: hall.id for hall in load_halls()}
        digit = by_hall[halls["ピーアーク北綾瀬駅前"]]
        coverage = by_hall[halls["ジュラク王子店"]]
        self.assertEqual(digit["event_type"], "特定日")
        self.assertEqual(digit["date"], "2026-10-07")
        self.assertEqual(coverage["event_type"], "取材")
        self.assertIn("来店", coverage["event_title"])

    def test_dechau_rows_stay_inside_the_account_prefecture_and_window(self) -> None:
        page = """
        <div class="item">
          <span class="days">10/7(水)</span><span class="prefs">愛知県</span>
          <h3>キクヤ一宮店</h3><h4>でちゃう取材</h4>
        </div>
        <div>
          <span class="days">10/1(木)</span><span class="prefs">愛知県</span>
          <h3>キクヤ一宮店</h3><h4>過去</h4>
        </div>
        <div>
          <span class="days">10/7(水)</span><span class="prefs">東京都</span>
          <h3>キングNo.1世田谷店</h3><h4>取材</h4>
        </div>
        """
        posts = parse_dechau_html(page, ["aichi"], today=date(2026, 10, 6))
        self.assertEqual(len(posts), 1)
        events = posts_to_events(posts, "dechau-tokai", prefectures=["aichi"])
        halls = {hall.name: hall.id for hall in load_halls()}
        self.assertEqual(events[0]["hall_id"], halls["キクヤ一宮店"])
        self.assertEqual(events[0]["date"], "2026-10-07")
        self.assertEqual(events[0]["event_title"], "でちゃう取材")
