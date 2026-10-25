from __future__ import annotations

import unittest

from src.analyzer.judgment import aggregate_machine_groups
from src.analyzer.juggler import attach_juggler_view, build_juggler_islands, classify_juggler_family, score_island


def _unit(dai: str, games: int, bb: int, rb: int, diff: int | None = None, history=None) -> dict:
    hits = bb + rb
    return {
        "dai_no": dai,
        "total_games": games,
        "bb_count": bb,
        "rb_count": rb,
        "synthetic_probability": round(games / hits, 1) if hits else None,
        "rb_probability": round(games / rb, 1) if rb else None,
        "diff_medals": diff,
        "history": history,
    }


def _machine(hall: str, name: str, units: list[dict], *, series: str = "ジャグラー", criteria=None) -> dict:
    return {
        "hall_name": hall,
        "machine_name": name,
        "series": series,
        "judgment_type": "A",
        "status": "NOT_HIGH",
        "unit_count": len(units),
        "criteria": criteria,
        "units": units,
    }


class JugglerFamilyTest(unittest.TestCase):
    def test_known_series_map_to_separate_islands(self) -> None:
        self.assertEqual(classify_juggler_family("SマイジャグラーⅤ")["id"], "my-juggler-v")
        self.assertEqual(classify_juggler_family("ﾏｲｼﾞｬｸﾞﾗｰVI")["id"], "my-juggler-vi")
        self.assertEqual(classify_juggler_family("マイジャグラーVI〔20円〕")["id"], "my-juggler-vi")
        self.assertEqual(classify_juggler_family("ファンキージャグラー2KT")["id"], "funky-juggler-2")
        self.assertEqual(classify_juggler_family("ゴージャグ3")["id"], "gogo-juggler-3")
        self.assertEqual(classify_juggler_family("SアイムジャグラーEX-TP")["id"], "im-juggler")
        self.assertEqual(classify_juggler_family("SネオアイムジャグラーEX-KK")["id"], "neo-im-juggler")
        self.assertIsNone(classify_juggler_family("L東京喰種", series="スマスロ"))

    def test_same_family_names_merge_and_im_stays_apart_from_neo(self) -> None:
        islands = build_juggler_islands(
            [
                _machine("天神", "ゴーゴージャグラー3", [_unit("1", 1000, 4, 4)]),
                _machine("天神", "ゴージャグ3", [_unit("2", 1000, 4, 4)]),
                _machine("天神", "アイムジャグラーEX", [_unit("10", 1000, 3, 3)]),
                _machine("天神", "ネオアイムジャグラーEX", [_unit("20", 1000, 3, 3)]),
                _machine("天神", "L東京喰種", [_unit("30", 5000, 0, 40)], series="スマスロ"),
            ]
        )
        by_family = {item["family_id"]: item for item in islands}
        self.assertEqual(set(by_family), {"gogo-juggler-3", "im-juggler", "neo-im-juggler"})
        self.assertEqual(by_family["gogo-juggler-3"]["unit_count"], 2)
        self.assertEqual(by_family["gogo-juggler-3"]["total_games"], 2000)

    def test_different_rates_stay_separate_islands(self) -> None:
        islands = build_juggler_islands(
            [
                _machine("博多", "ｼﾞｬｸﾞﾗｰｶﾞｰﾙｽﾞSS〔46枚〕", [_unit(str(n), 1000, 4, 4) for n in range(701, 705)]),
                _machine("博多", "ｼﾞｬｸﾞﾗｰｶﾞｰﾙｽﾞSS〔184枚〕", [_unit("936", 1000, 4, 4)]),
            ]
        )
        self.assertEqual(len(islands), 2)
        counts = sorted(item["unit_count"] for item in islands)
        self.assertEqual(counts, [1, 4])
        self.assertTrue(all(item["family_id"] == "juggler-girls" for item in islands))
        self.assertEqual({item["rate"] for item in islands}, {"46枚", "184枚"})


