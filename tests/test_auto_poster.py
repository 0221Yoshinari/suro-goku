from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from src.poster.card import HEIGHT, WIDTH, render_summary
from src.poster.copy import compose_post, line_reply
from src.poster.events import resolve_events
from src.poster.select import select_top

ROOT = Path(__file__).resolve().parents[1]
DATE = "2026-10-26"
JUDGED = "2026-10-26T03:00:00+00:00"


def _units(pairs: list[tuple[str, int | None]]) -> list[dict]:
    return [{"dai_no": dai, "diff_medals": diff, "total_games": 2000} for dai, diff in pairs]


def _machine(
    name: str,
    *,
    diff: int | None,
    units: int = 8,
    games: int = 10000,
    all_high: bool = False,
    high_56: bool = False,
    excluded: bool = False,
    unit_rows: list[dict] | None = None,
) -> dict:
    return {
        "machine_name": name,
        "unit_count": units,
        "total_games": games,
        "avg_games": round(games / units, 1) if units else None,
        "diff_medals": diff,
        "status": "EXCLUDED" if excluded else ("ALL_HIGH" if all_high else "NOT_HIGH"),
        "is_all_high": all_high,
        "is_high_56": high_56,
        "is_high_base": False,
        "judgment_excluded": excluded,
        "units": unit_rows or [],
    }


def _hall(name: str, machines: list[dict], *, judged_at: str = JUDGED, unit_count: int | None = None) -> dict:
    return {
        "hall_name": name,
        "judged_at": judged_at,
        "unit_count": unit_count if unit_count is not None else sum(item["unit_count"] for item in machines),
        "machines": machines,
    }


