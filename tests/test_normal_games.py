import unittest

from src.analyzer.judgment import judge_record, load_masters
from src.analyzer.normal_games import resolve_real_normal_games


class RealNormalGamesTest(unittest.TestCase):
    def test_tamaya_keeps_reported_total(self) -> None:
        resolved = resolve_real_normal_games(
            2000,
            includes_art_in_total_games=False,
            art_count=4,
            hits=[{"kind": "ART", "games": 300}, {"kind": "ART", "games": 180}],
        )
        self.assertEqual(resolved["real_normal_games"], 2000)
        self.assertEqual(resolved["art_consumed_games"], 0)
        self.assertTrue(resolved["normal_games_exact"])

    def test_plaza_subtracts_each_art_signal(self) -> None:
        resolved = resolve_real_normal_games(
            2000,
            includes_art_in_total_games=True,
            art_count=2,
            hits=[
                {"day": "2026-09-26", "kind": "ART", "games": 300},
                {"day": "2026-09-26", "kind": "AT", "games": 180},
                {"day": "2026-09-26", "kind": "BB", "games": 40},
            ],
        )
        self.assertEqual(resolved["art_consumed_games"], 480)
        self.assertEqual(resolved["real_normal_games"], 1520)

    def test_uses_start_end_difference_not_an_average(self) -> None:
        resolved = resolve_real_normal_games(
            1000,
            includes_art_in_total_games=True,
            art_count=1,
            hits=[{"kind": "ART", "start_game": 400, "end_game": 650}],
        )
        self.assertEqual(resolved["art_consumed_games"], 250)
        self.assertEqual(resolved["real_normal_games"], 750)

    def test_ignores_previous_days(self) -> None:
        resolved = resolve_real_normal_games(
            900,
            includes_art_in_total_games=True,
            art_count=1,
            history=[
                {"day": "2026-09-26", "art": 1, "hits": [{"kind": "ART", "games": 120}]},
                {"day": "2026-09-25", "art": 3, "hits": [{"kind": "ART", "games": 800}]},
            ],
        )
        self.assertEqual(resolved["art_consumed_games"], 120)
        self.assertEqual(resolved["real_normal_games"], 780)

    def test_count_is_not_consumed_games(self) -> None:
        resolved = resolve_real_normal_games(
            1654,
            includes_art_in_total_games=True,
            art_count=7,
            history=[{"day": "2026-09-26", "bb": 9, "rb": None, "art": None}],
        )
        self.assertIsNone(resolved["art_consumed_games"])
        self.assertIsNone(resolved["real_normal_games"])
        self.assertFalse(resolved["normal_games_exact"])

    def test_zero_art_needs_no_subtraction(self) -> None:
        resolved = resolve_real_normal_games(
            930,
            includes_art_in_total_games=True,
            art_count=0,
            history=[{"day": "2026-09-26", "bb": 0, "rb": None, "art": None}],
        )
        self.assertEqual(resolved["art_consumed_games"], 0)
        self.assertEqual(resolved["real_normal_games"], 930)

    def test_ghoul_rb_uses_real_normal_games(self) -> None:
        masters = load_masters()
        judged = judge_record(
            {
                "hall_name": "プラザ 本店",
                "fields": {
                    "machine_name": "L 東京喰種",
                    "hall_name": "プラザ 本店",
                    "total_games": 2000,
                    "bb_count": 0,
                    "rb_count": 4,
                    "art_count": 2,
                    "includes_art_in_total_games": True,
                    "hits": [
                        {"kind": "ART", "games": 300},
                        {"kind": "ART", "games": 180},
                    ],
                },
            },
            masters,
        )
        self.assertEqual(judged["art_consumed_games"], 480)
        self.assertEqual(judged["real_normal_games"], 1520)
        self.assertEqual(judged["total_games"], 1520)
        self.assertEqual(judged["first_hit_count"], 4)
        self.assertEqual(judged["rb_probability"], round(1520 / 4, 4))
        self.assertEqual(judged["synthetic_probability"], round(1520 / 4, 4))


if __name__ == "__main__":
    unittest.main()
