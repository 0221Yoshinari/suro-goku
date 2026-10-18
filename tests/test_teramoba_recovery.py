import json
import unittest
from html import escape
from typing import Optional
from unittest import mock

from src.scraper.halls import hall_by_name
from src.scraper.pace import summarize_failure
from src.scraper.teramoba import (
    RECOVERY_DISMISS_COOKIE,
    TeramobaClient,
    TeramobaError,
    is_maintenance_page,
    is_recovery_notice,
    target_from_hall,
)


def _inertia(component: str, props: Optional[dict] = None) -> str:
    payload = json.dumps({"component": component, "props": props or {}}, ensure_ascii=False)
    return (
        '<html><head><meta name="csrf-token" content="token"></head>'
        f'<div id="app" data-page="{escape(payload, quote=True)}"></div></html>'
    )


class TeramobaRecoveryNoticeTest(unittest.TestCase):
    def test_cushion_page_is_not_treated_as_an_outage(self) -> None:
        page = {"component": "MaintenancePage", "props": {}}
        self.assertTrue(is_recovery_notice(page))
        self.assertFalse(is_maintenance_page(_inertia("MaintenancePage"), page))

    def test_view_site_cookie_is_sent_and_the_rack_page_is_reread(self) -> None:
        hall = hall_by_name("玉屋∞本店")
        self.assertIsNotNone(hall)
        client = TeramobaClient(target_from_hall(hall))
        self.assertEqual(client._cookie_value(RECOVERY_DISMISS_COOKIE), "1")
        rack = _inertia(
            "RackInfo",
            {"latest_date": "2026-10-17", "data": {"key": "k", "iv": "v"}, "hall_informations": {}},
        )
        responses = [
            _inertia("MaintenancePage").encode("utf-8"),
            rack.encode("utf-8"),
        ]

        def fake_request(url, **kwargs):
            self.assertIn("rack_info_kt", url)
            return responses.pop(0)

        with mock.patch.object(client, "_request", side_effect=fake_request):
            with mock.patch("src.scraper.teramoba.persist_protection_cookies") as persist:
                html, page = client._read_html_page(
                    "https://tamaya.pt.teramoba2.com/honten/rack_info_kt?kind_code=G"
                )
        self.assertIn("RackInfo", html)
        self.assertEqual(page["component"], "RackInfo")
        self.assertEqual(page["props"]["latest_date"], "2026-10-17")
        persist.assert_called_once()
        self.assertEqual(responses, [])

    def test_a_second_cushion_is_a_specific_failure(self) -> None:
        hall = hall_by_name("玉屋∞本店")
        client = TeramobaClient(target_from_hall(hall))
        cushion = _inertia("MaintenancePage").encode("utf-8")
        with mock.patch.object(client, "_request", return_value=cushion):
            with mock.patch("src.scraper.teramoba.persist_protection_cookies"):
                with self.assertRaises(TeramobaError) as caught:
                    client._read_html_page("https://tamaya.pt.teramoba2.com/honten/rack_info_kt?kind_code=G")
        self.assertEqual(summarize_failure(str(caught.exception)), "復旧案内を通過できず")


class TeramobaInterstitialTest(unittest.TestCase):
    def test_inertia_page_is_not_a_captcha_even_if_the_head_mentions_it(self) -> None:
        from src.scraper.teramoba import is_access_interstitial

        cushion = (
            '<html><head><script src="/captcha.js"></script></head>'
            '<div data-page="{&quot;component&quot;:&quot;MaintenancePage&quot;}"></div></html>'
        ).encode()
        challenge = b"<html><title>Just a moment...</title><body>cf-challenge</body></html>"
        self.assertFalse(is_access_interstitial(cushion))
        self.assertTrue(is_access_interstitial(challenge))
        self.assertFalse(is_access_interstitial(b'{"message":"captcha required"}'))


if __name__ == "__main__":
    unittest.main()
