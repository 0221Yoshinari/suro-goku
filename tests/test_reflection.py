import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.analyzer.judgment import judge_record, judge_records
from src.dashboard.judgment_sync import assemble_prefecture, store_hall_payload
from src.scraper.reflection import begin_cycle, note_failed, note_reflected, status_line, status_snapshot


class ReflectionStatusTest(unittest.TestCase):
    def test_line_marks_partial_success_as_already_on_screen(self) -> None:
        self.assertEqual(status_line(12, 123), "成功12店舗 / 失敗123店舗（成功分は画面反映済み）")
        self.assertEqual(status_line(4, 0), "成功4店舗 / 失敗0店舗（画面反映済み）")
        self.assertEqual(status_line(0, 3), "成功0店舗 / 失敗3店舗")

    def test_failed_hall_does_not_undo_a_reflected_hall(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["REFLECTION_STATUS_PATH"] = str(Path(tmp) / "status.json")
            try:
                begin_cycle()
                note_reflected("成功店")
                note_failed("成功店")
                note_failed("失敗店")
                snap = status_snapshot()
            finally:
                os.environ.pop("REFLECTION_STATUS_PATH", None)
        self.assertEqual(snap["reflected"], ["成功店"])
        self.assertEqual(snap["failed"], ["失敗店"])
        self.assertEqual(snap["note"], "成功1店舗 / 失敗1店舗（成功分は画面反映済み）")


class PartialJudgmentTest(unittest.TestCase):
    def test_one_broken_record_still_writes_the_hall(self) -> None:
        records = [
            {
                "hall_name": "孤立テスト店",
                "machine_name": "マイジャグラーV",
                "fields": {"machine_name": "マイジャグラーV", "dai_no": "1", "total_games": 5000, "bb_count": 20, "rb_count": 10},
            },
            {
                "hall_name": "孤立テスト店",
                "machine_name": "マイジャグラーV",
                "fields": {"machine_name": "マイジャグラーV", "dai_no": "2", "total_games": 5000, "bb_count": 20, "rb_count": 10},
            },
        ]
        real = judge_record

        def explode(record, masters):
            if str((record.get("fields") or {}).get("dai_no")) == "1":
                raise RuntimeError("この台だけ壊れている")
            return real(record, masters)

        with tempfile.TemporaryDirectory() as tmp:
            with patch("src.analyzer.judgment.judge_record", side_effect=explode):
                summary = judge_records(records, output_dir=Path(tmp), source="test", incremental=True)
            self.assertGreaterEqual(summary["machine_count"], 1)
            hall_files = list((Path(tmp) / "halls").glob("*/latest.json"))
            self.assertEqual(len(hall_files), 1)
            saved = json.loads(hall_files[0].read_text(encoding="utf-8"))
            self.assertEqual(saved["hall_name"], "孤立テスト店")
            self.assertGreaterEqual(len(saved["machines"]), 1)

    def test_broken_digit_summary_keeps_other_hall_machines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store_hall_payload(
                {
                    "hall_name": "玉屋∞本店",
                    "judged_at": "2026-10-19T01:00:00+00:00",
                    "machines": [{"hall_name": "玉屋∞本店", "machine_name": "マイジャグラーV", "total_games": 1000, "units": []}],
                },
                output_dir=root,
            )
            with patch("src.analyzer.extras.hall_digit_summaries", side_effect=RuntimeError("末尾が壊れている")):
                view = assemble_prefecture("fukuoka", root)
            names = [item.get("hall_name") for item in view.get("machines") or []]
            self.assertIn("玉屋∞本店", names)
            self.assertEqual(view.get("hall_digit_summaries"), [])


if __name__ == "__main__":
    unittest.main()
