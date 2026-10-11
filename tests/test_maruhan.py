import json
import random
import unittest
from pathlib import Path
from unittest.mock import patch

from src.scraper.halls import hall_from_raw, load_halls_config
from src.scraper.maruhan import (
    PAUSE_MAX_SEC,
    PAUSE_MIN_SEC,
    MaruhanConfig,
    build_guest_headers,
    build_guest_request,
    config_for_hall,
    fetch_maruhan_data,
    fetch_maruhan_hall,
    map_maruhan_machine,
    map_maruhan_payload,
    uses_mock,
    wait_between_requests,
)
from src.scraper.pscube import _row_from_counts
from src.scraper.registry import list_sources

ROOT = Path(__file__).resolve().parents[1]


class MaruhanConfigTest(unittest.TestCase):
    def test_halls_json_defines_maruhan_app(self) -> None:
        load_halls_config.cache_clear()
        payload = json.loads((ROOT / "config" / "halls.json").read_text(encoding="utf-8"))
        block = payload["maruhan_app"]
        self.assertEqual(block["pause_min_sec"], 1.5)
        self.assertEqual(block["pause_max_sec"], 3.5)
        self.assertIn("{hall_id}", block["machines_path"])
        self.assertEqual(block["base_url"], "")
        self.assertEqual(block["user_agent"], "MaruhanApp/1.0")
        self.assertEqual(block["headers"]["X-App-Version"], "1.0.0")

    def test_source_type_and_hall_id(self) -> None:
        hall = hall_from_raw(
            {
                "id": "maruhan-sample",
                "name": "マルハンサンプル",
                "enabled": False,
                "group": "maruhan",
                "prefecture": "fukuoka",
                "area": "fukuoka_central",
                "source_type": "maruhan_app",
                "maruhan_hall_id": "hall-100",
                "maruhan_app": {"base_url": "https://app.example.test"},
            }
        )
        self.assertIsNotNone(hall)
        assert hall is not None
        self.assertEqual(hall.source, "maruhan_app")
        self.assertEqual(hall.maruhan_hall_id, "hall-100")
        self.assertEqual((hall.source_config or {})["base_url"], "https://app.example.test")
        self.assertEqual((hall.source_config or {})["maruhan_hall_id"], "hall-100")

    def test_env_overrides_url_and_user_agent(self) -> None:
        hall = hall_from_raw(
            {
                "name": "マルハンサンプル",
                "source": "maruhan_app",
                "maruhan_hall_id": "hall-100",
                "maruhan_app": {"user_agent": "ConfigAgent/1"},
            }
        )
        assert hall is not None
        with patch.dict(
            "os.environ",
            {"MARUHAN_BASE_URL": "https://guest.example.test/api/", "MARUHAN_USER_AGENT": "ManualAgent/2"},
            clear=False,
        ):
            config = config_for_hall(hall)
        self.assertEqual(config.base_url, "https://guest.example.test/api/")
        self.assertEqual(config.user_agent, "ManualAgent/2")
        self.assertEqual(config.hall_id, "hall-100")


class MaruhanGuestRequestTest(unittest.TestCase):
    def test_request_is_guest_get_with_app_headers(self) -> None:
        config = MaruhanConfig(
            hall_id="hall-100",
            hall_name="マルハンサンプル",
            base_url="https://guest.example.test/api/",
            machines_path="/guest/halls/{hall_id}/machines",
            history_path="",
            user_agent="MaruhanGuest/1.0",
            headers={"X-App-Version": "9.9.9", "Cookie": "session=secret", "X-Platform": "android"},
            query={"mode": "guest"},
        )
        request = build_guest_request(config, config.machines_path)
        self.assertEqual(request.method, "GET")
        self.assertIsNone(request.data)
        self.assertEqual(request.full_url, "https://guest.example.test/api/guest/halls/hall-100/machines?mode=guest")
        self.assertEqual(request.get_header("User-agent"), "MaruhanGuest/1.0")
        self.assertEqual(request.get_header("X-app-version"), "9.9.9")
        self.assertEqual(request.get_header("X-platform"), "android")
        self.assertIsNone(request.get_header("Cookie"))

    def test_pause_stays_between_1_5_and_3_5(self) -> None:
        delays: list[float] = []
        for seed in range(20):
            wait_between_requests(sleeper=delays.append, rng=random.Random(seed))
        self.assertEqual(PAUSE_MIN_SEC, 1.5)
        self.assertEqual(PAUSE_MAX_SEC, 3.5)
        self.assertTrue(all(PAUSE_MIN_SEC <= delay <= PAUSE_MAX_SEC for delay in delays))


