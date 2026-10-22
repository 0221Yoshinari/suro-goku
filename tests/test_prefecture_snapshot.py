import json
import unittest
import unittest.mock

from fastapi.responses import Response

from src.dashboard import judgment_store
from src.dashboard.app import get_judgments, get_machine_units
from src.dashboard.prefecture_snapshot import _build


class PrefectureSnapshotTest(unittest.TestCase):
    def test_schema_keeps_a_prebuilt_prefecture_row(self) -> None:
        sql = "\n".join(judgment_store._SCHEMA_SQL)
        self.assertIn("CREATE TABLE IF NOT EXISTS prefecture_snapshots", sql)
        self.assertIn("payload TEXT NOT NULL", sql)
        self.assertIn("events_updated_at", sql)

    def test_open_reads_the_snapshot_without_assembling(self) -> None:
        raw = json.dumps(
            {
                "machines": [{"hall_name": "玉屋∞本店", "unit_count": 3}],
                "machine_count": 1,
                "snapshot_status": "ready",
            },
            ensure_ascii=False,
        )
        with unittest.mock.patch(
            "src.dashboard.prefecture_snapshot.served_snapshot",
            return_value=raw,
        ):
            with unittest.mock.patch(
                "src.dashboard.judgment_sync.assemble_prefecture",
                side_effect=AssertionError("assembled"),
            ):
                response = get_judgments("fukuoka")
        self.assertIsInstance(response, Response)
        self.assertEqual(response.headers["X-Snapshot"], "hit")
        body = json.loads(response.body)
        self.assertEqual(body["machine_count"], 1)
        self.assertNotIn("units", body["machines"][0])

    def test_open_does_not_assemble_while_the_snapshot_is_missing(self) -> None:
        with unittest.mock.patch("src.dashboard.prefecture_snapshot.served_snapshot", return_value=None):
            with unittest.mock.patch("src.dashboard.judgment_store.durable_enabled", return_value=True):
                with unittest.mock.patch("src.dashboard.prefecture_snapshot.schedule") as schedule:
                    with unittest.mock.patch(
                        "src.dashboard.judgment_sync.assemble_prefecture",
                        side_effect=AssertionError("assembled"),
                    ):
                        body = get_judgments("tokyo")
        schedule.assert_called_once_with("tokyo", delay=0)
        self.assertEqual(body["snapshot_status"], "building")
        self.assertEqual(body["machines"], [])

    def test_machine_units_read_one_hall(self) -> None:
        hall = {
            "hall_name": "テスト店",
            "machines": [
                {
                    "hall_name": "テスト店",
                    "machine_name": "マイジャグラーV",
                    "units": [{"dai_no": "10"}],
                }
            ],
        }
        with unittest.mock.patch("src.dashboard.judgment_store.load_hall_payload", return_value=hall):
            with unittest.mock.patch(
                "src.dashboard.judgment_sync.assemble_prefecture",
                side_effect=AssertionError("assembled"),
            ):
                body = get_machine_units(hall="テスト店", machine="マイジャグラーV", prefecture="fukuoka")
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["units"][0]["dai_no"], "10")

    def test_build_stores_the_public_view_without_unit_rows(self) -> None:
        synced = {
            "machines": [
                {
                    "hall_name": "テスト店",
                    "machine_name": "マイジャグラーV",
                    "unit_count": 1,
                    "units": [{"dai_no": "10", "hall_name": "テスト店"}],
                }
            ],
            "halls": ["テスト店"],
            "machine_count": 1,
            "unit_count": 1,
            "count": 1,
            "judged_at": "2026-10-23T00:00:00+09:00",
        }
        saved: dict = {}

        def capture(prefecture: str, **kwargs):
            saved["prefecture"] = prefecture
            saved.update(kwargs)
            return True

        with unittest.mock.patch("src.dashboard.judgment_sync.assemble_prefecture", return_value=synced):
            with unittest.mock.patch("src.dashboard.judgment_store.hall_source_stamps", return_value={"fukuoka": "1:10"}):
                with unittest.mock.patch("src.dashboard.judgment_store.drop_durable_prefecture"):
                    with unittest.mock.patch("src.dashboard.judgment_store.save_prefecture_snapshot", side_effect=capture):
                        _build("fukuoka")
        self.assertEqual(saved["prefecture"], "fukuoka")
        self.assertEqual(saved["source_stamp"], "1:10")
        self.assertGreaterEqual(saved["build_ms"], 0)
        view = json.loads(saved["payload"])
        self.assertEqual(view["snapshot_status"], "ready")
        self.assertEqual(view["machine_count"], 1)
        self.assertNotIn("units", view["machines"][0])
        self.assertEqual(view["machines"][0]["unit_count"], 1)
        self.assertTrue(view["lineup_ready"])


if __name__ == "__main__":
    unittest.main()
