from __future__ import annotations

import unittest

from src.analyzer.judgment import aggregate_machine_groups, judge_record, load_masters
from src.analyzer.lineup import attach_lineup_blocks, find_lineup_blocks


class LineupBlocksTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.masters = load_masters()

    def _at(self, dai: str, games: int, rb: int, diff: int | None = None) -> dict:
        fields = {
            "machine_name": "LモンハンライズXA",
            "dai_no": dai,
            "total_games": games,
            "bb_count": 0,
            "rb_count": rb,
            "art_count": 1,
        }
        if diff is not None:
            fields["diff_medals"] = diff
        return judge_record({"hall_name": "テスト店", "fields": fields}, self.masters)

    def _juggler(self, dai: str, games: int, bb: int, rb: int) -> dict:
        return judge_record(
            {
                "hall_name": "テスト店",
                "fields": {
                    "machine_name": "ジャグラーガールズSS",
                    "dai_no": dai,
                    "total_games": games,
                    "bb_count": bb,
                    "rb_count": rb,
                },
            },
            self.masters,
        )

    def _machine(self, records: list[dict]) -> dict:
        return aggregate_machine_groups(records)[0]

    def test_three_hot_units_are_one_block_without_inner_pairs(self) -> None:
        machine = self._machine(
            [
                self._at("701", 4000, 20, diff=900),
                self._at("702", 4000, 20, diff=300),
                self._at("703", 4000, 20, diff=600),
                self._at("710", 4000, 8, diff=-1000),
            ]
        )
        blocks = find_lineup_blocks(machine)
        self.assertEqual([block["title"] for block in blocks], ["701号機〜703号機【3台並び】"])
        block = blocks[0]
        self.assertEqual(block["length"], 3)
        self.assertEqual(block["status"], "ALL_HIGH")
        self.assertAlmostEqual(block["avg_first_hit_probability"], round(12000 / 60, 4))
        self.assertEqual(block["avg_games"], 4000.0)
        self.assertEqual(block["avg_diff_medals"], 600)
        self.assertEqual(
            [row["dai_no"] for row in block["units"]],
            ["701", "702", "703"],
        )
        self.assertEqual(block["units"][0]["first_hit_count"], 20)
        self.assertEqual(block["units"][0]["total_games"], 4000)
        self.assertEqual(block["units"][0]["diff_medals"], 900)

    def test_pair_stays_when_the_longer_run_misses_the_bar(self) -> None:
        machine = self._machine(
            [
                self._at("701", 4000, 20),
                self._at("702", 4000, 20),
                self._at("703", 4000, 4),
            ]
        )
        blocks = find_lineup_blocks(machine)
        self.assertEqual([block["title"] for block in blocks], ["701号機〜702号機【2台並び】"])
        self.assertEqual(blocks[0]["status"], "ALL_HIGH")
        self.assertAlmostEqual(blocks[0]["avg_first_hit_probability"], round(8000 / 40, 4))

    def test_overlapping_pairs_both_show_when_the_trio_is_cold(self) -> None:
        machine = self._machine(
            [
                self._at("11", 5000, 10),
                self._at("12", 1000, 20),
                self._at("13", 5000, 10),
            ]
        )
        blocks = find_lineup_blocks(machine)
        self.assertEqual(
            [block["title"] for block in blocks],
            ["11号機〜12号機【2台並び】", "12号機〜13号機【2台並び】"],
        )

    def test_gap_and_excluded_unit_do_not_join(self) -> None:
        gapped = self._machine([self._at("701", 4000, 20), self._at("703", 4000, 20)])
        self.assertEqual(find_lineup_blocks(gapped), [])

        machine = self._machine(
            [
                self._at("1", 4000, 20),
                self._at("2", 4000, 20),
                self._at("3", 4000, 20),
            ]
        )
        machine["units"][1]["judgment_excluded"] = True
        machine["units"][1]["status"] = "EXCLUDED"
        self.assertEqual(find_lineup_blocks(machine), [])

    def test_cold_pair_is_omitted(self) -> None:
        machine = self._machine([self._at("1", 4000, 8), self._at("2", 4000, 8)])
        self.assertEqual(find_lineup_blocks(machine), [])

    def test_a_type_pair_needs_reg_as_well_as_combined(self) -> None:
        weak_reg = self._machine(
            [
                self._juggler("5", 4000, 40, 8),
                self._juggler("6", 4000, 40, 8),
            ]
        )
        self.assertEqual(find_lineup_blocks(weak_reg), [])

        strong = self._machine(
            [
                self._juggler("5", 4000, 20, 20),
                self._juggler("6", 4000, 20, 20),
            ]
        )
        blocks = find_lineup_blocks(strong)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["length"], 2)
        self.assertIn(blocks[0]["status"], {"ALL_HIGH", "HIGH_56", "HIGH_BASE"})

    def test_attach_keeps_only_halls_with_a_hit(self) -> None:
        hot = self._machine([self._at("1", 4000, 20), self._at("2", 4000, 20)])
        cold = self._machine([self._at("8", 4000, 8), self._at("9", 4000, 8)])
        cold["hall_name"] = "別店舗"
        attach_lineup_blocks([hot, cold, "skip"])
        self.assertIn("lineup_blocks", hot)
        self.assertEqual(len(hot["lineup_blocks"]), 1)
        self.assertNotIn("lineup_blocks", cold)
