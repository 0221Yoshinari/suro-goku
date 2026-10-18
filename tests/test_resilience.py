import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.notify.discord import build_error_content
from src.scraper.hall_catalog import _fold
from src.scraper.halls import Hall
from src.scraper.pace import public_http_detail, summarize_failure
from src.scraper.proxy_config import (
    build_client_opener,
    next_endpoint,
    playwright_proxy,
    reset_proxy_state,
)
from src.scraper.proxy_config import RotatingProxyHandler
from src.scraper.smart_retry import SmartRetry, is_deferrable, set_ledger
from src.scraper.source_fallback import fallback_hall, finish_hall_attempt


class ProxyConfigTest(unittest.TestCase):
    def tearDown(self) -> None:
        reset_proxy_state()

    def test_round_robin_uses_config_when_env_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "proxy_config.json"
            path.write_text(
                json.dumps(
                    {
                        "enabled": True,
                        "rotation": "round_robin",
                        "endpoints": [
                            {"protocol": "http", "host": "a.example", "port": 8000},
                            {"protocol": "http", "host": "b.example", "port": 8001, "username_env": "TEST_PROXY_USER"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            env = {
                "PROXY_CONFIG_PATH": str(path),
                "TEST_PROXY_USER": "resident",
                "SCRAPER_PROXY_SERVER": "",
                "SCRAPER_PROXY_HOST": "",
                "SCRAPER_PROXY_PORT": "",
                "SITE7_PROXY_SERVER": "",
            }
            with mock.patch.dict(os.environ, env, clear=False):
                reset_proxy_state()
                first = playwright_proxy()
                second = next_endpoint()
                third = playwright_proxy()
            self.assertEqual(first["server"], "http://a.example:8000")
            self.assertNotIn("username", first)
            self.assertEqual(second["server"], "http://b.example:8001")
            self.assertEqual(second["username"], "resident")
            self.assertNotIn("password", second)
            self.assertEqual(third["server"], "http://a.example:8000")
            reset_proxy_state()
            with mock.patch.dict(os.environ, env, clear=False):
                opener = build_client_opener()
            self.assertTrue(any(isinstance(handler, RotatingProxyHandler) for handler in opener.handlers))

    def test_env_proxy_overrides_config(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "SCRAPER_PROXY_SERVER": "http://env.example:9",
                "SCRAPER_PROXY_USER": "",
                "SCRAPER_PROXY_USERNAME": "",
                "SCRAPER_PROXY_PASS": "",
                "SCRAPER_PROXY_PASSWORD": "",
                "SITE7_PROXY_USER": "",
                "SITE7_PROXY_PASS": "",
            },
            clear=False,
        ):
            reset_proxy_state()
            proxy = playwright_proxy()
        self.assertEqual(proxy["server"], "http://env.example:9")
        self.assertNotIn("password", proxy)


class SmartRetryTest(unittest.TestCase):
    def tearDown(self) -> None:
        set_ledger(None)

    def test_defer_skips_until_backoff_and_then_allows(self) -> None:
        clock = {"now": 1_000.0}

        def now() -> float:
            return clock["now"]

        with tempfile.TemporaryDirectory() as tmp:
            ledger = SmartRetry(Path(tmp) / "retry.json", clock=now)
            self.assertTrue(is_deferrable("HTTP 429"))
            self.assertTrue(is_deferrable("タイムアウト"))
            self.assertFalse(is_deferrable("0 件"))
            delay = ledger.defer("hall-a", "goraggio", "HTTP 429 Too Many Requests")
            self.assertEqual(delay, 60.0)
            self.assertEqual(ledger.snapshot("hall-a", "goraggio")["reason"], "429上限")
            self.assertTrue(ledger.should_skip("hall-a", "goraggio"))
            clock["now"] += 60.0
            self.assertFalse(ledger.should_skip("hall-a", "goraggio"))
            self.assertEqual(ledger.defer("hall-a", "goraggio", "timed out", delay_sec=90), 120.0)
            self.assertEqual(ledger.defer("hall-a", "goraggio", "HTTP 429", delay_sec=5000), 1800.0)
            ledger.mark_ok("hall-a", "goraggio")
            self.assertFalse(ledger.should_skip("hall-a", "goraggio"))
            self.assertEqual(ledger.snapshot("hall-a", "goraggio"), {})


class SourceFallbackTest(unittest.TestCase):
    def tearDown(self) -> None:
        set_ledger(None)

    def test_goraggio_failure_switches_to_named_teramoba_hall(self) -> None:
        primary = Hall(id="g-1", name="テストホール", aliases=("テストホール",), source="goraggio")
        alternate = Hall(
            id="t-1",
            name="テストホール",
            aliases=("テストホール",),
            source="teramoba",
            source_url="https://example.pt.teramoba2.com/demo",
        )
        found = fallback_hall(primary, index={("teramoba", _fold("テストホール")): alternate})
        self.assertEqual(found.id, "t-1")
        other = Hall(id="g-2", name="別店", aliases=("別店",), source="goraggio")
        self.assertIsNone(fallback_hall(alternate, index={("goraggio", _fold("別店")): other}))

    def test_explicit_fallback_and_defer_do_not_leak_internal_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            set_ledger(SmartRetry(Path(tmp) / "retry.json", clock=lambda: 10.0))
            hall = Hall(id="sample", name="サンプル店", aliases=("サンプル店",), source="goraggio")
            config = {
                "halls": [
                    {
                        "id": "sample",
                        "name": "サンプル店",
                        "source": "goraggio",
                        "fallback": {
                            "source": "teramoba",
                            "base_url": "https://example.pt.teramoba2.com",
                            "department": "demo",
                        },
                    }
                ]
            }
            with mock.patch("src.scraper.source_fallback.load_halls_config", return_value=config):
                alternate = fallback_hall(hall, index={})
                self.assertEqual(alternate.source, "teramoba")
                self.assertEqual((alternate.source_config or {}).get("department"), "demo")
                with mock.patch("src.scraper.source_fallback.recover_with_fallback", return_value=[{"hall_name": "サンプル店"}]):
                    payload, error, via = finish_hall_attempt(
                        hall,
                        None,
                        'HTTP 429 https://daidata.goraggio.com/1 Traceback (most recent call last):\n  File "src/scraper/goraggio.py"',
                        "goraggio",
                    )
            self.assertEqual(payload, [{"hall_name": "サンプル店"}])
            self.assertEqual(error, "")
            self.assertEqual(via, "teramoba")
            from src.scraper.smart_retry import get_ledger

            self.assertEqual(get_ledger().snapshot("sample", "goraggio")["reason"], "429上限")


class ErrorMaskTest(unittest.TestCase):
    def test_notices_drop_traces_urls_and_paths(self) -> None:
        raw = (
            'Traceback (most recent call last):\n  File "/Users/me/src/scraper/goraggio.py", line 3\n'
            "https://daidata.goraggio.com/100969 token=secret"
        )
        self.assertEqual(summarize_failure(raw), "取得失敗")
        self.assertEqual(summarize_failure("HTTP 429 Too Many Requests"), "429上限")
        text = build_error_content(raw)
        self.assertNotIn("Traceback", text)
        self.assertNotIn("goraggio", text)
        self.assertNotIn("secret", text)
        self.assertNotIn("src/scraper", text)
        self.assertIn("取得失敗", text)
        self.assertEqual(
            public_http_detail('File "/tmp/app.py" https://internal.example token'),
            "処理に失敗しました。しばらくしてから再度お試しください。",
        )
        self.assertEqual(public_http_detail("認証が必要です"), "認証が必要です")


class MonitorEnableTest(unittest.TestCase):
    def test_every_region_monitor_is_enabled(self) -> None:
        payload = json.loads(
            (Path(__file__).resolve().parents[1] / "config" / "x_monitors.json").read_text(encoding="utf-8")
        )
        accounts = payload["accounts"]
        self.assertGreaterEqual(len(accounts), 9)
        self.assertTrue(all(item.get("enabled") is True for item in accounts))
        regions = {item.get("region") for item in accounts}
        self.assertTrue(
            {"hokkaido", "tohoku", "kanto", "koshinetsu", "tokai", "kansai", "chugoku", "shikoku", "kyushu"} <= regions
        )


class SourceSkipAndMaintenanceTest(unittest.TestCase):
    def test_teramoba_batch_skips_while_the_site_is_down(self) -> None:
        import asyncio

        from src.scraper.teramoba import fetch_teramoba_data_async

        async def run():
            with mock.patch.dict(os.environ, {"TERAMOBA_FETCH": "0"}, clear=False):
                return await fetch_teramoba_data_async([])

        result = asyncio.run(run())
        self.assertEqual(result.machines, [])
        self.assertTrue(result.extra.get("skipped"))
        self.assertEqual(result.extra.get("reason"), "テラモバ2障害中のため一時停止")

    def test_pscube_batch_skips_without_fetching(self) -> None:
        from src.scraper.pscube import fetch_pscube_data

        with mock.patch.dict(os.environ, {"PSCUBE_FETCH": ""}, clear=False):
            with mock.patch("src.scraper.pscube._fetch_pscube_data_live") as live:
                result = fetch_pscube_data([])
        live.assert_not_called()
        self.assertEqual(result.machines, [])
        self.assertTrue(result.extra.get("skipped"))

    def test_maintenance_page_is_detected_before_captcha(self) -> None:
        from src.scraper.teramoba import TeramobaMaintenance, is_maintenance_page

        html = (
            '<div data-page="{&quot;component&quot;:&quot;UnsupportedDevicePage&quot;,'
            '&quot;props&quot;:{&quot;showMaintenanceImage&quot;:true}}"></div>'
            "システム障害に関するお詫び"
        )
        self.assertTrue(is_maintenance_page(html, {"component": "UnsupportedDevicePage", "props": {"showMaintenanceImage": True}}))
        self.assertFalse(is_maintenance_page("<html>台番号</html>", {"component": "rack_info_kt", "props": {}}))
        self.assertEqual(
            __import__("src.scraper.pace", fromlist=["summarize_failure"]).summarize_failure(
                "店舗ステータス：メンテナンス中"
            ),
            "メンテナンス中",
        )
        from src.scraper.smart_retry import is_deferrable

        self.assertTrue(is_deferrable("店舗ステータス：メンテナンス中"))
        self.assertTrue(issubclass(TeramobaMaintenance, Exception))


class LineGateAndTurnstileTest(unittest.TestCase):
    def setUp(self) -> None:
        self._env = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)

    def test_pscube_browser_requires_bright_data_proxy(self) -> None:
        from src.scraper.pscube import PscubeError, _LINE_GATE_JS, _launch_chrome

        self.assertIn("公式LINE追加されている方のみ見ることができます", _LINE_GATE_JS)
        self.assertIn("line-auth-block", _LINE_GATE_JS)
        self.assertIn("line-connect-modal", _LINE_GATE_JS)
        with mock.patch("src.scraper.proxy_config.playwright_proxy", return_value=None):
            with self.assertRaises(PscubeError):
                _launch_chrome()

        launch = mock.Mock()
        browser = mock.Mock()
        context = mock.Mock()
        launch.return_value = browser
        browser.new_context.return_value = context
        context.new_page.return_value = mock.Mock()
        playwright = mock.Mock()
        playwright.chromium.launch = launch
        starter = mock.Mock()
        starter.start.return_value = playwright
        proxy = {"server": "http://brd.superproxy.io:44445", "username": "zone", "password": "secret"}
        with mock.patch("playwright.sync_api.sync_playwright", return_value=starter):
            with mock.patch("src.scraper.proxy_config.playwright_proxy", return_value=proxy):
                _launch_chrome()
        self.assertEqual(launch.call_args.kwargs["proxy"]["server"], proxy["server"])
        self.assertIn("line-auth-block", context.add_init_script.call_args_list[-1].args[0])

    def test_teramoba_turnstile_token_is_sent_on_later_requests(self) -> None:
        from src.scraper.teramoba import RENDER_WAIT_SEC, TeramobaClient, target_from_hall
        from src.scraper.halls import hall_by_name

        hall = hall_by_name("玉屋トリアーダ宗像店")
        self.assertIsNotNone(hall)
        client = TeramobaClient(target_from_hall(hall))
        client._install_turnstile_token("turnstile-token")
        headers = client._headers(json_api=True)
        self.assertEqual(headers["cf-turnstile-response"], "turnstile-token")
        self.assertEqual(client._cookie_value("cf-turnstile-response"), "turnstile-token")
        self.assertGreaterEqual(RENDER_WAIT_SEC, 3)


if __name__ == "__main__":
    unittest.main()
