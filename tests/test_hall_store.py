import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.scraper.hall_store import load_hall_snapshots, persist_fetched_halls


class HallStoreTest(unittest.TestCase):
    def test_sqlite_upserts_only_fetched_halls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            url = "sqlite:///" + str(Path(tmp) / "halls.sqlite")
            judgment = {
                "judged_at": "2026-10-13T12:00:00+00:00",
                "fetched_at": "2026-10-13T11:00:00+00:00",
                "source": "mixed",
                "hall_counts": {
                    "玉屋∞本店": {
                        "count": 100,
                        "machine_count": 12,
                        "all_high_count": 2,
                        "high_56_count": 1,
                        "high_base_count": 1,
                    },
                    "残す店": {"count": 4, "machine_count": 1, "all_high_count": 0, "high_56_count": 0, "high_base_count": 0},
                },
            }
            with mock.patch.dict(os.environ, {"DATABASE_URL": url}, clear=False):
                self.assertEqual(persist_fetched_halls(judgment, ["玉屋∞本店"]), "sqlite")
                self.assertEqual(persist_fetched_halls(judgment, ["残す店"]), "sqlite")
                rows = {row["hall_name"]: row for row in load_hall_snapshots()}
            self.assertEqual(set(rows), {"玉屋∞本店", "残す店"})
            self.assertEqual(rows["玉屋∞本店"]["machine_count"], 12)
            self.assertEqual(rows["玉屋∞本店"]["unit_count"], 100)
            self.assertEqual(rows["玉屋∞本店"]["prefecture"], "fukuoka")

    def test_empty_database_url_skips(self) -> None:
        with mock.patch.dict(os.environ, {"DATABASE_URL": ""}, clear=False):
            self.assertEqual(persist_fetched_halls({"hall_counts": {}}, ["玉屋∞本店"]), "skipped")
            self.assertEqual(load_hall_snapshots(), [])
