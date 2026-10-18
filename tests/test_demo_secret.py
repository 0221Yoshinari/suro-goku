from __future__ import annotations

import json
import unittest
from pathlib import Path

from src.dashboard.demo_secret import demo_payload
from src.dashboard.demo_secret import rank_group


ROOT = Path(__file__).resolve().parents[1]


class DemoSecretDataTest(unittest.TestCase):
    def test_october_10_joint_winners(self) -> None:
        face = rank_group("face")
        self.assertEqual(face[0]["machine_name"], "スマスロ モンスターハンターライズ：サンブレイク")
        self.assertEqual(face[0]["avg_games"], 3234)
        self.assertGreaterEqual(len(face[0]["rows"]), 8)
        self.assertTrue(all(2000 <= row["games"] <= 3600 for row in face[0]["rows"]))
        self.assertTrue(any(row["status"] == "HIGH_56" for row in face[0]["rows"]))
        self.assertTrue(any(row["status"] == "HIGH_BASE" for row in face[0]["rows"]))
        self.assertIn("🏆 グループ1位機種", [badge["text"] for badge in face[0]["badges"]])
        self.assertIn("🔥 合同濃厚", [badge["text"] for badge in face[0]["badges"]])

        tamaya = rank_group("tamaya")
        self.assertEqual(tamaya[0]["machine_name"], "L真打吉宗")
        self.assertEqual([row["status"] for row in tamaya[0]["rows"]], ["HIGH_56", "HIGH_BASE", "HIGH_BASE"])

        station = rank_group("dstation")
        self.assertEqual(station[0]["machine_name"], "スマスロ 戦国乙女4")
        self.assertEqual(station[1]["machine_name"], "スマスロ 戦国乙女5")
        self.assertLessEqual(station[0]["score"] - station[1]["score"], 3)
        names = [item["machine_name"] for item in station]
        self.assertNotIn("スマスロ モンキーターンV", names)

    def test_lunchtime_games_and_no_extra_metrics(self) -> None:
        payload = demo_payload()
        blob = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("勝率", blob)
        self.assertNotIn("差枚", blob)
        self.assertNotIn("東京喰種", blob)
        self.assertNotIn("吉宗（真打）", blob)
        self.assertNotIn("Lモンスターハンターライズ", blob)
        self.assertNotIn("L 真・一騎当千", blob)
        games = [machine["games"] for hall in payload["halls"] for machine in hall["machines"]]
        games.extend(row["games"] for hall in payload["digits"] for row in hall["digits"])
        games.extend(
            unit["games"]
            for hall in payload["lineup_halls"]
            for island in hall["islands"]
            for unit in island["units"]
        )
        games.extend(row["games"] for row in payload["city"])
        self.assertTrue(games)
        self.assertTrue(all(2000 <= value <= 3600 for value in games))
        js = (ROOT / "src" / "dashboard" / "static" / "demo-secret.js").read_text(encoding="utf-8")
        self.assertNotIn("勝率", js)
        self.assertNotIn("差枚", js)

    def test_summaries_match_the_october_10_story(self) -> None:
        payload = demo_payload()
        by_id = {group["id"]: group for group in payload["groups"]}
        self.assertIn("3,234", by_id["face"]["summary"])
        self.assertIn("昼", by_id["face"]["summary"])
        self.assertGreaterEqual(len(by_id["face"]["halls"]), 8)
        self.assertNotIn("123", by_id)
        self.assertIn("L真打吉宗", by_id["tamaya"]["summary"])
        self.assertIn("昼", by_id["tamaya"]["summary"])
        self.assertIn("スマスロ 戦国乙女4", by_id["dstation"]["summary"])
        self.assertIn("スマスロ 戦国乙女5", by_id["dstation"]["summary"])
        self.assertIn("7店舗のうち7店舗", by_id["dstation"]["summary"])

    def test_lineup_events_use_fictional_halls_and_major_machines(self) -> None:
        payload = demo_payload()
        halls = payload["lineup_halls"]
        catalog = json.loads((ROOT / "config" / "halls.json").read_text(encoding="utf-8"))
        real_names = {row["name"] for row in catalog["halls"]}
        city_names = {row["hall_name"] for row in payload["city"]}
        names = {hall["hall_name"] for hall in halls}
        self.assertTrue(names.isdisjoint(real_names))
        self.assertTrue(names.isdisjoint(city_names))
        machines = [island["machine_name"] for hall in halls for island in hall["islands"]]
        self.assertIn("マイジャグラーV", machines)
        self.assertTrue(any("北斗の拳" in name for name in machines))
        self.assertTrue(any("モンキーターン" in name for name in machines))
        self.assertTrue(any("カバネリ" in name for name in machines))
        for hall in halls:
            self.assertGreaterEqual(len(hall["islands"]), 2)
            for island in hall["islands"]:
                targets = [unit for unit in island["units"] if unit["target"]]
                others = [unit for unit in island["units"] if not unit["target"]]
                self.assertEqual(len(island["units"]), 15)
                self.assertEqual(island["floor_units"], 15)
                nos = [unit["no"] for unit in island["units"]]
                self.assertEqual(nos, list(range(nos[0], nos[0] + 15)))
                self.assertIn("全15台", island["finding"])
                self.assertGreaterEqual(len(targets), 3)
                self.assertTrue(others)
                self.assertTrue(all(unit["status"] == "NORMAL" for unit in others))
                self.assertIn("良さそう", island["finding"])
                self.assertEqual(island["target_from"], targets[0]["no"])
                self.assertEqual(island["target_to"], targets[-1]["no"])
                for unit in island["units"]:
                    est = unit["est"]
                    if island["series"] == "ジャグラー":
                        bb = float(unit["bb"].split("/")[1])
                        reg = float(unit["reg"].split("/")[1])
                        self.assertAlmostEqual(bb, {6: 229.1, 5: 240.1, 4: 254.0, 3: 266.4, 2: 270.8}[est], delta=1.1)
                        self.assertAlmostEqual(reg, {6: 229.1, 5: 268.6, 4: 290.0, 3: 336.1, 2: 385.5}[est], delta=1.1)
                    else:
                        published = {
                            "北斗の拳": {6: 235.1, 5: 258.7, 4: 297.8, 3: 334.0, 2: 370.5},
                            "モンキーターン": {6: 222.9, 5: 235.7, 4: 258.8, 3: 277.0, 2: 295.5},
                            "カバネリ": {6: 195.1, 5: 203.2, 4: 214.0, 3: 239.6, 2: 242.3},
                        }[island["series"]]
                        hit = float(unit["first_hit"].split("/")[1])
                        self.assertAlmostEqual(hit, published[est], delta=1.1)
        js = (ROOT / "src" / "dashboard" / "static" / "demo-secret.js").read_text(encoding="utf-8")
        self.assertIn("並び反応", js)
        self.assertNotIn("123博多店", js)
        self.assertNotIn("玉屋", js)

    def test_area_floor_mixes_five_machines_without_prefecture(self) -> None:
        payload = demo_payload()
        self.assertEqual(payload["judged_at"], "2026-10-17 16:30")
        self.assertEqual(payload["area_label"], "天神・博多周辺")
        self.assertEqual(len(payload["nearby"]), 20)
        self.assertEqual(len(set(payload["nearby"])), 20)
        city = payload["city"]
        self.assertEqual(len(city), 5)
        self.assertTrue(all(row["area"] == payload["area_label"] for row in city))
        self.assertTrue(all(row["hall_name"] in payload["nearby"] for row in city))
        by_name = {row["machine_name"]: row for row in city}
        self.assertEqual(by_name["ファンキージャグラー2"]["units"], 13)
        self.assertEqual(by_name["ファンキージャグラー2"]["status"], "HIGH_56")
        self.assertEqual(by_name["いざ！番長"]["units"], 4)
        self.assertEqual(by_name["いざ！番長"]["status"], "ALL_HIGH")
        self.assertEqual(by_name["L甲鉄城のカバネリ海門決戦"]["units"], 10)
        self.assertEqual(by_name["L甲鉄城のカバネリ海門決戦"]["status"], "HIGH_56")
        self.assertEqual(by_name["スマスロ モンスターハンターライズ：サンブレイク"]["units"], 8)
        self.assertEqual(by_name["スマスロ モンスターハンターライズ：サンブレイク"]["status"], "HIGH_BASE")
        self.assertEqual(by_name["スマスロ北斗の拳"]["units"], 6)
        self.assertEqual(by_name["スマスロ北斗の拳"]["status"], "HIGH_BASE")
        self.assertEqual(sum(row["status"] == "ALL_HIGH" for row in city), 1)
        visible = json.dumps(
            {"area": payload["area_label"], "city": city, "nearby": payload["nearby"]},
            ensure_ascii=False,
        )
        for word in ("都道府県", "北海道", "東京都", "大阪府", "京都府", "県", "福岡", "佐賀", "熊本"):
            self.assertNotIn(word, visible)
        js = (ROOT / "src" / "dashboard" / "static" / "demo-secret.js").read_text(encoding="utf-8")
        self.assertNotIn("福岡", js)
        self.assertNotIn("都道府県", js)

    def test_my_juggler_units_alternate_at_evening(self) -> None:
        floor = demo_payload()["juggler"]
        units = floor["units"]
        self.assertEqual(floor["hall_name"], "白金ステージ天神")
        self.assertEqual(floor["machine_name"], "マイジャグラーV")
        self.assertNotIn("県", floor["area"])
        self.assertEqual(len(units), 20)
        self.assertEqual([unit["no"] for unit in units], list(range(241, 261)))
        self.assertEqual([unit["est"] for unit in units], [1, 5, 2, 6] * 5)
        for unit in units:
            self.assertEqual(unit["status"], {1: "NORMAL", 2: "NORMAL", 5: "HIGH_56", 6: "ALL_HIGH"}[unit["est"]])
            self.assertEqual(unit["hit_count"], unit["bb_count"] + unit["reg_count"])
            games = unit["games"]
            hourly = games / 6.5
            self.assertGreaterEqual(hourly, 740)
            self.assertLessEqual(hourly, 830)
            bb = games / unit["bb_count"]
            reg = games / unit["reg_count"]
            syn = games / unit["hit_count"]
            published = {
                1: (273.1, 409.6, 163.8, 6.0, 10.0, 2.6),
                2: (270.8, 385.5, 159.1, 6.0, 8.0, 2.2),
                5: (240.1, 268.6, 126.8, 4.2, 4.2, 1.8),
                6: (229.1, 229.1, 114.6, 3.2, 3.2, 1.6),
            }[unit["est"]]
            self.assertAlmostEqual(bb, published[0], delta=published[3])
            self.assertAlmostEqual(reg, published[1], delta=published[4])
            self.assertAlmostEqual(syn, published[2], delta=published[5])
            self.assertEqual(unit["first_hit"], f"1/{syn:.1f}")
            self.assertEqual(unit["bb"], f"1/{bb:.1f}")
            self.assertEqual(unit["reg"], f"1/{reg:.1f}")
        cold_reg = [unit["games"] / unit["reg_count"] for unit in units if unit["est"] == 1]
        warm_reg = [unit["games"] / unit["reg_count"] for unit in units if unit["est"] == 2]
        self.assertGreater(min(cold_reg), max(warm_reg))

    def test_page_is_unlisted_outside_the_demo_files(self) -> None:
        skip = {"demo_secret.html", "demo-secret.js", "demo-secret.css"}
        for folder in ("templates", "static"):
            for path in (ROOT / "src" / "dashboard" / folder).rglob("*"):
                if not path.is_file() or path.suffix not in {".html", ".js", ".css"}:
                    continue
                if path.name in skip:
                    continue
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("demo-secret", text, path.name)
                self.assertNotIn("pr-demo", text, path.name)
