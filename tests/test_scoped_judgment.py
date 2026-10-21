import unittest

from src.dashboard.judgment_sync import _hall_index, build_hall_parts


class ScopedJudgmentTest(unittest.TestCase):
    def test_numbers_come_from_each_hall_file(self) -> None:
        summary, board = build_hall_parts(
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
                                "hall_name": "テスト店",
                            }
                        ],
                    }
                ],
            }
        )
        self.assertEqual(summary["machine_count"], 1)
        machine = board["machines"][0]
        self.assertEqual(machine["total_games"], 8000)
        self.assertEqual(machine["synthetic_probability"], 140.2)
        self.assertNotIn("units", machine)
        self.assertEqual(summary["hall_digit_summaries"][0]["hall_name"], "テスト店")
        self.assertEqual(summary["hall_digit_summaries"][0]["unit_count"], 1)

    def test_prefecture_slice_excludes_other_prefectures(self) -> None:
        by_slug: dict[str, str] = {}
        for _name, (slug, prefecture) in _hall_index().items():
            by_slug[slug] = prefecture
        self.assertEqual(by_slug["tamaya-karatsu"], "saga")
        self.assertEqual(by_slug["tamaya-honten"], "fukuoka")


if __name__ == "__main__":
    unittest.main()
