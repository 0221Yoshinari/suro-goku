import unittest
from typing import Optional

from src.analyzer.extras import summarize_digit_units


def _unit(dai: str, level: Optional[int], games: int = 1000) -> dict:
    if level is None:
        checks = {"meets_setting_6": None, "meets_setting_5": None, "meets_setting_4": None}
        status = "INCOMPLETE"
    elif level >= 4:
        checks = {
            "meets_setting_6": level == 6,
            "meets_setting_5": level >= 5,
            "meets_setting_4": True,
        }
        status = "ALL_HIGH" if level == 6 else "HIGH_BASE"
    else:
        checks = {"meets_setting_6": False, "meets_setting_5": False, "meets_setting_4": False}
        status = "NOT_HIGH"
    return {
        "dai_no": dai,
        "judgment_type": "A",
        "status": status,
        "total_games": games,
        "real_normal_games": games,
        "checks": checks,
    }


class HallDigitAverageTest(unittest.TestCase):
    def test_sparse_rated_units_still_show_the_average(self) -> None:
        units = [_unit("101", 6, 200)] + [_unit(f"1{index:02d}", None, 1000) for index in range(2, 21)]
        row = summarize_digit_units(units, 1)
        self.assertEqual(row["avg_setting"], 6.0)
        self.assertEqual(row["status_label"], "全6数値")
        self.assertNotEqual(row["status_label"], "データ不足")
        self.assertEqual(row["unit_count"], 20)
        self.assertEqual(row["rated_count"], 1)

    def test_stored_estimate_without_checks_is_averaged_to_two_decimals(self) -> None:
        units = []
        for index, level in enumerate([0, 0, 6, 4, 2]):
            units.append(
                {
                    "dai_no": f"30{index}",
                    "status": "NOT_HIGH",
                    "total_games": 1000,
                    "real_normal_games": 1000,
                    "estimated_setting": level,
                }
            )
        row = summarize_digit_units(units, 3)
        self.assertEqual(row["avg_setting"], 3.2)
        self.assertEqual(f"{row['avg_setting']:.2f}", "3.20")
        self.assertNotEqual(row["status_label"], "データ不足")
        self.assertEqual(row["rated_count"], 5)

    def test_representative_sample_keeps_the_average(self) -> None:
        units = [_unit(f"20{index}", 6 if index < 8 else 0, 1000) for index in range(10)]
        row = summarize_digit_units(units, 0)
        self.assertEqual(row["rated_count"], 10)
        self.assertEqual(row["avg_setting"], 5.2)
        self.assertTrue(row["is_hit"])
        self.assertEqual(row["status_label"], "全56数値")