def _pair(name: str, diff: int, **kwargs) -> list[dict]:
    return [
        _machine(name, diff=diff, **kwargs),
        _machine(f"{name}2", diff=max(diff // 2, 1), games=8000),
    ]


class GuardrailTest(unittest.TestCase):
    def test_hall_without_a_tool_signal_is_not_posted(self) -> None:
        quiet = _hall("周年の店", _pair("マイジャグラーV", 8000))
        visible = _hall("残った店", _pair("スマスロ番長", 9000, all_high=True))
        events = [{"date": DATE, "event_type": "取材", "event_title": "10周年", "hall_names": ["周年の店"]}]
        selection = select_top([quiet, visible], events, date=DATE)
        self.assertIsNotNone(selection.pick)
        assert selection.pick is not None
        self.assertEqual(selection.pick.hall_name, "残った店")
        self.assertEqual(selection.excluded_counts["insufficient_data"], 1)
        text = compose_post(selection.pick, date=DATE)
        self.assertNotIn("周年の店", text)
        self.assertNotIn("差枚", text)

    def test_running_hall_without_status_is_insufficient(self) -> None:
        cold = _hall(
            "低調店",
            [
                _machine("スマスロ番長", diff=80, games=20000),
                _machine("マイジャグラーV", diff=20, games=15000),
            ],
        )
        selection = select_top([cold], [], date=DATE)
        self.assertIsNone(selection.pick)
        self.assertEqual(selection.excluded_counts["insufficient_data"], 1)

    def test_flat_hall_without_status_is_insufficient(self) -> None:
        flat = _hall("横ばい店", [_machine("A", diff=0, games=8000), _machine("B", diff=0, games=8000)])
        selection = select_top([flat], [], date=DATE)
        self.assertEqual(selection.excluded_counts["insufficient_data"], 1)

    def test_missing_diff_is_insufficient(self) -> None:
        unknown = _hall("不明店", [_machine("A", diff=None, games=12000), _machine("B", diff=None, games=12000)])
        selection = select_top([unknown], [], date=DATE)
        self.assertIsNone(selection.pick)
        self.assertEqual(selection.excluded_counts["insufficient_data"], 1)

    def test_small_sample_is_insufficient(self) -> None:
        small = _hall("小さい店", _pair("スマスロ番長", 8000), unit_count=4)
        selection = select_top([small], [], date=DATE)
        self.assertEqual(selection.excluded_counts["insufficient_data"], 1)

    def test_sparse_diff_coverage_is_insufficient(self) -> None:
        machines = [_machine("主力", diff=20000, units=4, games=8000)]
        machines.extend(_machine(f"不明{index}", diff=None, units=4, games=8000) for index in range(3))
        selection = select_top([_hall("穴だらけ店", machines, unit_count=20)], [], date=DATE)
        self.assertEqual(selection.excluded_counts["insufficient_data"], 1)

    def test_partial_unit_diffs_do_not_invent_a_plus(self) -> None:
        thin = _machine(
            "スマスロ番長",
            diff=None,
            units=8,
            games=10000,
            unit_rows=_units([("1", 4000), ("2", 1000), ("3", None), ("4", None), ("5", None), ("6", None)]),
        )
        other = _machine("もう一台", diff=3000, games=8000)
        selection = select_top([_hall("一部だけ店", [thin, other])], [], date=DATE)
        self.assertEqual(selection.excluded_counts["insufficient_data"], 1)

    def test_featured_machine_uses_probability_not_unit_diff(self) -> None:
        rows = _units([("101", 2000), ("102", 1800), ("103", -300), ("104", None)])
        first = _machine("マイジャグラーV", diff=None, units=6, games=9000, all_high=True, unit_rows=rows)
        first["synthetic_probability"] = 148.2
        first["avg_games"] = 1500
        second = _machine("スマスロ番長", diff=4000, games=8000, high_56=True)
        selection = select_top([_hall("台番号店", [first, second])], [], date=DATE)
        assert selection.pick is not None
        machine = selection.pick.machines[0]
        self.assertEqual(machine.name, "マイジャグラーV")
        self.assertEqual(selection.pick.pattern, "hall")
        text = compose_post(selection.pick, date=DATE)
        self.assertIn("全台系挙動", text)
        self.assertIn("1/148.2", text)
        self.assertNotIn("差枚", text)
        self.assertNotIn("101番", text)

    def test_excluded_machine_does_not_block_a_visible_hall(self) -> None:
        mixed = _hall(
            "混ぜ店",
            [
                _machine("載せる", diff=8000, all_high=True),
                _machine("もう一台", diff=4000, high_56=True),
                _machine("除外台", diff=-20000, excluded=True, all_high=True),
            ],
        )
        selection = select_top([mixed], [], date=DATE)
        assert selection.pick is not None
        names = [machine.name for machine in selection.pick.machines]
        self.assertIn("載せる", names)
        self.assertNotIn("除外台", names)

    def test_excluded_machine_is_not_featured(self) -> None:
        hall = _hall(
            "表に出す店",
            [
                _machine("載せない", diff=9000, excluded=True, all_high=True),
                _machine("載せる", diff=3000, all_high=True),
                _machine("もう一台", diff=2500, high_56=True),
            ],
        )
        selection = select_top([hall], [], date=DATE)
        assert selection.pick is not None
        names = [machine.name for machine in selection.pick.machines]
        self.assertEqual(names[0], "載せる")
        self.assertNotIn("載せない", names)

    def test_other_day_and_jst_boundary(self) -> None:
        yesterday = _hall("昨日店", _pair("スマスロ番長", 9000, all_high=True), judged_at="2026-10-25T10:00:00+00:00")
        next_morning = _hall("日付をまたいだ店", _pair("スマスロ番長", 9000, all_high=True), judged_at="2026-10-26T16:00:00+00:00")
        today = select_top([yesterday, next_morning], [], date=DATE)
        self.assertIsNone(today.pick)
        self.assertEqual(today.excluded_counts["other_day"], 2)
        rolled = select_top([next_morning], [], date="2026-10-27")
        assert rolled.pick is not None
        self.assertEqual(rolled.pick.hall_name, "日付をまたいだ店")

    def test_missing_timestamp_is_insufficient(self) -> None:
        hall = _hall("時刻なし店", _pair("スマスロ番長", 9000), judged_at="")
        selection = select_top([hall], [], date=DATE)
        self.assertEqual(selection.excluded_counts["insufficient_data"], 1)


class RankingTest(unittest.TestCase):
    def test_anniversary_outranks_a_hotter_juggler_hall(self) -> None:
        anniversary = _hall(
            "周年店",
            [
                _machine("マイジャグラーV", diff=4000, all_high=True, games=12000, unit_rows=_units([("12", 2500)])),
                _machine("スマスロ番長", diff=2000, games=8000, high_56=True),
            ],
        )
        juggler = _hall(
            "ジャグ店",
            [
                _machine("マイジャグラーV", diff=20000, games=20000, high_56=True),
                _machine("アイムジャグラー", diff=15000, games=18000, high_56=True),
            ],
        )
        events = [{"date": DATE, "event_type": "取材", "event_title": "10周年", "hall_names": ["周年店"]}]
        selection = select_top([juggler, anniversary], events, date=DATE)
        assert selection.pick is not None
        self.assertEqual(selection.pick.hall_name, "周年店")
        self.assertEqual(selection.pick.rank, "S")
        self.assertEqual(selection.pick.event_tier, "anniversary")
        self.assertEqual(selection.runners_up[0].hall_name, "ジャグ店")
        self.assertEqual(selection.runners_up[0].rank, "A")
        self.assertTrue(selection.runners_up[0].juggler_hot)

    def test_specific_day_is_s_and_all_high_without_event_is_a(self) -> None:
        specific = _hall("特定日の店", _pair("スマスロ番長", 6000, all_high=True))
        all_high = _hall("全台の店", [_machine("スマスロ番長", diff=7000, all_high=True), _machine("隣", diff=3000)])
        events = [{"date": DATE, "event_type": "特定日", "event_title": "7のつく日", "hall_names": ["特定日の店"]}]
        selection = select_top([all_high, specific], events, date=DATE)
        assert selection.pick is not None
        self.assertEqual(selection.pick.rank, "S")
        self.assertEqual(selection.pick.hall_name, "特定日の店")
        self.assertEqual(selection.runners_up[0].rank, "A")

    def test_model_prefix_is_hidden_in_the_post(self) -> None:
        hall = _hall("表記店", [_machine("46L東京喰種FT", diff=6000, all_high=True), _machine("L番長", diff=3000, high_56=True)])
        selection = select_top([hall], [], date=DATE)
        assert selection.pick is not None
        self.assertEqual([machine.name for machine in selection.pick.machines], ["東京喰種", "番長"])

    def test_shallow_all_high_is_not_posted(self) -> None:
        shallow = _hall(
            "朝イチ店",
            [
                _machine("モンキーターン5CE", diff=None, units=6, games=2400, all_high=True),
                _machine("隣", diff=None, units=6, games=3000),
            ],
        )
        shallow["machines"][0]["avg_games"] = 400
        events = [{"date": DATE, "event_type": "周年", "event_title": "10周年", "hall_names": ["朝イチ店"]}]
        selection = select_top([shallow], events, date=DATE)
        self.assertIsNone(selection.pick)
        self.assertEqual(selection.excluded_counts["insufficient_data"], 1)

    def test_halfwidth_machine_name_is_readable(self) -> None:
        hall = _hall("半角店", [_machine("ﾏｲｼﾞｬｸﾞﾗｰV", diff=8000, all_high=True), _machine("番長", diff=3000, high_56=True)])
        selection = select_top([hall], [], date=DATE)
        assert selection.pick is not None
        self.assertEqual(selection.pick.machines[0].name, "マイジャグラーV")
        self.assertTrue(selection.pick.juggler_hot)


class CopyAndImageTest(unittest.TestCase):
    def test_post_mentions_the_selected_hall_only(self) -> None:
        anniversary = _hall(
            "周年店",
            [
                _machine("マイジャグラーV", diff=5000, all_high=True, unit_rows=_units([("12", 3200)])),
                _machine("東京喰種", diff=3000, high_56=True),
            ],
        )
        anniversary["machines"][0]["digit_judgments"] = [
            {"digit": 2, "is_all_high": True, "status": "ALL_HIGH"},
            {"digit": 8, "is_high_56": True, "status": "HIGH_56"},
        ]
        events = [{"date": DATE, "event_type": "合同", "event_title": "10周年", "hall_names": ["周年店"]}]
        selection = select_top([anniversary], events, date=DATE)
        assert selection.pick is not None
        text = compose_post(selection.pick, date=DATE)
        self.assertIn("周年店", text)
        self.assertIn("10周年", text)
        self.assertIn("マイジャグラーV", text)
        self.assertIn("全台系挙動", text)
        self.assertIn("全6数値", text)
        self.assertNotIn("完全放置", text)
        self.assertNotIn("平均", text)
        self.assertNotIn("http", text)
        self.assertNotIn("！", text)
        reply = line_reply()
        self.assertIn("1週間無料でスロ極を使ってみてください↓", reply)
        self.assertIn("https://", reply)
        self.assertNotIn("#", text)
        self.assertLessEqual(len(text), 280)
        self.assertNotIn("差枚", text)
        self.assertNotIn("マイナス", text)

    def test_digit_post_uses_average_setting(self) -> None:
        hall = _hall("末尾の店", [_machine("番長", diff=None, games=8000), _machine("隣", diff=None, games=8000)])
        hall["digits"] = [
            {
                "digit": 0,
                "avg_setting": 5.39,
                "unit_count": 13,
                "total_games": 18000,
                "avg_games": 1384.6,
                "status": "HIGH_56",
                "status_label": "全56数値",
            }
        ]
        selection = select_top([hall], [], date=DATE)
        assert selection.pick is not None
        self.assertEqual(selection.pick.pattern, "digits")
        text = compose_post(selection.pick, date=DATE)
        self.assertIn("末尾の店　12時時点", text)
        self.assertIn("末尾0が動いてる。平均設定5.39、13台が全56数値。", text)
        self.assertNotIn("です", text)
        self.assertNotIn("見ています", text)
        self.assertNotIn("候補に入れて", text)
        self.assertNotIn("見てからでいい", text)
        self.assertNotIn("共有してみてください", text)
        self.assertNotIn("1週間無料", text)
        self.assertNotIn("📊", text)
        self.assertNotIn("http", text)
        self.assertNotIn("！", text)
        self.assertIn("https://", line_reply())
        self.assertNotIn("#", text)
        self.assertNotIn("差枚", text)

    def test_summary_image_is_a_timeline_png(self) -> None:
        hall = _hall(
            "周年店",
            [
                _machine("マイジャグラーV", diff=8200, all_high=True, unit_rows=_units([("2032", 4100), ("2038", 2500)])),
                _machine("東京喰種", diff=6100, high_56=True, games=9000),
                _machine("スマスロ番長", diff=2800, games=7000),
            ],
        )
        events = [{"date": DATE, "event_type": "取材", "event_title": "10周年", "hall_names": ["周年店"]}]
        selection = select_top([hall], events, date=DATE)
        assert selection.pick is not None
        image = render_summary(selection.pick, date=DATE)
        self.assertEqual(image.size, (WIDTH, HEIGHT))
        self.assertEqual(image.mode, "RGB")
        extrema = image.getextrema()
        self.assertTrue(any(low != high for low, high in extrema))


class EventResolveTest(unittest.TestCase):
    def test_group_event_expands_to_prefecture_halls(self) -> None:
        halls = [
            SimpleNamespace(id="a", name="福岡店", aliases=("福岡の店",), group="tamaya", prefecture="fukuoka"),
            SimpleNamespace(id="b", name="東京店", aliases=(), group="tamaya", prefecture="tokyo"),
        ]
        rows = [{"date": DATE, "group": "tamaya", "event_type": "合同", "event_title": "全店合同", "prefectures": ["fukuoka"]}]
        resolved = resolve_events(rows, DATE, halls=halls)
        self.assertEqual(resolved[0]["hall_names"], ["福岡店", "福岡の店"])
        self.assertNotIn("東京店", resolved[0]["hall_names"])


class CommandTest(unittest.TestCase):
    def test_afternoon_slot_uses_juggler_top4_dry_run(self) -> None:
        poster = _load_auto_poster()
        island = {
            "hall_name": "テストホール",
            "family_label": "マイジャグラーV",
            "badge_label": "全6",
            "synthetic_probability": 171.0,
            "rb_probability": 320.0,
            "score": 80,
            "badge": "all6",
        }
        png = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
            b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            out_dir = base / "juggler" / DATE / "afternoon"
            out_dir.mkdir(parents=True)
            (out_dir / "01.png").write_bytes(png)
            text_path = base / "post.txt"
            meta = base / "post.json"
            argv = [
                "--halls",
                str(base / "halls"),
                "--date",
                DATE,
                "--image",
                str(base / "summary.png"),
                "--text",
                str(text_path),
                "--meta",
                str(meta),
                "--dry-run",
                "--allow-off-hours",
                "--slot",
                "afternoon",
                "--skip-capture",
            ]
            with (
                mock.patch.object(poster, "DEFAULT_JUGGLER_DIR", base / "juggler"),
                mock.patch("src.poster.juggler_pickup_flow.rank_juggler_islands", return_value=[island]),
            ):
                code = poster.main(argv)
            self.assertEqual(code, 0)
            body = text_path.read_text(encoding="utf-8")
            self.assertIn("ジャグラーモード", body)
            self.assertIn("テストホール", body)
            self.assertNotIn("全台系挙動", body)
            payload = json.loads(meta.read_text(encoding="utf-8"))
            self.assertEqual(payload.get("mode"), "juggler_top4")
            self.assertFalse(payload["posted"])

    def test_morning_slot_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            halls = base / "halls" / "one"
            halls.mkdir(parents=True)
            (halls / "latest.json").write_text(
                json.dumps(_hall("店", _pair("スマスロ番長", 9000, all_high=True)), ensure_ascii=False),
                encoding="utf-8",
            )
            image = base / "summary.png"
            image.write_bytes(b"stale")
            text_path = base / "post.txt"
            text_path.write_text("stale", encoding="utf-8")
            meta = base / "post.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "auto_poster.py"),
                    "--halls",
                    str(halls.parent),
                    "--events",
                    str(base / "missing-events.json"),
                    "--date",
                    DATE,
                    "--image",
                    str(image),
                    "--text",
                    str(text_path),
                    "--meta",
                    str(meta),
                    "--allow-off-hours",
                    "--slot",
                    "morning",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("朝のターゲット投稿は停止", result.stderr)
            self.assertFalse(image.is_file())
            self.assertFalse(text_path.is_file())
            payload = json.loads(meta.read_text(encoding="utf-8"))
            self.assertEqual(payload.get("skipped"), "morning_disabled")
            self.assertFalse(payload["posted"])

    def test_command_removes_stale_promo_when_nothing_qualifies(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            halls = base / "halls"
            halls.mkdir(parents=True)
            image = base / "summary.png"
            text_path = base / "post.txt"
            image.write_bytes(b"stale")
            text_path.write_text("stale", encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "auto_poster.py"),
                    "--halls",
                    str(halls.parent),
                    "--events",
                    str(base / "missing-events.json"),
                    "--date",
                    DATE,
                    "--image",
                    str(image),
                    "--text",
                    str(text_path),
                    "--meta",
                    str(base / "post.json"),
                    "--dry-run",
                    "--allow-off-hours",
                    "--slot",
                    "afternoon",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(image.exists())
            self.assertFalse(text_path.exists())
            payload = json.loads((base / "post.json").read_text(encoding="utf-8"))
            self.assertTrue(payload["ok"])
            self.assertEqual(payload.get("skipped"), "no_juggler")
            self.assertIn("ジャグラー島が無い", result.stderr)


class PublishGateTest(unittest.TestCase):
    def test_posts_only_when_ok_and_image_exist(self) -> None:
        from src.poster.x_api import should_publish

        self.assertFalse(should_publish(ok=False, dry_run=False, image_ready=True))
        self.assertFalse(should_publish(ok=True, dry_run=True, image_ready=True))
        self.assertFalse(should_publish(ok=True, dry_run=False, image_ready=False))
        self.assertTrue(should_publish(ok=True, dry_run=False, image_ready=True))
        self.assertTrue(should_publish(ok=True, dry_run=False, image_ready=False, text_only=True))
        self.assertFalse(should_publish(ok=True, dry_run=False, image_ready=False, text_only=False))

    def test_same_slot_is_not_posted_twice(self) -> None:
        from src.poster.ledger import posted_entry, remember

        ledger: dict = {}
        self.assertIsNone(posted_entry(ledger, "2026-10-29", "morning", "プラザ 天神"))
        remember(ledger, "2026-10-29", "morning", "プラザ 天神", "100", "101")
        found = posted_entry(ledger, "2026-10-29", "morning", "プラザ 天神")
        self.assertEqual(found["tweet_id"], "100")
        self.assertIsNone(posted_entry(ledger, "2026-10-29", "midday", "プラザ 天神"))

    def test_publish_attaches_image_without_leaking_secret(self) -> None:
        import os

        import httpx

        from src.poster.x_api import publish_tweet

        keys = ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET")
        saved = {key: os.environ.get(key) for key in keys}
        os.environ["X_API_KEY"] = "consumer-key"
        os.environ["X_API_SECRET"] = "consumer-secret-value"
        os.environ["X_ACCESS_TOKEN"] = "access-token"
        os.environ["X_ACCESS_TOKEN_SECRET"] = "access-secret-value"
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            header = request.headers.get("authorization", "")
            self.assertNotIn("consumer-secret-value", header)
            self.assertNotIn("access-secret-value", header)
            if request.url.path.endswith("/2/media/upload"):
                return httpx.Response(200, json={"data": {"id": "99"}})
            body = json.loads(request.content.decode("utf-8"))
            self.assertEqual(body["text"], "プラザ長浜")
            self.assertEqual(body["media"]["media_ids"], ["99"])
            self.assertNotIn("マイナス", body["text"])
            return httpx.Response(201, json={"data": {"id": "555", "text": body["text"]}})

        try:
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                result = publish_tweet("プラザ長浜", b"\x89PNG", client=client)
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.assertEqual(result["id"], "555")
        self.assertEqual(len(seen), 2)

    def test_text_only_post_has_no_media(self) -> None:
        import os

        import httpx

        from src.poster.x_api import publish_tweet

        keys = ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET")
        saved = {key: os.environ.get(key) for key in keys}
        os.environ["X_API_KEY"] = "consumer-key"
        os.environ["X_API_SECRET"] = "consumer-secret-value"
        os.environ["X_ACCESS_TOKEN"] = "access-token"
        os.environ["X_ACCESS_TOKEN_SECRET"] = "access-secret-value"

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertFalse(request.url.path.endswith("/2/media/upload"))
            body = json.loads(request.content.decode("utf-8"))
            self.assertEqual(body["text"], "本日はこのホールを追います。")
            self.assertNotIn("media", body)
            return httpx.Response(201, json={"data": {"id": "777", "text": body["text"]}})

        try:
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                result = publish_tweet("本日はこのホールを追います。", client=client)
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.assertEqual(result["id"], "777")

    def test_write_probe_rejects_read_only_app(self) -> None:
        import os

        import httpx

        from src.poster.x_api import XApiError, assert_write_access

        keys = ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET")
        saved = {key: os.environ.get(key) for key in keys}
        os.environ["X_API_KEY"] = "consumer-key"
        os.environ["X_API_SECRET"] = "consumer-secret-value"
        os.environ["X_ACCESS_TOKEN"] = "access-token"
        os.environ["X_ACCESS_TOKEN_SECRET"] = "access-secret-value"

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.content, b"{}")
            return httpx.Response(
                403,
                json={"detail": "Your client app is not configured with the appropriate oauth1 app permissions"},
            )

        try:
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                with self.assertRaises(XApiError) as caught:
                    assert_write_access(client=client)
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.assertIn("Read and Write", str(caught.exception))

    def test_reply_points_at_the_main_post(self) -> None:
        import os

        import httpx

        from src.poster.x_api import publish_reply

        keys = ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET")
        saved = {key: os.environ.get(key) for key in keys}
        os.environ["X_API_KEY"] = "consumer-key"
        os.environ["X_API_SECRET"] = "consumer-secret-value"
        os.environ["X_ACCESS_TOKEN"] = "access-token"
        os.environ["X_ACCESS_TOKEN_SECRET"] = "access-secret-value"

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            self.assertEqual(body["reply"]["in_reply_to_tweet_id"], "555")
            self.assertIn("https://", body["text"])
            self.assertNotIn("consumer-secret-value", request.headers.get("authorization", ""))
            return httpx.Response(201, json={"data": {"id": "556", "text": body["text"]}})

        try:
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                result = publish_reply("1週間無料でスロ極を使ってみてください↓\nhttps://lin.ee/7XxLBBy", "555", client=client)
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.assertEqual(result["id"], "556")


class DiscordPreviewTest(unittest.TestCase):
    def test_preview_posts_text_and_png_without_the_webhook(self) -> None:
        from unittest.mock import patch

        import httpx

        from src.poster.discord_preview import send_x_preview

        def handler(request: httpx.Request) -> httpx.Response:
            body = request.content
            self.assertIn("【X投稿プレビュー】".encode(), body)
            self.assertIn("プラザ長浜".encode(), body)
            self.assertIn(b"image-1.png", body)
            self.assertNotIn(b"api/webhooks", body)
            return httpx.Response(204)

        with patch("src.poster.discord_preview.webhook_url", return_value="https://preview.example/hook"):
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                status = send_x_preview("プラザ長浜\n全台系挙動", b"\x89PNG", client=client)
        self.assertEqual(status, 204)

    def test_missing_webhook_does_not_invent_a_url(self) -> None:
        from unittest.mock import patch

        from src.poster.discord_preview import DiscordPreviewError, send_x_preview

        with patch("src.poster.discord_preview.webhook_url", return_value=""):
            with self.assertRaises(DiscordPreviewError) as caught:
                send_x_preview("本文", b"png")
        self.assertNotIn("http", str(caught.exception))


class DiscordPreviewTest(unittest.TestCase):
    def test_preview_posts_text_and_png_without_the_webhook(self) -> None:
        from unittest.mock import patch

        import httpx

        from src.poster.discord_preview import send_x_preview

        def handler(request: httpx.Request) -> httpx.Response:
            body = request.content
            self.assertIn("【X投稿プレビュー】".encode(), body)
            self.assertIn("プラザ長浜".encode(), body)
            self.assertIn(b"image-1.png", body)
            self.assertNotIn(b"api/webhooks", body)
            return httpx.Response(204)

        with patch("src.poster.discord_preview.webhook_url", return_value="https://preview.example/hook"):
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                status = send_x_preview("プラザ長浜\n全台系挙動", b"\x89PNG", client=client)
        self.assertEqual(status, 204)

    def test_missing_webhook_does_not_invent_a_url(self) -> None:
        from unittest.mock import patch

        from src.poster.discord_preview import DiscordPreviewError, send_x_preview

        with patch("src.poster.discord_preview.webhook_url", return_value=""):
            with self.assertRaises(DiscordPreviewError) as caught:
                send_x_preview("本文", b"png")
        self.assertNotIn("http", str(caught.exception))


class CaptureScreenTest(unittest.TestCase):
    def test_capture_crops_hall_screen_to_timeline_size(self) -> None:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        from src.poster.capture import PHONE_HEIGHT, PHONE_SCALE, PHONE_WIDTH, capture_hall

        page = """<!DOCTYPE html>
<html><body style="margin:0;background:#08101c;color:#fff;font-family:sans-serif">
<select id="hall-select"><option value="投稿する店">投稿する店</option></select>
<h1 style="font-size:42px">スロ極 投稿する店</h1>
<p>全台系挙動</p>
</body></html>""".encode("utf-8")

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                body = b"ok" if self.path.startswith("/health") else page
                kind = "text/plain" if body == b"ok" else "text/html; charset=utf-8"
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt: str, *args) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        import threading

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as folder:
                dest = Path(folder) / "summary.png"
                capture_hall("投稿する店", dest, base_url=f"http://127.0.0.1:{port}")
                with Image.open(dest) as opened:
                    self.assertEqual(opened.format, "PNG")
                    self.assertEqual(opened.size[0], PHONE_WIDTH * PHONE_SCALE)
                    self.assertAlmostEqual(opened.size[1], PHONE_HEIGHT * PHONE_SCALE, delta=2)
        finally:
            server.shutdown()


class ChaseSelectionTest(unittest.TestCase):
    def test_anniversary_outranks_a_specific_day(self) -> None:
        from src.poster.chase import select_chase

        catalog = {
            "周年店": ("osaka", "周年店"),
            "特定店": ("osaka", "特定店"),
        }
        events = [
            {"date": DATE, "event_type": "周年", "event_title": "10周年", "hall_names": ["周年店"]},
            {"date": DATE, "event_type": "特定日", "event_title": "7のつく日", "hall_names": ["特定店"]},
        ]
        targets = select_chase([], events, date=DATE, areas=("osaka",), per_area=2, catalog=catalog)
        self.assertEqual([item.hall_name for item in targets], ["周年店", "特定店"])
        self.assertGreater(targets[0].points, targets[1].points)

    def test_tie_prefers_the_higher_past_average(self) -> None:
        from src.poster.chase import select_chase

        catalog = {"高稼働店": ("fukuoka", "高稼働店"), "低稼働店": ("fukuoka", "低稼働店")}
        events = [
            {"date": DATE, "event_type": "特定日", "event_title": "8のつく日", "hall_names": ["高稼働店"]},
            {"date": DATE, "event_type": "特定日", "event_title": "8のつく日", "hall_names": ["低稼働店"]},
        ]
        halls = [
            _hall("高稼働店", [_machine("番長", diff=None, units=10, games=30000)]),
            _hall("低稼働店", [_machine("番長", diff=None, units=10, games=12000)]),
        ]
        targets = select_chase(halls, events, date=DATE, areas=("fukuoka",), per_area=2, catalog=catalog)
        self.assertEqual(targets[0].hall_name, "高稼働店")
        self.assertGreater(targets[0].avg_games, targets[1].avg_games)

    def test_rainbow_dechau_scores_as_a_strong_cover(self) -> None:
        from src.poster.chase import event_points

        strong = event_points({"event_type": "取材", "event_title": "でちゃう取材(虹)"}, "fukuoka")
        plain = event_points({"event_type": "取材", "event_title": "でちゃう取材(金)"}, "fukuoka")
        self.assertGreater(strong, plain)

    def test_tokyo_strong_events_and_empty_areas_stay_independent(self) -> None:
        from src.poster.chase import AREAS, event_points, select_chase

        self.assertEqual(AREAS, ("fukuoka", "osaka", "tokyo"))
        visitor = event_points({"event_type": "取材", "event_title": "クロロエンジェルＳ来店"}, "tokyo")
        station = event_points({"event_type": "取材", "event_title": "スロパチステーション"}, "tokyo")
        atuhime = event_points({"event_type": "取材", "event_title": "アツ姫"}, "tokyo")
        dechau = event_points({"event_type": "取材", "event_title": "でちゃう取材(金)"}, "tokyo")
        specific = event_points({"event_type": "特定日", "event_title": "9がつく日"}, "tokyo")
        self.assertGreater(visitor, specific)
        self.assertGreater(station, specific)
        self.assertGreater(atuhime, specific)
        self.assertGreater(dechau, specific)
        catalog = {
            "来店店": ("tokyo", "来店店"),
            "特定店": ("tokyo", "特定店"),
            "福岡店": ("fukuoka", "福岡店"),
        }
        events = [
            {"date": DATE, "event_type": "取材", "event_title": "演者来店", "hall_names": ["来店店"]},
            {"date": DATE, "event_type": "特定日", "event_title": "9がつく日", "hall_names": ["特定店"]},
            {"date": DATE, "event_type": "特定日", "event_title": "9がつく日", "hall_names": ["福岡店"]},
        ]
        targets = select_chase([], events, date=DATE, areas=AREAS, per_area=2, catalog=catalog)
        self.assertEqual([item.prefecture for item in targets], ["fukuoka", "tokyo", "tokyo"])
        self.assertEqual(targets[1].hall_name, "来店店")
        self.assertEqual(targets[2].hall_name, "特定店")

    def test_slots_follow_japan_time_and_morning_has_no_data_image(self) -> None:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from src.poster.chase import attach_data_image, offhours_reason, scheduled_slot

        jst = ZoneInfo("Asia/Tokyo")
        utc = ZoneInfo("UTC")
        self.assertIsNone(scheduled_slot(datetime(2026, 10, 29, 2, 44, tzinfo=jst)))
        self.assertEqual(scheduled_slot(datetime(2026, 10, 29, 10, 5, tzinfo=jst)), "morning")
        self.assertEqual(scheduled_slot(datetime(2026, 10, 29, 11, 14, tzinfo=jst)), "morning")
        self.assertIsNone(scheduled_slot(datetime(2026, 10, 29, 11, 16, tzinfo=jst)))
        self.assertEqual(scheduled_slot(datetime(2026, 10, 29, 11, 50, tzinfo=jst)), "pickup")
        self.assertIsNone(scheduled_slot(datetime(2026, 10, 29, 12, 40, tzinfo=jst)))
        self.assertEqual(scheduled_slot(datetime(2026, 10, 29, 14, 0, tzinfo=jst)), "midday")
        self.assertEqual(scheduled_slot(datetime(2026, 10, 29, 17, 0, tzinfo=jst)), "afternoon")
        self.assertEqual(scheduled_slot(datetime(2026, 10, 29, 19, 30, tzinfo=jst)), "evening")
        self.assertEqual(scheduled_slot(datetime(2026, 10, 29, 21, 0, tzinfo=jst)), "night")
        self.assertEqual(scheduled_slot(datetime(2026, 10, 29, 1, 5, tzinfo=utc)), "morning")
        self.assertEqual(scheduled_slot(datetime(2026, 10, 29, 10, 0, tzinfo=utc)), "evening")
        night = datetime(2026, 10, 29, 2, 44, tzinfo=jst)
        self.assertIn("停止", offhours_reason("morning", "2026-10-29", night))
        self.assertIn("停止", offhours_reason("auto", "2026-10-28", datetime(2026, 10, 29, 10, 5, tzinfo=jst)))
        self.assertEqual(offhours_reason("morning", "2026-10-29", datetime(2026, 10, 29, 10, 5, tzinfo=jst)), "")
        self.assertFalse(attach_data_image("morning", same_day=True))
        self.assertFalse(attach_data_image("midday", same_day=False))
        self.assertTrue(attach_data_image("midday", same_day=True))
        self.assertTrue(attach_data_image("evening", same_day=True))
        self.assertTrue(attach_data_image("pickup", same_day=True))
        self.assertTrue(attach_data_image("night", same_day=True))
        self.assertFalse(attach_data_image("pickup", same_day=False))

    def test_morning_declaration_does_not_quote_numbers(self) -> None:
        from src.poster.chase import ChaseTarget
        from src.poster.copy import compose_chase, compose_morning, compose_morning_areas, line_reply_for, line_reply_hall
        from src.poster.select import HallPick

        osaka = ChaseTarget("丸一店", "osaka", 50, ("8のつく日",), 1800.0, 0.2, None)
        fukuoka = ChaseTarget("天神店", "fukuoka", 60, ("でちゃう取材(虹)",), 2000.0, 0.1, None)
        text = compose_morning_areas()
        self.assertIn("今日追うエリア。", text)
        self.assertIn("反応した機種とホールを随時投稿していきます。", text)
        self.assertIn("大阪府 全域", text)
        self.assertIn("東京都 全域", text)
        self.assertIn("福岡県 全域", text)
        legacy = compose_morning([fukuoka, osaka])
        self.assertIn("・天神店　でちゃう取材(虹)。", legacy)
        self.assertNotIn("本日の立ち回りやホール選びの参考にどうぞ。", text)
        self.assertNotIn("共有してみてください", text)
        self.assertNotIn("揃って", text)
        self.assertNotIn("不確実", text)
        self.assertNotIn("👇", text)
        self.assertNotIn("http", text)
        self.assertNotIn("！", text)
        self.assertNotIn("平均設定", text)
        self.assertEqual(compose_chase(osaka, "midday"), "")
        self.assertEqual(line_reply_hall("morning", [osaka, fukuoka]), "")
        self.assertEqual(line_reply_for(enabled=False), "")
        self.assertEqual(line_reply_for(enabled=True), "")
        pick = HallPick(
            "数値店",
            "digits",
            "A",
            1,
            10,
            10000,
            "",
            None,
            (),
            False,
            False,
            (),
            (),
            (),
        )
        strong = ChaseTarget("強い店", "tokyo", 100, ("来店",), 900.0, 0.1, pick)
        weak = ChaseTarget("弱い店", "tokyo", 50, ("つく日",), 2000.0, 0.9, pick)
        plain = ChaseTarget("数値なし", "osaka", 200, ("周年",), 3000.0, 0.9, None)
        self.assertEqual(line_reply_hall("evening", [weak, strong, plain]), "強い店")

    def test_morning_card_lists_halls_without_numbers(self) -> None:
        from src.poster.morning_card import morning_area_card_html

        page = morning_area_card_html(date="2026-10-30")
        self.assertIn("今日追うエリア", page)
        self.assertIn("反応した機種とホールを随時投稿していきます。", page)
        self.assertIn("2026年10月30日", page)
        self.assertIn("大阪府 全域", page)
        self.assertIn("東京都 全域", page)
        self.assertIn("福岡県 全域", page)
        self.assertNotIn("天神店", page)
        self.assertNotIn("つく日は末尾", page)
        self.assertNotIn("全台系", page)
        self.assertNotIn("末尾", page)
        self.assertNotIn("2072", page)
        self.assertNotIn("963", page)
        self.assertNotIn("平均", page)


def _load_auto_poster():
    path = ROOT / "scripts" / "auto_poster.py"
    name = "auto_poster_line_notice"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class LineNoticeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.poster = _load_auto_poster()

    def _args(self, *, dry_run: bool = False, discord: bool = False):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: None)
        return SimpleNamespace(dry_run=dry_run, discord=discord, meta=folder / "post.json")

    def _deliver(self, poster, args, ledger: dict | None = None):
        return poster._deliver(
            "本文",
            "",
            None,
            date=DATE,
            slot="morning",
            hall="店",
            args=args,
            ledger={} if ledger is None else ledger,
            ok=True,
            image_ready=False,
            text_only=True,
        )

    def test_dry_run_and_discord_preview_do_not_notify_line(self) -> None:
        poster = self.poster
        with mock.patch.object(poster, "_notify_ops_line") as line:
            dry = self._deliver(poster, self._args(dry_run=True))
            with mock.patch.object(poster, "send_x_preview", return_value=204):
                preview = self._deliver(poster, self._args(discord=True))
        line.assert_not_called()
        self.assertFalse(dry.posted)
        self.assertTrue(preview.previewed)

    def test_successful_publish_sends_the_tweet_url(self) -> None:
        poster = self.poster
        with (
            mock.patch.object(poster, "publish_tweet", return_value={"id": "42"}) as publish,
            mock.patch.object(poster, "send_x_preview", return_value=204),
            mock.patch.object(poster, "_notify_ops_line", return_value="") as line,
            mock.patch.object(poster, "_write_json"),
        ):
            outcome = self._deliver(poster, self._args())
        publish.assert_called_once()
        line.assert_called_once_with("https://x.com/i/web/status/42")
        self.assertTrue(outcome.posted)
        self.assertEqual(outcome.tweet_id, "42")
        self.assertEqual(outcome.error, "")

    def test_already_posted_hall_is_not_notified_again(self) -> None:
        poster = self.poster
        ledger = {DATE: {"morning": {"店": {"tweet_id": "7", "reply_id": ""}}}}
        with mock.patch.object(poster, "_notify_ops_line") as line:
            outcome = self._deliver(poster, self._args(), ledger)
        line.assert_not_called()
        self.assertTrue(outcome.posted)
        self.assertEqual(outcome.tweet_id, "7")

    def test_line_failure_keeps_the_tweet(self) -> None:
        poster = self.poster
        with (
            mock.patch.object(poster, "publish_tweet", return_value={"id": "42"}),
            mock.patch.object(poster, "send_x_preview", return_value=204),
            mock.patch.object(poster, "_notify_ops_line", return_value="運営LINEへの通知に失敗しました。"),
            mock.patch.object(poster, "_write_json"),
        ):
            outcome = self._deliver(poster, self._args())
        self.assertTrue(outcome.posted)
        self.assertEqual(outcome.tweet_id, "42")
        self.assertIn("運営LINEへの通知に失敗しました。", outcome.error)


if __name__ == "__main__":
    unittest.main()
