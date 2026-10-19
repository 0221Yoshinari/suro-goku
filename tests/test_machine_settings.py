import unittest
from types import SimpleNamespace

from src.analyzer.judgment import (
    STATUS_ALL_HIGH,
    STATUS_HIGH_BASE,
    STATUS_INSUFFICIENT_GAMES,
    STATUS_NOT_HIGH,
    _classify,
    judge_record,
    load_masters,
)


class MachineSettingsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.masters = load_masters()

    def test_sao_is_excluded_and_setting6_is_corrected(self) -> None:
        sao = next(master for master in self.masters if master.id == "sao-2")
        self.assertFalse(sao.enabled)
        self.assertEqual(sao.settings[6].synthetic, 207.7)
        result = judge_record(
            {
                "hall_name": "テスト店",
                "fields": {
                    "machine_name": "ＬソードアートＰＡ１",
                    "total_games": 6000,
                    "bb_count": 1,
                    "rb_count": 40,
                    "art_count": 80,
                },
            },
            self.masters,
        )
        self.assertEqual(result["status"], "EXCLUDED")
        self.assertTrue(result["judgment_excluded"])
        self.assertFalse(result["is_all_high"])

    def test_iza_bancho_is_excluded_because_at_bonus_cannot_be_split(self) -> None:
        master = next(item for item in self.masters if item.id == "iza-bancho")
        self.assertFalse(master.enabled)
        for name in ("Lいざ!番長", "L/いざ番長/SB8", "Ｌいざ番長ＳＢ８"):
            result = judge_record(
                {
                    "hall_name": "テスト店",
                    "fields": {
                        "machine_name": name,
                        "total_games": 4000,
                        "bb_count": 40,
                        "rb_count": 30,
                    },
                },
                self.masters,
            )
            self.assertEqual(result["status"], "EXCLUDED", name)
            self.assertTrue(result["judgment_excluded"], name)
            self.assertFalse(result["is_all_high"], name)
            self.assertFalse(result["is_high_56"], name)
            self.assertFalse(result["is_high_base"], name)
        other = judge_record(
            {
                "hall_name": "テスト店",
                "fields": {
                    "machine_name": "押忍!番長ZERO",
                    "total_games": 4000,
                    "bb_count": 10,
                    "rb_count": 20,
                },
            },
            self.masters,
        )
        self.assertFalse(other["judgment_excluded"])
        self.assertNotEqual(other["status"], "EXCLUDED")

    def test_mh_rise_uses_rb_probability_against_new_bars(self) -> None:
        mh = next(master for master in self.masters if master.id == "mh-rise")
        self.assertTrue(mh.enabled)
        self.assertEqual(mh.probability_source, "rb")
        expected = {1: 309.5, 2: 301.4, 3: 290.8, 4: 256.4, 5: 237.1, 6: 230.8}
        for level, value in expected.items():
            self.assertEqual(mh.settings[level].synthetic, value)

        # 3000/10 = 300。ART 30回なら 1/100 になり全6に見えるが、RBだけを使う。
        result = judge_record(
            {
                "hall_name": "テスト店",
                "fields": {
                    "machine_name": "ＬモンハンライズＸＡ",
                    "total_games": 3000,
                    "bb_count": 0,
                    "rb_count": 10,
                    "art_count": 30,
                },
            },
            self.masters,
        )
        self.assertEqual(result["first_hit_count"], 10)
        self.assertAlmostEqual(result["rb_probability"], 300.0)
        self.assertAlmostEqual(result["synthetic_probability"], 300.0)
        self.assertEqual(result["status"], "NOT_HIGH")
        self.assertEqual(result["estimated_setting"], 2)

        hot = judge_record(
            {
                "hall_name": "テスト店",
                "fields": {
                    "machine_name": "LモンハンライズXA",
                    "total_games": 4616,
                    "bb_count": 0,
                    "rb_count": 20,
                    "art_count": 5,
                },
            },
            self.masters,
        )
        self.assertEqual(hot["first_hit_count"], 20)
        self.assertAlmostEqual(hot["synthetic_probability"], 230.8)
        self.assertEqual(hot["status"], "ALL_HIGH")

        no_rb = judge_record(
            {
                "hall_name": "テスト店",
                "fields": {
                    "machine_name": "モンハンライズ",
                    "total_games": 3000,
                    "bb_count": 2,
                    "rb_count": 0,
                    "art_count": 30,
                },
            },
            self.masters,
        )
        self.assertIsNone(no_rb["first_hit_count"])
        self.assertEqual(no_rb["status"], "INCOMPLETE")

    def test_hokuto_ad_first_hit_is_rb_not_combined(self) -> None:
        """BB32 / RB10 / 通常G5887 は初当り 1/588.7。合算 1/140.2 や短い台の 1/157 では判定しない。"""
        combined_looking = judge_record(
            {
                "hall_name": "123＋N新市街店",
                "fields": {
                    "machine_name": "L パチスロ北斗の拳AD XR",
                    "total_games": 5887,
                    "bb_count": 32,
                    "rb_count": 10,
                    "art_count": 0,
                    "first_hit_count": 42,
                    "synthetic_probability": 140.2,
                },
            },
            self.masters,
        )
        self.assertEqual(combined_looking["judgment_type"], "AT")
        self.assertEqual(combined_looking["first_hit_count"], 10)
        self.assertAlmostEqual(combined_looking["synthetic_probability"], 588.7, places=1)
        self.assertNotEqual(combined_looking["status"], "ALL_HIGH")
        self.assertFalse(combined_looking["checks"]["meets_setting_6"])

        short_unit = judge_record(
            {
                "hall_name": "123＋N新市街店",
                "fields": {
                    "machine_name": "⑥L パチスロ北斗の拳AD XR",
                    "total_games": 157,
                    "bb_count": 7,
                    "rb_count": 1,
                    "art_count": 0,
                    "synthetic_probability": 20,
                },
            },
            self.masters,
        )
        self.assertEqual(short_unit["first_hit_count"], 1)
        self.assertAlmostEqual(short_unit["synthetic_probability"], 157.0)
        self.assertEqual(short_unit["status"], "INSUFFICIENT_GAMES")
        self.assertFalse(short_unit["checks"]["meets_setting_6"])

    def test_shallow_games_follow_setting_bars(self) -> None:
        master = SimpleNamespace(min_games=3000, early_min_games=300)
        self.assertEqual(_classify(800, master, True, True, True), STATUS_ALL_HIGH)
        self.assertEqual(_classify(800, master, False, True, True), "HIGH_56")
        self.assertEqual(_classify(800, master, False, False, True), STATUS_HIGH_BASE)
        self.assertEqual(_classify(800, master, False, False, False), STATUS_INSUFFICIENT_GAMES)
        self.assertEqual(_classify(4000, master, False, False, False), STATUS_NOT_HIGH)

        # 1000/4 = 250。設定4(256.4)は超え、設定6(230.8)は超えない。規定3000G未満でも全456。
        shallow = judge_record(
            {
                "hall_name": "テスト店",
                "fields": {
                    "machine_name": "ＬモンハンライズＸＡ",
                    "total_games": 1000,
                    "bb_count": 0,
                    "rb_count": 4,
                    "art_count": 20,
                },
            },
            self.masters,
        )
        self.assertEqual(shallow["status"], "HIGH_BASE")
        self.assertEqual(shallow["status_label"], "全456数値")
        self.assertFalse(shallow["is_early_alert"])

        # 1000/5 = 200。設定6(230.8)をクリアするので、浅くても全6。
        hot = judge_record(
            {
                "hall_name": "テスト店",
                "fields": {
                    "machine_name": "ＬモンハンライズＸＡ",
                    "total_games": 1000,
                    "bb_count": 0,
                    "rb_count": 5,
                    "art_count": 1,
                },
            },
            self.masters,
        )
        self.assertEqual(hot["status"], "ALL_HIGH")
        self.assertEqual(hot["status_label"], "全6数値")

    def test_at_machines_judge_from_rb_not_chain_bonus(self) -> None:
        from src.analyzer.judgment import aggregate_machine_groups

        # BB 107回なら 10282/107 ≒ 1/96.1 で設定6を誤って超える。初当りは RB 52回。
        fields = {
            "machine_name": "L甲鉄城のカバネリ海門決戦",
            "total_games": 10282,
            "bb_count": 107,
            "rb_count": 52,
            "art_count": 40,
        }
        result = judge_record({"hall_name": "GOGOアリーナ天神", "fields": fields}, self.masters)
        self.assertEqual(result["judgment_type"], "AT")
        self.assertEqual(result["first_hit_count"], 52)
        self.assertAlmostEqual(result["synthetic_probability"], round(10282 / 52, 4))
        self.assertNotAlmostEqual(result["synthetic_probability"], round(10282 / 107, 4), places=2)
        # 10282/52 ≒ 1/197.7。設定6(195.1)を約1.3%上回り、設定5(203.2)より良い。
        # 10000G超なので誤差範囲内として全6。BB回数では判定しない。
        self.assertEqual(result["status"], "ALL_HIGH")
        self.assertEqual(result["status_label"], "全6数値")
        self.assertTrue(result["checks"]["meets_setting_6"])
        self.assertTrue(result["checks"]["meets_setting_4"])

        grouped = aggregate_machine_groups(
            [
                judge_record(
                    {
                        "hall_name": "GOGOアリーナ天神",
                        "fields": {**fields, "dai_no": "1", "total_games": 5000, "bb_count": 80, "rb_count": 20, "art_count": 30},
                    },
                    self.masters,
                ),
                judge_record(
                    {
                        "hall_name": "GOGOアリーナ天神",
                        "fields": {**fields, "dai_no": "2", "total_games": 5282, "bb_count": 27, "rb_count": 32, "art_count": 10},
                    },
                    self.masters,
                ),
            ]
        )
        machine = grouped[0]
        self.assertEqual(machine["first_hit_count"], 52)
        self.assertEqual(machine["bb_count"], 107)
        self.assertAlmostEqual(machine["synthetic_probability"], round(10282 / 52, 4))
        self.assertEqual(machine["status"], "ALL_HIGH")
        self.assertFalse(machine["is_high_base"])

    def test_setting6_margin_promotes_near_miss_and_keeps_priority(self) -> None:
        from src.analyzer.judgment import SETTING6_MARGIN_MIN_GAMES, aggregate_machine_groups

        # 8000/65 ≒ 1/123.08（設定6 119.2 の +3.3%）、REG 8000/33 ≒ 1/242.4（238.4 の +1.7%）。
        # どちらも設定5（合成124.0 / REG252.1）より良く、平均8000Gなので全6。
        near = judge_record(
            {
                "hall_name": "テスト店",
                "fields": {
                    "machine_name": "ジャグラーガールズSS",
                    "dai_no": "1",
                    "total_games": 8000,
                    "bb_count": 32,
                    "rb_count": 33,
                },
            },
            self.masters,
        )
        self.assertGreaterEqual(near["total_games"], SETTING6_MARGIN_MIN_GAMES)
        self.assertGreater(near["synthetic_probability"], 119.2)
        self.assertLess(near["synthetic_probability"], 124.0)
        self.assertGreater(near["rb_probability"], 238.4)
        self.assertLess(near["rb_probability"], 252.1)
        self.assertTrue(near["checks"]["meets_setting_6"])
        self.assertTrue(near["checks"]["meets_setting_4"])
        self.assertEqual(near["status"], "ALL_HIGH")
        self.assertEqual(near["status_label"], "全6数値")
        self.assertEqual(near["estimated_setting"], 6)
        self.assertFalse(near["is_high_base"])

        # 同じ比率でも平均3000G未満は設定6の許容を使わない。設定5理論値はクリアするので全56。
        shallow = judge_record(
            {
                "hall_name": "テスト店",
                "fields": {
                    "machine_name": "ジャグラーガールズSS",
                    "dai_no": "2",
                    "total_games": 2460,
                    "bb_count": 10,
                    "rb_count": 10,
                },
            },
            self.masters,
        )
        self.assertLess(shallow["total_games"], SETTING6_MARGIN_MIN_GAMES)
        self.assertGreater(shallow["synthetic_probability"], 119.2)
        self.assertEqual(shallow["status"], "HIGH_56")
        self.assertEqual(shallow["status_label"], "全56数値")
        self.assertFalse(shallow["checks"]["meets_setting_6"])
        self.assertTrue(shallow["checks"]["meets_setting_5"])

        # 合成が設定5理論値ちょうどなら、4%以内に見えても全6にしない。全56にする。
        setting5 = judge_record(
            {
                "hall_name": "テスト店",
                "fields": {
                    "machine_name": "マイジャグラーV",
                    "dai_no": "3",
                    "total_games": 5950,
                    "bb_count": 23,
                    "rb_count": 27,
                },
            },
            self.masters,
        )
        self.assertAlmostEqual(setting5["synthetic_probability"], 119.0)
        self.assertEqual(setting5["status"], "HIGH_56")
        self.assertEqual(setting5["status_label"], "全56数値")
        self.assertFalse(setting5["checks"]["meets_setting_6"])
        self.assertTrue(setting5["is_high_56"])
        self.assertFalse(setting5["is_high_base"])

        # 1台が冷えて合算が理論値をわずかに超えても、平均3000G以上なら機種単位は全6。
        hot = judge_record(
            {
                "hall_name": "テスト店",
                "fields": {
                    "machine_name": "ジャグラーガールズSS",
                    "dai_no": "11",
                    "total_games": 8000,
                    "bb_count": 34,
                    "rb_count": 36,
                },
            },
            self.masters,
        )
        cold = judge_record(
            {
                "hall_name": "テスト店",
                "fields": {
                    "machine_name": "ジャグラーガールズSS",
                    "dai_no": "12",
                    "total_games": 8000,
                    "bb_count": 29,
                    "rb_count": 31,
                },
            },
            self.masters,
        )
        self.assertEqual(hot["status"], "ALL_HIGH")
        self.assertNotEqual(cold["status"], "ALL_HIGH")
        grouped = aggregate_machine_groups([hot, cold])
        machine = grouped[0]
        self.assertGreaterEqual(machine["avg_games"], SETTING6_MARGIN_MIN_GAMES)
        self.assertGreater(machine["synthetic_probability"], 119.2)
        self.assertLess(machine["synthetic_probability"], 124.0)
        self.assertGreater(machine["rb_probability"], 238.4)
        self.assertTrue(machine["checks"]["meets_setting_6"])
        self.assertTrue(machine["checks"]["meets_setting_4"])
        self.assertEqual(machine["status"], "ALL_HIGH")
        self.assertEqual(machine["status_label"], "全6数値")
        self.assertFalse(machine["is_high_base"])

    def test_setting5_margin_is_all56_and_does_not_override_all6(self) -> None:
        # プラザ博多のジャグラーガールズSS相当。合成は設定6の誤差内、REGは設定5理論値の直前。
        # 5044/42 ≒ 1/120.1、5044/20 = 1/252.2。設定6 REG 238.4 には届かず、設定5 REG 252.1 の許容内。
        near5 = judge_record(
            {
                "hall_name": "プラザ 博多",
                "fields": {
                    "machine_name": "ジャグラーガールズSS",
                    "dai_no": "101",
                    "total_games": 5044,
                    "bb_count": 22,
                    "rb_count": 20,
                },
            },
            self.masters,
        )
        self.assertGreater(near5["synthetic_probability"], 119.2)
        self.assertLess(near5["synthetic_probability"], 124.0)
        self.assertGreater(near5["rb_probability"], 252.1)
        self.assertLess(near5["rb_probability"], 273.1)
        self.assertFalse(near5["checks"]["meets_setting_6"])
        self.assertTrue(near5["checks"]["meets_setting_5"])
        self.assertTrue(near5["checks"]["meets_setting_4"])
        self.assertEqual(near5["status"], "HIGH_56")
        self.assertEqual(near5["status_label"], "全56数値")
        self.assertTrue(near5["is_high_56"])
        self.assertFalse(near5["is_all_high"])
        self.assertFalse(near5["is_high_base"])

        # REGが設定5の許容を超え、設定4理論値には収まる場合は全456のまま。
        only4 = judge_record(
            {
                "hall_name": "プラザ 博多",
                "fields": {
                    "machine_name": "ジャグラーガールズSS",
                    "dai_no": "102",
                    "total_games": 7950,
                    "bb_count": 33,
                    "rb_count": 30,
                },
            },
            self.masters,
        )
        self.assertGreater(only4["rb_probability"], 252.1 * 1.04)
        self.assertLessEqual(only4["rb_probability"], 273.1)
        self.assertEqual(only4["status"], "HIGH_BASE")
        self.assertEqual(only4["status_label"], "全456数値")

    def test_bakemonogatari_uses_published_at_rates_for_kh_and_plain(self) -> None:
        from src.analyzer.judgment import aggregate_machine_groups, match_machine

        master = next(item for item in self.masters if item.id == "bakemonogatari")
        expected = {1: 265.1, 2: 260.7, 3: 252.1, 4: 238.8, 5: 230.8, 6: 219.6}
        for level, value in expected.items():
            self.assertEqual(master.settings[level].synthetic, value)
        self.assertEqual(match_machine("Lスマスロ化物語 KH", self.masters).id, "bakemonogatari")
        self.assertEqual(match_machine("L化物語", self.masters).id, "bakemonogatari")
        self.assertNotEqual(master.settings[6].synthetic, 319.0)
        self.assertNotEqual(master.settings[4].synthetic, 370.0)

        # 玉屋GO＋空港の5台。合算は 3805/13 ≒ 1/292.7 で設定1（1/265.1）より悪い。
        # 84番だけ 867/4 ≒ 1/216.8 で設定6だが、機種全体が届かないので全6にしない。
        units = [
            ("82", 235, 1),
            ("83", 712, 2),
            ("84", 867, 4),
            ("85", 466, 1),
            ("86", 1525, 5),
        ]
        judged = [
            judge_record(
                {
                    "hall_name": "玉屋GO＋空港",
                    "fields": {
                        "machine_name": "Lスマスロ化物語 KH",
                        "dai_no": dai,
                        "total_games": games,
                        "bb_count": 0,
                        "rb_count": hits,
                        "includes_art_in_total_games": False,
                    },
                },
                self.masters,
            )
            for dai, games, hits in units
        ]
        hot = next(item for item in judged if item["dai_no"] == "84")
        self.assertEqual(hot["status"], "ALL_HIGH")
        grouped = aggregate_machine_groups(judged)[0]
        self.assertAlmostEqual(grouped["synthetic_probability"], round(3805 / 13, 4))
        self.assertGreater(grouped["synthetic_probability"], 265.1)
        self.assertNotIn(grouped["status"], ("ALL_HIGH", "HIGH_56", "HIGH_BASE"))
        self.assertFalse(grouped["is_all_high"])
        self.assertFalse(grouped["is_high_56"])
        self.assertFalse(grouped["is_high_base"])

        # MJアリーナ空港店のL化物語2台。1台は 1/348.8、合算は 3496/9 ≒ 1/388.4。
        pair = [
            judge_record(
                {
                    "hall_name": "MJアリーナ空港店",
                    "fields": {
                        "machine_name": "L化物語",
                        "dai_no": dai,
                        "total_games": games,
                        "rb_count": hits,
                        "includes_art_in_total_games": False,
                    },
                },
                self.masters,
            )
            for dai, games, hits in (("629", 1744, 5), ("630", 1752, 4))
        ]
        self.assertAlmostEqual(pair[0]["synthetic_probability"], 348.8)
        self.assertNotEqual(pair[0]["status"], "HIGH_56")
        machine = aggregate_machine_groups(pair)[0]
        self.assertAlmostEqual(machine["synthetic_probability"], round(3496 / 9, 4))
        self.assertNotIn(machine["status"], ("ALL_HIGH", "HIGH_56", "HIGH_BASE"))

    def test_kabaneri_bonus_total_does_not_treat_cold_pair_as_setting6(self) -> None:
        from src.analyzer.judgment import aggregate_machine_groups, match_machine
        from src.analyzer.lineup import find_lineup_blocks

        master = next(item for item in self.masters if item.id == "kabaneri-1")
        expected = {1: 237.0, 2: 230.7, 3: 214.0, 4: 186.5, 5: 171.3, 6: 151.3}
        for level, value in expected.items():
            self.assertEqual(master.settings[level].synthetic, value)
        self.assertNotEqual(master.settings[6].synthetic, 299.0)
        self.assertNotEqual(master.settings[6].synthetic, 290.6)
        self.assertEqual(match_machine("甲鉄城のｶﾊﾞﾈﾘ", self.masters).id, "kabaneri-1")
        kaimon = match_machine("L甲鉄城のカバネリ海門決戦", self.masters)
        self.assertEqual(kaimon.id, "kabaneri-kaimon")
        self.assertEqual(kaimon.settings[6].synthetic, 195.1)

        # 2台の合算は 2623/9 ≒ 1/291.4。設定1のボーナス合算 1/237.0 より悪く、全6にしない。
        # 766番の 1713/8 ≒ 1/214.1 は設定3付近で、全6・全56・全456ではない。
        judged = [
            judge_record(
                {
                    "hall_name": "MJアリーナ空港店",
                    "fields": {
                        "machine_name": "甲鉄城のｶﾊﾞﾈﾘ",
                        "dai_no": dai,
                        "total_games": games,
                        "rb_count": hits,
                        "art_count": art,
                        "includes_art_in_total_games": False,
                    },
                },
                self.masters,
            )
            for dai, games, hits, art in (("766", 1713, 8, 10), ("767", 910, 1, 2))
        ]
        self.assertAlmostEqual(judged[0]["synthetic_probability"], round(1713 / 8, 4))
        self.assertNotIn(judged[0]["status"], ("ALL_HIGH", "HIGH_56", "HIGH_BASE"))
        self.assertEqual(judged[0]["estimated_setting"], 2)
        near_setting3 = judge_record(
            {
                "hall_name": "MJアリーナ空港店",
                "fields": {
                    "machine_name": "甲鉄城のｶﾊﾞﾈﾘ",
                    "dai_no": "766",
                    "total_games": 2902,
                    "rb_count": 14,
                    "includes_art_in_total_games": False,
                },
            },
            self.masters,
        )
        self.assertAlmostEqual(near_setting3["synthetic_probability"], round(2902 / 14, 4))
        self.assertEqual(near_setting3["estimated_setting"], 3)
        self.assertEqual(near_setting3["estimated_setting_label"], "設定3相当")
        self.assertNotIn(near_setting3["status"], ("ALL_HIGH", "HIGH_56", "HIGH_BASE"))
        machine = aggregate_machine_groups(judged)[0]
        self.assertAlmostEqual(machine["synthetic_probability"], round(2623 / 9, 4))
        self.assertGreater(machine["synthetic_probability"], 237.0)
        self.assertNotIn(machine["status"], ("ALL_HIGH", "HIGH_56", "HIGH_BASE"))
        self.assertFalse(machine["is_all_high"])
        self.assertEqual(find_lineup_blocks(machine), [])


if __name__ == "__main__":
    unittest.main()
