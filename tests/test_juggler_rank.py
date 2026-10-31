import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from src.poster.juggler_rank import compose_juggler_pickup_post, pick_juggler_closing

JST = ZoneInfo("Asia/Tokyo")


class JugglerPickupCopyTest(unittest.TestCase):
    def test_post_has_human_line_and_no_island_score(self) -> None:
        text = compose_juggler_pickup_post(
            [
                {
                    "hall_name": "テスト店",
                    "family_label": "アイムジャグラーEX",
                    "badge_label": "全6",
                    "synthetic_probability": 111.8,
                    "rb_probability": 245.3,
                    "score": 86,
                    "unit_count": 3,
                }
            ]
        )
        self.assertIn("ツール上反応しているところ", text)
        self.assertIn("合算1/111.8", text)
        self.assertNotIn("島スコア", text)
        self.assertNotIn("候補に入れて", text)
        self.assertNotIn("全部行く", text)

    def test_closing_follows_slot(self) -> None:
        evening = pick_juggler_closing(
            now=datetime(2026, 10, 31, 10, 0, tzinfo=JST),
            slot="evening",
        )
        self.assertIn("ジャグラー", evening)
        self.assertNotIn("絶対", evening)
        afternoon = pick_juggler_closing(
            now=datetime(2026, 10, 31, 14, 0, tzinfo=JST),
            slot="afternoon",
        )
        self.assertIn("随時ピックアップ", afternoon)
        self.assertNotIn("反応店", afternoon)


if __name__ == "__main__":
    unittest.main()
