import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from src.scraper.base import FetchResult
from src.scraper.fetch_all import log_acquisition_failures


class AcquisitionSaveTest(unittest.TestCase):
    def test_incoming_json_is_replaced_atomically(self) -> None:
        result = FetchResult(
            source="site7",
            hall_name="複数店舗",
            machines=[{"hall_name": "玉屋∞本店", "dai_no": "1"}],
            extra={"failed_halls": ["プラザ 本店"]},
        )
        with TemporaryDirectory() as tmp:
            path = result.write_incoming(Path(tmp))
            self.assertEqual(path.name, "incoming.json")
            self.assertFalse(path.with_suffix(".json.tmp").exists())
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["machines"][0]["hall_name"], "玉屋∞本店")
        self.assertEqual(payload["failed_halls"], ["プラザ 本店"])

    def test_failure_log_names_the_hall_and_reason(self) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            log_acquisition_failures(
                [
                    {"hall": "プラザ 本店", "reason": "429上限"},
                    {"hall": "", "reason": "取得失敗"},
                    "skip",
                ]
            )
        text = buffer.getvalue()
        self.assertIn("取得失敗 プラザ 本店: 429上限", text)
        self.assertEqual(text.count("取得失敗"), 1)