class JugglerIslandScoreTest(unittest.TestCase):
    def test_setting6_island_is_all6(self) -> None:
        units = [_unit(str(index), 3200, 14, 14, diff=100) for index in range(1, 9)]
        islands = build_juggler_islands([_machine("天神", "マイジャグラーV", units)])
        island = islands[0]
        self.assertEqual(island["badge"], "all6")
        self.assertEqual(island["badge_label"], "全6")
        self.assertFalse(island["low_sample"])
        self.assertTrue(island["hot"])
        self.assertEqual(island["unit_count"], 8)
        self.assertEqual(island["avg_games"], 3200)
        self.assertEqual(island["bb_count"], 112)
        self.assertEqual(island["rb_count"], 112)
        self.assertLessEqual(island["synthetic_probability"], 114.6)
        self.assertLessEqual(island["rb_probability"], 229.1)
        self.assertGreaterEqual(island["score"], 80)

    def test_hot_combined_with_cold_reg_is_not_a_high_island(self) -> None:
        units = [_unit(str(index), 3200, 23, 5) for index in range(1, 9)]
        island = build_juggler_islands([_machine("天神", "マイジャグラーV", units)])[0]
        self.assertLess(island["synthetic_probability"], 119.0)
        self.assertGreater(island["rb_probability"], 238.3)
        self.assertEqual(island["badge"], "none")
        self.assertFalse(island["hot"])

    def test_reg_advantage_is_called_out_when_combined_lags(self) -> None:
        # 合算は設定5に届かず、REGだけ設定5を超える島。
        units = [_unit(str(index), 3200, 8, 14) for index in range(1, 5)]
        island = build_juggler_islands([_machine("天神", "マイジャグラーV", units)])[0]
        self.assertGreater(island["synthetic_probability"], 119.0)
        self.assertLessEqual(island["rb_probability"], 238.3)
        self.assertEqual(island["badge"], "none")
        self.assertFalse(island["hot"])
        self.assertTrue(island["reg_meets_setting_5"])

    def test_short_sample_with_high_probs_stays_in_all6(self) -> None:
        units = [_unit(str(index), 1500, 8, 8) for index in range(1, 5)]
        island = build_juggler_islands([_machine("天神", "マイジャグラーV", units)])[0]
        self.assertEqual(island["badge"], "all6")
        self.assertEqual(island["badge_label"], "全6")
        self.assertTrue(island["low_sample"])
        self.assertEqual(island["avg_games"], 1500)

    def test_single_hot_unit_is_all6(self) -> None:
        island = build_juggler_islands([_machine("天神", "マイジャグラーV", [_unit("7", 4000, 18, 18)])])[0]
        self.assertEqual(island["badge"], "all6")
        self.assertEqual(island["badge_label"], "全6")
        self.assertFalse(island["low_sample"])

    def test_gogo_arena_low_average_hits_stay_visible(self) -> None:
        # GOGOアリーナ天神の当日値。平均300G台でも合算とREGが乗っていれば欄に残す。
        neo = score_island(
            unit_count=40,
            avg_games=307.1,
            synthetic=132.1,
            rb_probability=267.0,
            bars={
                "min_games": 3000,
                "6": {"synthetic": 127.5, "rb": 255.0},
                "5": {"synthetic": 128.5, "rb": 255.0},
                "4": {"synthetic": 142.2, "rb": 315.1},
            },
        )
        self.assertEqual(neo["badge"], "high456")
        self.assertTrue(neo["low_sample"])
        gogo = score_island(
            unit_count=20,
            avg_games=342.8,
            synthetic=102.3,
            rb_probability=253.9,
            bars={
                "min_games": 3000,
                "6": {"synthetic": 117.4, "rb": 234.9},
                "5": {"synthetic": 123.7, "rb": 247.3},
                "4": {"synthetic": 130.5, "rb": 268.6},
            },
        )
        self.assertEqual(gogo["badge"], "high456")
        self.assertTrue(gogo["low_sample"])

    def test_setting5_margin_matches_store_dashboard(self) -> None:
        # ネオアイムの公表設定5（合成128.5 / REG255.0）。少し悪いが+4%以内なので全56。
        judged = score_island(
            unit_count=10,
            avg_games=3140.6,
            synthetic=132.0,
            rb_probability=260.0,
            bars={
                "min_games": 3000,
                "6": {"synthetic": 127.5, "rb": 255.0},
                "5": {"synthetic": 128.5, "rb": 255.0},
                "4": {"synthetic": 142.2, "rb": 315.1},
            },
        )
        self.assertEqual(judged["badge"], "high56")
        self.assertFalse(judged["low_sample"])


