import asyncio
import random
import tempfile
import unittest
from pathlib import Path

from src.scraper.site7_circuit import (
    CircuitHalted,
    build_plan,
    classify_stop,
    run_circuit,
    sample_gap,
    write_halt,
)


class TokyoCircuitPlanTest(unittest.TestCase):
    def test_default_plan_is_ten_tokyo_maruhan_halls(self) -> None:
        plan = build_plan()
        self.assertEqual(len(plan.halls), 10)
        self.assertEqual(plan.gap_min_sec, 360)
        self.assertEqual(plan.gap_max_sec, 720)
        names = [hall.name for hall in plan.halls]
        self.assertEqual(
            names,
            [
                "マルハン新宿東宝ビル店",
                "マルハン池袋店",
                "マルハン大山店",
                "メガガイア明大前店",
                "メガガイア調布",
                "マルハン蒲田駅東店",
                "メガガイア蒲田店",
                "マルハン小岩スロット館",
                "マルハン亀有店",
                "マルハン鹿浜店",
            ],
        )
        self.assertTrue(all(hall.id.startswith("daidata-") for hall in plan.halls))

    def test_unknown_hall_id_is_rejected(self) -> None:
        with self.assertRaises(RuntimeError):
            build_plan(hall_ids=["daidata-203505", "missing-hall"])

    def test_gap_stays_inside_six_to_twelve_minutes(self) -> None:
        rng = random.Random(7)
        for _ in range(40):
            gap = sample_gap(rng, 360, 720)
            self.assertGreaterEqual(gap, 360)
            self.assertLessEqual(gap, 720)


class TokyoCircuitRunTest(unittest.TestCase):
    def test_captcha_stops_the_rest_of_the_circuit(self) -> None:
        plan = build_plan()
        seen: list[str] = []

        async def fetch_one(name: str) -> list:
            seen.append(name)
            if name == "マルハン大山店":
                raise RuntimeError("【要対応】サイトセブン側でCAPTCHA認証が発生しました")
            return [{"machine_name": "ジャグラー"}]

        slept: list[float] = []

        async def sleeper(delay: float) -> None:
            slept.append(delay)

        with tempfile.TemporaryDirectory() as tmp:
            halt = Path(tmp) / "circuit_halt.json"
            result = asyncio.run(
                run_circuit(
                    plan,
                    fetch_one,
                    sleeper=sleeper,
                    rng=random.Random(1),
                    halt_path=halt,
                    clock=lambda: 0.0,
                )
            )
            self.assertEqual(result.stopped, "captcha")
            self.assertEqual(seen, ["マルハン新宿東宝ビル店", "マルハン池袋店", "マルハン大山店"])
            self.assertEqual(len(slept), 2)
            self.assertTrue(halt.is_file())
            with self.assertRaises(CircuitHalted):
                asyncio.run(
                    run_circuit(
                        plan,
                        fetch_one,
                        sleeper=sleeper,
                        halt_path=halt,
                        clock=lambda: 0.0,
                    )
                )

    def test_missing_myhall_continues_after_the_gap(self) -> None:
        plan = build_plan(hall_ids=["daidata-203505", "daidata-204020"])
        seen: list[str] = []

        async def fetch_one(name: str) -> list:
            seen.append(name)
            if name == "マルハン新宿東宝ビル店":
                raise RuntimeError("マイホールに「マルハン新宿東宝ビル店」が見つかりません")
            return [{"machine_name": "ジャグラー"}, {"machine_name": "番長"}]

        slept: list[float] = []

        async def sleeper(delay: float) -> None:
            slept.append(delay)

        with tempfile.TemporaryDirectory() as tmp:
            result = asyncio.run(
                run_circuit(
                    plan,
                    fetch_one,
                    sleeper=sleeper,
                    rng=random.Random(3),
                    halt_path=Path(tmp) / "circuit_halt.json",
                    clock=lambda: 0.0,
                )
            )
        self.assertIsNone(result.stopped)
        self.assertEqual(seen, ["マルハン新宿東宝ビル店", "マルハン池袋店"])
        self.assertEqual([item.status for item in result.outcomes], ["failed", "ok"])
        self.assertEqual(result.outcomes[0].reason, "マイホール未登録")
        self.assertEqual(result.outcomes[1].rows, 2)
        self.assertEqual(len(slept), 1)
        self.assertGreaterEqual(slept[0], 360)
        self.assertLessEqual(slept[0], 720)

    def test_account_restriction_is_a_hard_stop(self) -> None:
        self.assertEqual(
            classify_stop(RuntimeError("サイトセブンが自動取得を検知したため、ホールデータを表示していません。")),
            "restricted",
        )
        self.assertIsNone(classify_stop(RuntimeError("マイホールに「マルハン池袋店」が見つかりません")))
        plan = build_plan(hall_ids=["daidata-204020", "daidata-203922"])

        async def fetch_one(name: str) -> list:
            raise RuntimeError("利用を一時的に停止しています")

        async def sleeper(_delay: float) -> None:
            raise AssertionError("停止後に待機しない")

        with tempfile.TemporaryDirectory() as tmp:
            halt = Path(tmp) / "halt.json"
            result = asyncio.run(
                run_circuit(plan, fetch_one, sleeper=sleeper, halt_path=halt, clock=lambda: 0.0)
            )
            payload = write_halt("restricted", "マルハン池袋店", path=halt)
            self.assertEqual(result.stopped, "restricted")
            self.assertEqual(len(result.outcomes), 1)
            self.assertTrue(payload.is_file())
            self.assertNotIn("password", payload.read_text(encoding="utf-8").casefold())


if __name__ == "__main__":
    unittest.main()