class MaruhanMappingTest(unittest.TestCase):
    def test_maps_counts_and_history_to_pscube_row(self) -> None:
        raw = {
            "machineName": "マイジャグラーV",
            "machineNo": "0123",
            "bbCount": 2,
            "rbCount": 8,
            "totalGame": 4000,
            "detailHistory": [
                {"date": "2026-10-01", "bb": 1, "rb": 3, "games": 1800},
                {"date": "2026-09-30", "bbCount": 1, "rbCount": 5, "totalGames": 2200},
            ],
        }
        row = map_maruhan_machine(raw, "マルハンサンプル")
        expected = _row_from_counts(
            machine_name="マイジャグラーV",
            dai_no="0123",
            games=4000,
            bb=2,
            rb=8,
            art=None,
            hall_name="マルハンサンプル",
        )
        self.assertIsNotNone(row)
        assert row is not None
        for key, value in expected.items():
            self.assertEqual(row[key], value)
        self.assertEqual(
            row["history"],
            [
                {"day": "2026-10-01", "bb": 1, "rb": 3, "art": None, "games": 1800},
                {"day": "2026-09-30", "bb": 1, "rb": 5, "art": None, "games": 2200},
            ],
        )

    def test_payload_list_and_wrapped_object(self) -> None:
        wrapped = {
            "result": {
                "machines": [
                    {"name": "番長", "dai": "平均", "bb": 1, "rb": 1, "games": 100},
                    {"name": "番長", "dai": "50", "bb": 1, "rb": 2, "games": 900},
                    {"name": "番長", "dai": "50", "bb": 9, "rb": 9, "games": 1},
                ]
            }
        }
        rows = map_maruhan_payload(wrapped, "マルハンサンプル")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["dai_no"], "50")
        self.assertEqual(rows[0]["bb"], 1)
        self.assertEqual(rows[0]["total_games"], 900)

    def test_source_is_registered(self) -> None:
        self.assertIn("maruhan_app", list_sources())

    def test_empty_endpoint_uses_mock_rows(self) -> None:
        load_halls_config.cache_clear()
        hall = hall_from_raw(
            {
                "id": "maruhan-hakozaki",
                "name": "マルハン箱崎店",
                "source": "maruhan_app",
                "maruhan_hall_id": "",
            }
        )
        assert hall is not None
        config = config_for_hall(hall)
        self.assertTrue(uses_mock(config))
        self.assertEqual(config.user_agent, "MaruhanApp/1.0")
        headers = build_guest_headers(config)
        self.assertEqual(headers["User-Agent"], "MaruhanApp/1.0")
        self.assertEqual(headers["X-App-Version"], "1.0.0")
        with patch("src.scraper.maruhan.urllib.request.urlopen") as opened:
            rows = fetch_maruhan_hall(hall, sleeper=lambda _delay: None)
        opened.assert_not_called()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["mock"] for row in rows))
        self.assertEqual({row["dai_no"] for row in rows}, {"101", "102"})
        self.assertTrue(all(row["history"] for row in rows))

    def test_fetch_skips_maruhan_without_network(self) -> None:
        with patch("src.scraper.maruhan.urllib.request.urlopen") as opened:
            result = fetch_maruhan_data([])
        opened.assert_not_called()
        self.assertEqual(result.machines, [])
        self.assertTrue(result.extra.get("skipped"))


if __name__ == "__main__":
    unittest.main()
