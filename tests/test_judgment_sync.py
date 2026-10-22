import json
import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from src.dashboard import judgment_store
from src.dashboard.judgment_sync import _digit_boards, assemble_prefecture, store_hall_payload, token_matches


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


if __name__ == "__main__":
    unittest.main()
