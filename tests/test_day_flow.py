"""day_flow の投稿候補選び。"""

from __future__ import annotations

import unittest

from src.poster.day_flow import area_targets, best_material, watch_targets


def _hall(name: str, *, judged_at: str, digits: list[dict] | None = None, machines=None):
    return {
        "hall_name": name,
        "judged_at": judged_at,
        "machines": machines or [],
        "digits": digits or [],
    }


class DayFlowDigitTests(unittest.TestCase):
    def test_soft_digit_without_game_floor(self) -> None:
        events = [
            {
                "date": "2026-10-31",
                "event_title": "取材",
                "hall_names": ["テスト店"],
            }
        ]
        targets = watch_targets(events, "2026-10-31")
        halls = [
            _hall(
                "テスト店",
                judged_at="2026-10-31T11:00:00+09:00",
                digits=[
                    {"digit": 0, "avg_setting": 3.05, "unit_count": 8, "total_games": 120, "status_label": "通常"},
                    {"digit": 1, "avg_setting": 2.90, "unit_count": 7, "total_games": 100, "status_label": "通常"},
                ],
            )
        ]
        pick = best_material(halls, targets, "2026-10-31")
        self.assertIsNotNone(pick)
        assert pick is not None
        self.assertEqual(pick.pattern, "digits")
        self.assertIn("soft_digit", pick.tags)
        self.assertEqual(pick.digits[0].digit, 0)

    def test_strong_digit_still_wins(self) -> None:
        events = [
            {
                "date": "2026-10-31",
                "event_title": "取材",
                "hall_names": ["テスト店"],
            }
        ]
        targets = watch_targets(events, "2026-10-31")
        halls = [
            _hall(
                "テスト店",
                judged_at="2026-10-31T11:00:00+09:00",
                digits=[
                    {"digit": 3, "avg_setting": 4.80, "unit_count": 10, "total_games": 500, "status_label": "全56数値"},
                ],
            )
        ]
        pick = best_material(halls, targets, "2026-10-31")
        self.assertIsNotNone(pick)
        assert pick is not None
        self.assertNotIn("soft_digit", pick.tags)


    def test_area_targets_covers_three_prefectures(self) -> None:
        rows = area_targets([], "2026-10-31")
        prefs = {row.prefecture for row in rows}
        self.assertIn("osaka", prefs)
        self.assertIn("tokyo", prefs)
        self.assertIn("fukuoka", prefs)
        self.assertGreater(len(rows), 100)


if __name__ == "__main__":
    unittest.main()
