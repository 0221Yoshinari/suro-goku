import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from src.scraper.halls import Hall, prioritize_halls
from src.scheduler import past_hard_stop


TOKYO = ZoneInfo("Asia/Tokyo")


class CyclePriorityTest(unittest.TestCase):
    def test_major_prefectures_come_first(self) -> None:
        halls = [
            Hall(id="o", name="大阪店", aliases=(), prefecture="osaka"),
            Hall(id="s", name="佐賀店", aliases=(), prefecture="saga"),
            Hall(id="f", name="福岡店", aliases=(), prefecture="fukuoka"),
            Hall(id="t", name="東京店", aliases=(), prefecture="tokyo"),
        ]
        ordered = [hall.prefecture for hall in prioritize_halls(halls)]
        self.assertEqual(ordered, ["fukuoka", "tokyo", "osaka", "saga"])

    def test_hard_stop_is_23_jst(self) -> None:
        self.assertFalse(past_hard_stop(datetime(2026, 10, 17, 22, 59, tzinfo=TOKYO)))
        self.assertTrue(past_hard_stop(datetime(2026, 10, 17, 23, 0, tzinfo=TOKYO)))


if __name__ == "__main__":
    unittest.main()
