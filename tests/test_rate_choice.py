import unittest

from src.scraper.halls import hall_by_name
from src.scraper.teramoba import (
    TeramobaClient,
    choose_rate_kind,
    kind_labels_from_note,
    target_from_hall,
)


class RateChoiceTest(unittest.TestCase):
    def test_twenty_yen_wins_even_when_another_rate_is_higher(self) -> None:
        labels = {"G": "20円", "P": "46枚", "D": "5円"}
        self.assertEqual(choose_rate_kind(["G", "P", "D"], labels), "G")

    def test_without_twenty_yen_the_highest_rate_is_kept(self) -> None:
        labels = kind_labels_from_note("46枚=V / 184枚=X")
        self.assertEqual(choose_rate_kind(["V", "X"], labels), "V")
        labels = kind_labels_from_note("46枚=21 / 160枚=6")
        self.assertEqual(choose_rate_kind(["21", "6"], labels), "21")
        self.assertEqual(choose_rate_kind(["A", "B"], {"A": "10円", "B": "5円"}), "A")

    def test_unreadable_note_keeps_the_primary_kind(self) -> None:
        self.assertEqual(choose_rate_kind(["G", "D"], {}), "G")

    def test_configured_halls_resolve_to_one_rate(self) -> None:
        expected = {
            "ブーム天神": "G",
            "１２３博多店": "V",
        }
        for name, code in expected.items():
            hall = hall_by_name(name)
            self.assertIsNotNone(hall)
            client = TeramobaClient(target_from_hall(hall))
            self.assertEqual(client.resolve_slot_kinds(), [code])
