import unittest
from unittest import mock

from src.notify.discord import build_error_content, build_success_content, dashboard_access_line
from src.tunnel import (
    _ensure_public_url_locked,
    _tunnel_still_serving,
    current_public_url,
    extract_public_url,
    local_tunnel_pids,
    tunnel_log_is_dead,
)


class PublicUrlTest(unittest.TestCase):
    def test_extracts_cloudflared_url(self) -> None:
        log = (
            "INF |  Your quick Tunnel has been created! Visit it at:  |\n"
            "INF |  https://foo-bar.trycloudflare.com                  |\n"
        )
        self.assertEqual(extract_public_url(log), "https://foo-bar.trycloudflare.com")

    def test_dead_log_after_registration(self) -> None:
        log = (
            "INF Registered tunnel connection connIndex=0\n"
            'ERR Register tunnel error error="Unauthorized: Tunnel not found"\n'
        )
        self.assertTrue(tunnel_log_is_dead(log))

    def test_registered_log_is_alive(self) -> None:
        log = (
            'ERR Register tunnel error error="Unauthorized: Tunnel not found"\n'
            "INF Registered tunnel connection connIndex=0\n"
        )
        self.assertFalse(tunnel_log_is_dead(log))

    def test_extracts_ngrok_url(self) -> None:
        log = 'msg="started tunnel" url=https://example.ngrok-free.app'
        self.assertEqual(extract_public_url(log), "https://example.ngrok-free.app")

    def test_access_line_is_markdown_link(self) -> None:
        with mock.patch("src.notify.discord.dashboard_url", return_value="https://foo-bar.trycloudflare.com"):
            self.assertEqual(
                dashboard_access_line(),
                "[ダッシュボードを開く](https://foo-bar.trycloudflare.com)",
            )

    def test_local_access_line_stays_plain(self) -> None:
        with mock.patch("src.notify.discord.dashboard_url", return_value="http://127.0.0.1:8000"):
            self.assertEqual(dashboard_access_line(), "ダッシュボード: http://127.0.0.1:8000")

    def test_success_notice_is_compact(self) -> None:
        text = build_success_content(
            {
                "machines": [],
                "judged_at": "2026-10-03T01:45:00+09:00",
                "source": "test",
                "machine_count": 0,
                "unit_count": 0,
                "target_halls": ["テスト店"],
            }
        )
        self.assertIn("📊 **【台データ取得完了】**", text)
        self.assertIn("・成功: **1** 店舗", text)
        self.assertNotIn("trycloudflare", text)

    def test_error_notice_includes_link(self) -> None:
        with mock.patch("src.notify.discord.dashboard_url", return_value="https://foo-bar.trycloudflare.com"):
            text = build_error_content("接続失敗")
        self.assertIn("[ダッシュボードを開く](https://foo-bar.trycloudflare.com)", text)

    def test_cloudflared_uses_ipv4(self) -> None:
        with mock.patch("src.tunnel._binary", return_value="cloudflared"):
            from src.tunnel import _cloudflared_command

            command = _cloudflared_command(8000)
        self.assertEqual(
            command,
            [
                "cloudflared",
                "tunnel",
                "--no-autoupdate",
                "--edge-ip-version",
                "4",
                "--url",
                "http://127.0.0.1:8000",
            ],
        )

    def test_local_tunnel_pids_match_port(self) -> None:
        ps = "\n".join(
            [
                "31907 /opt/homebrew/bin/cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8000",
                "47360 /opt/homebrew/bin/cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8765",
                "12 /usr/bin/python -m src.dashboard",
            ]
        )
        self.assertEqual(local_tunnel_pids(ps, 8000), [31907])
        self.assertEqual(local_tunnel_pids(ps), [31907, 47360])

    def test_live_connector_survives_failed_self_check(self) -> None:
        state = {
            "url": "https://live.trycloudflare.com",
            "pid": 42,
            "provider": "cloudflared",
            "local_port": 8000,
        }
        with mock.patch("src.tunnel._read_state", return_value=state), mock.patch(
            "src.tunnel._log_since_last_start",
            return_value="INF Registered tunnel connection connIndex=0\n",
        ), mock.patch("src.tunnel._pid_alive", return_value=True), mock.patch(
            "src.tunnel.public_url_reachable", return_value=False
        ) as probe:
            self.assertTrue(_tunnel_still_serving("https://live.trycloudflare.com"))
        probe.assert_not_called()

    def test_dead_log_is_not_kept(self) -> None:
        state = {
            "url": "https://dead.trycloudflare.com",
            "pid": 42,
            "provider": "cloudflared",
            "local_port": 8000,
        }
        log = (
            "INF Registered tunnel connection connIndex=0\n"
            'ERR Register tunnel error error="Unauthorized: Tunnel not found"\n'
        )
        with mock.patch("src.tunnel._read_state", return_value=state), mock.patch(
            "src.tunnel._log_since_last_start", return_value=log
        ), mock.patch("src.tunnel._pid_alive", return_value=True):
            self.assertFalse(_tunnel_still_serving("https://dead.trycloudflare.com"))

    def test_issued_url_is_never_replaced(self) -> None:
        state = {
            "url": "https://someone-textiles-unions-muze.trycloudflare.com",
            "pid": 87204,
            "provider": "cloudflared",
            "local_port": 8000,
        }
        with mock.patch("src.tunnel._read_state", return_value=state), mock.patch(
            "src.tunnel._start_process"
        ) as start, mock.patch("src.tunnel._stop_process") as stop, mock.patch(
            "src.tunnel._pid_alive", return_value=False
        ):
            url, created = _ensure_public_url_locked(8000)
        self.assertEqual(url, "https://someone-textiles-unions-muze.trycloudflare.com")
        self.assertFalse(created)
        start.assert_not_called()
        stop.assert_not_called()

    def test_current_public_url_requires_live_process(self) -> None:
        live = {"url": "https://live.trycloudflare.com", "pid": 42, "local_port": 8000}
        with mock.patch("src.tunnel._read_state", return_value=live), mock.patch(
            "src.tunnel._pid_alive", return_value=True
        ):
            self.assertEqual(current_public_url(8000), "https://live.trycloudflare.com")
            self.assertIsNone(current_public_url(8001))
        with mock.patch("src.tunnel._read_state", return_value=live), mock.patch(
            "src.tunnel._pid_alive", return_value=False
        ):
            self.assertIsNone(current_public_url(8000))


if __name__ == "__main__":
    unittest.main()
