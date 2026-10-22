import json
import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from src.dashboard import judgment_store
from src.dashboard.judgment_sync import (
    _digit_boards,
    assemble_opening,
    assemble_prefecture,
    build_hall_parts,
    store_hall_payload,
    token_matches,
)


class JudgmentSyncTest(unittest.TestCase):
    def test_store_drops_duplicate_rows_and_checks_token(self) -> None:
        os.environ["JUDGMENT_SYNC_TOKEN"] = "sync-test-token"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = store_hall_payload(
                {
                    "hall_name": "テスト店",
                    "judged_at": "2026-10-17T23:00:00+09:00",
                    "machines": [{"hall_name": "テスト店", "total_games": 1200, "units": []}],
                    "results": [{"dai_no": "1"}],
                },
                output_dir=root,
            )
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("results", saved)
            self.assertEqual(saved["machines"][0]["total_games"], 1200)
        self.assertTrue(token_matches("Bearer sync-test-token"))
        self.assertFalse(token_matches("nope"))

    def test_assemble_uses_only_requested_prefecture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store_hall_payload(
                {"hall_name": "玉屋∞本店", "machines": [{"hall_name": "玉屋∞本店", "total_games": 10, "units": []}]},
                output_dir=root,
            )
            view = assemble_prefecture("fukuoka", root)
            names = [item.get("hall_name") for item in view.get("machines") or []]
            self.assertIn("玉屋∞本店", names)
            self.assertEqual(view["machine_count"], len(names))

    def test_disk_wipe_falls_back_to_durable_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stored = {
                "hall_name": "MJアリーナ干隈店",
                "prefecture": "fukuoka",
                "fetched_at": "2026-10-19T14:00:00+09:00",
                "machines": [{"hall_name": "MJアリーナ干隈店", "total_games": 800, "units": []}],
            }
            with unittest.mock.patch(
                "src.dashboard.judgment_store.load_durable",
                return_value=[stored],
            ):
                view = assemble_prefecture("fukuoka", root)
            names = [item.get("hall_name") for item in view.get("machines") or []]
            self.assertEqual(names, ["MJアリーナ干隈店"])
            self.assertEqual(view["machine_count"], 1)
            self.assertNotIn("プラザ 博多", view.get("halls") or [])

    def test_prefecture_index_covers_hall_lookup(self) -> None:
        sql = "\n".join(judgment_store._SCHEMA_SQL)
        self.assertIn("hall_judgments_prefecture_hall_idx", sql)
        self.assertIn("(prefecture, hall_name)", sql)
        self.assertIn("ADD COLUMN IF NOT EXISTS summary", sql)
        self.assertIn("ADD COLUMN IF NOT EXISTS board", sql)

    def test_durable_cache_skips_another_database_read(self) -> None:
        judgment_store.clear_durable_cache()
        judgment_store._remember_prefecture("fukuoka", [{"hall_name": "A店", "machine_count": 1}])
        try:
            with unittest.mock.patch.object(judgment_store, "_postgres_url", return_value="postgres://db"):
                with unittest.mock.patch.object(judgment_store, "_engine", side_effect=AssertionError("queried")):
                    rows = judgment_store.load_durable("fukuoka")
            self.assertEqual(rows[0]["hall_name"], "A店")
            judgment_store._cache_replace("fukuoka", {"hall_name": "A店", "machine_count": 4})
            judgment_store._cache_replace("fukuoka", {"hall_name": "B店", "machine_count": 2})
            cached = judgment_store._cached_prefecture("fukuoka")
            self.assertEqual(
                [(row["hall_name"], row["machine_count"]) for row in cached],
                [("A店", 4), ("B店", 2)],
            )
        finally:
            judgment_store.clear_durable_cache()

    def test_digit_summary_is_reused_for_the_same_hall(self) -> None:
        payload = {
            "hall_name": "テスト店",
            "machines": [
                {
                    "hall_name": "テスト店",
                    "judgment_type": "A",
                    "units": [
                        {
                            "hall_name": "テスト店",
                            "judgment_type": "A",
                            "dai_no": "10",
                            "total_games": 3000,
                            "bb_count": 12,
                            "rb_count": 8,
                            "estimated_setting": 6,
                        }
                    ],
                }
            ],
        }
        first = _digit_boards(payload, "テスト店", payload["machines"])
        self.assertEqual(first[0]["hall_name"], "テスト店")
        self.assertEqual(len(first[0]["digits"]), 10)
        with unittest.mock.patch(
            "src.analyzer.extras.hall_digit_summaries",
            side_effect=AssertionError("recomputed"),
        ):
            second = _digit_boards(payload, "テスト店", payload["machines"])
        self.assertEqual(second, first)

    def test_prefecture_view_is_reused_until_the_ttl(self) -> None:
        from src.dashboard.app import clear_judgment_view_cache, get_judgments

        clear_judgment_view_cache()
        synced = {
            "machines": [{"hall_name": "玉屋∞本店"}],
            "halls": ["玉屋∞本店"],
            "hall_digit_summaries": [],
            "machine_count": 1,
            "unit_count": 0,
            "count": 1,
            "judged_at": "2026-10-19T00:00:00+00:00",
        }
        try:
            with unittest.mock.patch("src.dashboard.judgment_sync.assemble_prefecture", return_value=dict(synced)) as assembled:
                with unittest.mock.patch("src.dashboard.judgment_sync.read_reflection_status", return_value={}):
                    first = get_judgments("fukuoka")
                    second = get_judgments("fukuoka")
            self.assertEqual(assembled.call_count, 1)
            self.assertEqual(first["machine_count"], 1)
            self.assertEqual(second["machine_count"], 1)
            self.assertNotIn("units", (first.get("machines") or [{}])[0])
        finally:
            clear_judgment_view_cache()

    def test_public_view_drops_unit_rows_without_forgetting_them(self) -> None:
        from src.dashboard.app import juggler_units_from, machine_units_from, public_judgment_view

        payload = {
            "machines": [
                {
                    "hall_name": "テスト店",
                    "machine_name": "マイジャグラーV",
                    "unit_count": 1,
                    "units": [{"dai_no": "10"}],
                }
            ],
            "juggler": {
                "islands": [
                    {
                        "hall_name": "テスト店",
                        "family_id": "my",
                        "unit_count": 1,
                        "units": [{"dai_no": "10"}],
                    }
                ]
            },
        }
        view = public_judgment_view(payload)
        self.assertNotIn("units", view["machines"][0])
        self.assertNotIn("units", view["juggler"]["islands"][0])
        self.assertEqual(view["machines"][0]["unit_count"], 1)
        self.assertEqual(machine_units_from(payload, "テスト店", "マイジャグラーV")[0]["dai_no"], "10")
        self.assertEqual(juggler_units_from(payload, "テスト店", "my")[0]["dai_no"], "10")
        self.assertIn("units", payload["machines"][0])

    def test_hall_parts_keep_unit_rows_out_of_the_summary(self) -> None:
        payload = {
            "hall_name": "テスト店",
            "prefecture": "fukuoka",
            "judged_at": "2026-10-22T09:00:00+09:00",
            "machines": [
                {
                    "hall_name": "テスト店",
                    "machine_name": "全台機",
                    "status": "NOT_HIGH",
                    "checks": {"meets_setting_6": True, "early_enough": True},
                    "synthetic_probability": 120,
                    "units": [{"dai_no": "10", "hall_name": "テスト店", "judgment_type": "A"}],
                }
            ],
        }
        summary, board = build_hall_parts(payload)
        self.assertEqual(summary["all_high_count"], 1)
        self.assertEqual(summary["machine_count"], 1)
        self.assertNotIn("units", summary["featured"][0])
        self.assertEqual(summary["featured"][0]["machine_name"], "全台機")
        self.assertEqual(summary["featured"][0]["unit_count"], 1)
        self.assertNotIn("units", board["machines"][0])
        self.assertEqual(board["machines"][0]["unit_count"], 1)
        self.assertEqual(payload["machines"][0]["units"][0]["dai_no"], "10")

    def test_opening_view_lists_featured_halls_without_machines(self) -> None:
        view = assemble_opening(
            [
                {
                    "hall_name": "テスト店",
                    "machine_count": 8,
                    "unit_count": 40,
                    "all_high_count": 1,
                    "high_56_count": 0,
                    "high_base_count": 0,
                    "judged_at": "2026-10-22T09:00:00+09:00",
                    "featured": [{"hall_name": "テスト店", "machine_name": "全台機"}],
                    "hall_digit_summaries": [{"hall_name": "テスト店", "digits": []}],
                    "juggler_islands": [],
                }
            ],
            pending=False,
            stamp="10:1:1",
            hall_count=1,
        )
        self.assertEqual(view["machines"], [])
        self.assertEqual(view["featured_machines"][0]["machine_name"], "全台機")
        self.assertEqual(view["machine_count"], 8)
        self.assertEqual(view["reported_halls"], ["テスト店"])
        self.assertFalse(view["summary_pending"])

    def test_hall_cards_keep_every_store_in_the_list(self) -> None:
        from src.dashboard.judgment_sync import summaries_from_hall_cards

        rows = summaries_from_hall_cards(
            [
                {
                    "hall_name": "GOGOアリーナ天神",
                    "machine_count": 2,
                    "unit_count": 10,
                    "machines": [
                        {"hall_name": "GOGOアリーナ天神", "machine_name": "化物語", "status": "ALL_HIGH", "checks": {"meets_setting_6": True}},
                    ],
                },
                {
                    "hall_name": "玉屋409雑餉隈",
                    "machine_count": 3,
                    "unit_count": 12,
                    "machines": [
                        {"hall_name": "玉屋409雑餉隈", "machine_name": "モンキーターン", "status": "HIGH_BASE", "checks": {"meets_setting_5": False}},
                    ],
                },
            ]
        )
        self.assertEqual([row["hall_name"] for row in rows], ["GOGOアリーナ天神", "玉屋409雑餉隈"])
        self.assertEqual(rows[0]["all_high_count"], 1)
        self.assertEqual(rows[1]["featured"][0]["machine_name"], "モンキーターン")

    def test_summary_does_not_assemble_the_prefecture(self) -> None:
        from src.dashboard.app import clear_judgment_view_cache, get_judgment_summary

        clear_judgment_view_cache()
        coverage = {"halls": 1, "ready": 1, "updated_at": 10}
        rows = [
            {
                "hall_name": "テスト店",
                "machine_count": 4,
                "unit_count": 12,
                "all_high_count": 1,
                "high_56_count": 0,
                "high_base_count": 0,
                "judged_at": "2026-10-22T09:00:00+09:00",
                "featured": [
                    {
                        "hall_name": "テスト店",
                        "machine_name": "全台機",
                        "status": "ALL_HIGH",
                        "checks": {"meets_setting_6": True},
                    }
                ],
                "hall_digit_summaries": [],
                "juggler_islands": [],
            }
        ]
        try:
            with unittest.mock.patch("src.dashboard.judgment_store.durable_enabled", return_value=True):
                with unittest.mock.patch("src.dashboard.judgment_store.summary_coverage", return_value=coverage):
                    with unittest.mock.patch("src.dashboard.judgment_store.load_opening_summaries", return_value=rows) as loaded:
                        with unittest.mock.patch(
                            "src.dashboard.judgment_sync.assemble_prefecture",
                            side_effect=AssertionError("assembled"),
                        ):
                            with unittest.mock.patch("src.dashboard.judgment_sync.read_reflection_status", return_value={}):
                                first = get_judgment_summary("fukuoka")
                                second = get_judgment_summary("fukuoka")
            self.assertEqual(loaded.call_count, 1)
            self.assertTrue(first["opening"])
            self.assertEqual(first["machines"], [])
            self.assertEqual(first["featured_machines"][0]["machine_name"], "全台機")
            self.assertEqual(first["machine_count"], 4)
            self.assertEqual(second["summary_stamp"], first["summary_stamp"])
            self.assertEqual(first["summary_stamp"], "10:0:1:1")
        finally:
            clear_judgment_view_cache()

    def test_same_second_hall_update_changes_the_opening_stamp(self) -> None:
        from src.dashboard.app import _opening_cache_key

        before = _opening_cache_key({"updated_at": 100, "updated_sum": 1000, "ready": 2, "halls": 2})
        after = _opening_cache_key({"updated_at": 100, "updated_sum": 1050, "ready": 2, "halls": 2})
        self.assertNotEqual(before, after)
        self.assertEqual(after, "100:1050:2:2")

    def test_machine_units_read_one_hall(self) -> None:
        from src.dashboard.app import get_machine_units

        payload = {
            "hall_name": "テスト店",
            "machines": [
                {
                    "hall_name": "テスト店",
                    "machine_name": "全台機",
                    "units": [{"dai_no": "10"}],
                }
            ],
        }
        with unittest.mock.patch("src.dashboard.judgment_sync.read_hall_payload", return_value=payload) as read:
            body = get_machine_units("テスト店", "全台機", "fukuoka")
        read.assert_called_once_with("テスト店")
        self.assertEqual(body["units"][0]["dai_no"], "10")
        self.assertEqual(body["count"], 1)


if __name__ == "__main__":
    unittest.main()
