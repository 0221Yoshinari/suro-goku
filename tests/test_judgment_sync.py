import json
import os
import tempfile
import unittest
from pathlib import Path

from src.dashboard.judgment_sync import assemble_prefecture, store_hall_payload, token_matches


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


if __name__ == "__main__":
    unittest.main()