class JugglerSlumpTest(unittest.TestCase):
    def test_diff_and_unit_order_slump(self) -> None:
        units = [
            _unit("12", 3000, 10, 10, diff=-40),
            _unit("3", 3000, 10, 10, diff=100),
        ]
        island = build_juggler_islands([_machine("天神", "マイジャグラーV", units)])[0]
        self.assertEqual(island["diff_medals"], 60)
        self.assertEqual(island["diff_label"], "差枚プラス")
        self.assertEqual(island["slump_kind"], "units")
        self.assertEqual([point["dai_no"] for point in island["slump"]], ["3", "12"])

    def test_aligned_history_becomes_island_slump(self) -> None:
        units = [
            _unit(
                "1",
                2000,
                8,
                8,
                diff=50,
                history=[{"day": "10:00", "games": 500, "diff": 10}, {"day": "12:00", "games": 2000, "diff": 50}],
            ),
            _unit(
                "2",
                2000,
                8,
                8,
                diff=-20,
                history=[{"day": "10:00", "games": 500, "差枚": -5}, {"day": "12:00", "games": 2000, "差枚": -20}],
            ),
        ]
        island = build_juggler_islands([_machine("天神", "ファンキージャグラー2", units)])[0]
        self.assertEqual(island["slump_kind"], "history")
        self.assertEqual([point["diff"] for point in island["slump"]], [5, 30])

    def test_results_overlay_fills_missing_unit_diff(self) -> None:
        machine = _machine("天神", "マイジャグラーV", [_unit("5", 3200, 14, 14)])
        machine["units"][0]["diff_medals"] = None
        islands = build_juggler_islands(
            [machine],
            results=[{"hall_name": "天神", "machine_name": "マイジャグラーV", "dai_no": "5", "diff_medals": 420}],
        )
        self.assertEqual(islands[0]["diff_medals"], 420)
        self.assertEqual(islands[0]["units"][0]["diff_medals"], 420)


class JugglerPrefectureViewTest(unittest.TestCase):
    def test_store_dashboard_payload_keeps_area_juggler_islands(self) -> None:
        from src.dashboard.app import _finish_synced

        payload = _finish_synced(
            {
                "machines": [
                    _machine(
                        "GOGOアリーナ天神",
                        "ネオアイムジャグラーEX",
                        [_unit(str(index), 3200, 14, 14) for index in range(1, 5)],
                    )
                ],
                "halls": ["GOGOアリーナ天神"],
            }
        )
        islands = payload["juggler"]["islands"]
        self.assertEqual(payload["juggler"]["all6_count"], 1)
        self.assertEqual(islands[0]["badge"], "all6")
        self.assertEqual(islands[0]["family_id"], "neo-im-juggler")
        self.assertEqual(islands[0]["hall_name"], "GOGOアリーナ天神")
        self.assertNotIn("results", payload)


class JugglerPayloadTest(unittest.TestCase):
    def test_attach_counts_hot_islands(self) -> None:
        payload = {
            "machines": [
                _machine("天神", "マイジャグラーV", [_unit(str(index), 3200, 14, 14) for index in range(1, 5)]),
                _machine("博多", "マイジャグラーV", [_unit("1", 500, 1, 1)]),
            ],
            "results": [],
        }
        attach_juggler_view(payload)
        self.assertEqual(payload["juggler"]["island_count"], 2)
        self.assertEqual(payload["juggler"]["all6_count"], 1)
        self.assertEqual(payload["juggler"]["zentai_count"], 1)
        self.assertGreaterEqual(payload["juggler"]["hot_count"], 1)

    def test_aggregate_keeps_unit_diff(self) -> None:
        machines = aggregate_machine_groups(
            [
                {
                    "hall_name": "テスト店",
                    "machine_name": "マイジャグラーV",
                    "judgment_type": "A",
                    "series": "ジャグラー",
                    "judgment_excluded": True,
                    "dai_no": "2",
                    "real_normal_games": 1000,
                    "bb_count": 4,
                    "rb_count": 4,
                    "diff_medals": 40,
                },
                {
                    "hall_name": "テスト店",
                    "machine_name": "マイジャグラーV",
                    "judgment_type": "A",
                    "series": "ジャグラー",
                    "judgment_excluded": True,
                    "dai_no": "1",
                    "real_normal_games": 1000,
                    "bb_count": 4,
                    "rb_count": 4,
                    "diff_medals": -15,
                },
            ]
        )
        self.assertEqual(len(machines), 1)
        self.assertEqual(machines[0]["diff_medals"], 25)
        self.assertEqual([unit["diff_medals"] for unit in machines[0]["units"]], [-15, 40])
