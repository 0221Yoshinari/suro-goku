import unittest

from src.dashboard.app import assemble_hall_judgments


class ScopedJudgmentTest(unittest.TestCase):
    def test_numbers_come_from_each_hall_file(self) -> None:
        payload = assemble_hall_judgments(
            [
                {
                    "hall_name": "テスト店",
                    "judged_at": "2026-10-17T06:00:00+00:00",
                    "fetched_at": "2026-10-17T05:00:00+00:00",
                    "machines": [
                        {
                            "machine_name": "マイジャグラーV",
                            "hall_name": "テスト店",
                            "judgment_type": "A",
                            "status": "ALL_HIGH",
                            "is_all_high": True,
                            "unit_count": 1,
                            "total_games": 8000,
                            "synthetic_probability": 140.2,
                            "units": [
                                {
                                    "dai_no": "120",
                                    "real_normal_games": 8000,
                                    "bb_count": 40,
                                    "rb_count": 17,
                                    "estimated_setting": 6,
                                    "judgment_type": "A",
                                }
                            ],
                        }
                    ],
                }
            ]
        )
        self.assertEqual(payload["machine_count"], 1)
        machine = payload["machines"][0]
        self.assertEqual(machine["total_games"], 8000)
        self.assertEqual(machine["synthetic_probability"], 140.2)
        self.assertEqual(payload["hall_digit_summaries"][0]["hall_name"], "テスト店")
        self.assertEqual(payload["hall_digit_summaries"][0]["unit_count"], 1)

    def test_prefecture_slice_excludes_other_prefectures(self) -> None:
        from src.dashboard.app import scoped_hall_judgment_paths

        saga = {path.parent.name for path in scoped_hall_judgment_paths("saga")}
        fukuoka = {path.parent.name for path in scoped_hall_judgment_paths("fukuoka")}
        self.assertIn("tamaya-karatsu", saga)
        self.assertNotIn("tamaya-honten", saga)
        self.assertIn("tamaya-honten", fukuoka)
        self.assertNotIn("tamaya-karatsu", fukuoka)


if __name__ == "__main__":
    unittest.main()
