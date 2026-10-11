import unittest

from src.analyzer.judgment import combine_judgments


def _unit(hall: str, dai: str, games: int = 1000) -> dict:
    return {
        "hall_name": hall,
        "dai_no": dai,
        "total_games": games,
        "real_normal_games": games,
        "judgment_type": "AT",
        "estimated_setting": 6,
        "status": "ALL_HIGH",
    }


def _machine(hall: str) -> dict:
    return {
        "hall_name": hall,
        "machine_name": "機種",
        "unit_count": 1,
        "is_all_high": True,
        "is_high_56": False,
        "is_high_base": False,
        "is_early_alert": False,
        "status": "ALL_HIGH",
        "digit_judgments": [],
    }


class JudgmentMergeTest(unittest.TestCase):
    def test_partial_update_keeps_other_hall_digits(self) -> None:
        base = {
            "machines": [_machine("Ａ店"), _machine("Ｂ店")],
            "results": [_unit("Ａ店", "1"), _unit("Ｂ店", "12")],
            "halls": ["Ａ店", "Ｂ店"],
        }
        update = {
            "judged_at": "2026-10-06T00:00:00+00:00",
            "fetched_at": "2026-10-06T00:00:00+00:00",
            "source": "site7",
            "machines": [_machine("Ａ店")],
            "results": [_unit("Ａ店", "21", 2000)],
            "halls": ["Ａ店"],
        }
        merged = combine_judgments(base, update)
        names = {row["hall_name"] for row in merged["results"]}
        self.assertEqual(names, {"Ａ店", "Ｂ店"})
        boards = {board["hall_name"]: board for board in merged["hall_digit_summaries"]}
        self.assertEqual(set(boards), {"Ａ店", "Ｂ店"})
        self.assertEqual(boards["Ａ店"]["digits"][1]["total_games"], 2000)
        self.assertGreater(boards["Ｂ店"]["unit_count"], 0)


if __name__ == "__main__":
    unittest.main()
