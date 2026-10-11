import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.analyzer.judgment import judge_record, load_masters
from src.analyzer.overrides import OverrideError
from src.dashboard.admin import clear_master_edit, inspection_rows, list_master_rows, save_master_edit


def _demo_master() -> dict:
    return {
        "version": "test",
        "machines": [
            {
                "id": "demo-at",
                "name": "デモAT",
                "type": "AT",
                "setting_6": 200.0,
                "setting_5": 230.0,
                "setting_4": 250.0,
            }
        ],
    }


class OverridePriorityTest(unittest.TestCase):
    def test_saved_theory_overrides_master_on_judgment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            masters_path = root / "machines.json"
            overrides_path = root / "overrides.json"
            masters_path.write_text(json.dumps(_demo_master()), encoding="utf-8")
            overrides_path.write_text(
                json.dumps(
                    {
                        "machines": {
                            "demo-at": {
                                "setting_6": 400.0,
                                "setting_5": 420.0,
                                "setting_4": 450.0,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            base = load_masters(masters_path, apply_override=False)
            self.assertEqual(base[0].settings[6].synthetic, 200.0)
            self.assertEqual(base[0].hit_target, "rb")
            overridden = load_masters(masters_path, overrides_path=overrides_path)
            self.assertEqual(overridden[0].settings[6].synthetic, 400.0)
            record = {
                "hall_name": "テスト店",
                "fields": {
                    "machine_name": "デモAT",
                    "total_games": 3000,
                    "bb_count": 0,
                    "rb_count": 10,
                },
            }
            before = judge_record(record, base)
            after = judge_record(record, overridden)
            self.assertEqual(before["status"], "NOT_HIGH")
            self.assertEqual(after["status"], "ALL_HIGH")
            self.assertEqual(after["criteria"]["setting_6"], 400.0)
            self.assertTrue(after["criteria"]["override"]["active"])

    def test_at_ignores_bb_hit_target_and_uses_rb(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            masters_path = Path(tmp) / "machines.json"
            payload = _demo_master()
            payload["machines"][0]["hit_target"] = "bb"
            payload["machines"][0]["hit_source"] = "bb"
            payload["machines"][0]["target_count"] = "bonus"
            masters_path.write_text(json.dumps(payload), encoding="utf-8")
            masters = load_masters(masters_path, apply_override=False)
            self.assertEqual(masters[0].hit_target, "rb")
            self.assertEqual(masters[0].hit_source, "rb")
            result = judge_record(
                {
                    "hall_name": "テスト店",
                    "fields": {
                        "machine_name": "デモAT",
                        "total_games": 3000,
                        "bb_count": 30,
                        "rb_count": 5,
                        "art_count": 40,
                    },
                },
                masters,
            )
            self.assertEqual(result["first_hit_count"], 5)
            self.assertAlmostEqual(result["synthetic_probability"], 600.0)
            self.assertEqual(result["criteria"]["hit_target"], "rb")

    def test_kabaneri_series_uses_rb(self) -> None:
        masters = load_masters(apply_override=False)
        by_id = {item.id: item for item in masters}
        for machine_id in ("kabaneri-1", "kabaneri-kaimon"):
            master = by_id[machine_id]
            self.assertEqual(master.type, "AT")
            self.assertEqual(master.hit_target, "rb")
            self.assertEqual(master.hit_source, "rb")
            self.assertEqual(master.target_count, "cz")
        matched = next(item for item in masters if item.id == "kabaneri-1")
        from src.analyzer.judgment import match_machine

        self.assertEqual(match_machine("Sパチスロ甲鉄城のカバネリZR", masters), matched)
        result = judge_record(
            {
                "hall_name": "テスト店",
                "fields": {
                    "machine_name": "Sパチスロ甲鉄城のカバネリZR",
                    "total_games": 1863,
                    "bb_count": 12,
                    "rb_count": 9,
                },
            },
            masters,
        )
        self.assertEqual(result["matched_machine"]["id"], "kabaneri-1")
        self.assertEqual(result["first_hit_count"], 9)
        self.assertAlmostEqual(result["synthetic_probability"], 207.0)

    def test_save_then_clear_restores_published_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "overrides.json"
            with patch("src.analyzer.overrides.DEFAULT_OVERRIDES_PATH", path):
                saved = save_master_edit(
                    "my-juggler-v",
                    {
                        "type": "A",
                        "hit_target": "combined",
                        "setting_6": 100.0,
                        "setting_5": 110.0,
                        "setting_4": 120.0,
                        "rb_6": 200.0,
                        "rb_5": 220.0,
                        "rb_4": 240.0,
                    },
                )
                self.assertTrue(saved["override_active"])
                self.assertEqual(saved["setting_6"], 100.0)
                loaded = next(item for item in load_masters() if item.id == "my-juggler-v")
                self.assertEqual(loaded.settings[6].synthetic, 100.0)
                self.assertEqual(loaded.settings[6].rb, 200.0)
                cleared = clear_master_edit("my-juggler-v")
                self.assertFalse(cleared["override_active"])
                self.assertEqual(cleared["setting_6"], 114.6)
                restored = next(item for item in load_masters() if item.id == "my-juggler-v")
                self.assertEqual(restored.settings[6].synthetic, 114.6)

    def test_identical_values_do_not_keep_an_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "overrides.json"
            with patch("src.analyzer.overrides.DEFAULT_OVERRIDES_PATH", path):
                current = next(item for item in list_master_rows()["machines"] if item["id"] == "im-juggler-ex")
                saved = save_master_edit(
                    "im-juggler-ex",
                    {
                        "type": current["type"],
                        "hit_target": current["hit_target"],
                        "setting_6": current["setting_6"],
                        "setting_5": current["setting_5"],
                        "setting_4": current["setting_4"],
                        "rb_6": current["rb_6"],
                        "rb_5": current["rb_5"],
                        "rb_4": current["rb_4"],
                    },
                )
                self.assertFalse(saved["override_active"])
                stored = json.loads(path.read_text(encoding="utf-8"))
                self.assertNotIn("im-juggler-ex", stored["machines"])

    def test_invalid_probability_is_rejected(self) -> None:
        with self.assertRaises(OverrideError):
            save_master_edit(
                "my-juggler-v",
                {
                    "type": "A",
                    "hit_target": "combined",
                    "setting_6": 0,
                    "setting_5": 110,
                    "setting_4": 120,
                },
            )

    def test_inspection_reads_latest_without_error(self) -> None:
        payload = inspection_rows()
        self.assertIn("rows", payload)
        self.assertEqual(payload["count"], len(payload["rows"]))
