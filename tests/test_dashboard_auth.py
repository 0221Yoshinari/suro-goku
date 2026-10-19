from __future__ import annotations

import json
import os
import unittest
from unittest import mock
from urllib.parse import urlencode

os.environ["DASHBOARD_PASSWORD"] = "2026"
os.environ["DASHBOARD_SESSION_DAYS"] = "7"
os.environ["STRIPE_WEBHOOK_SECRET"] = ""

from src.dashboard.auth import issue_token
from src.dashboard.auth import passwords_match
from src.dashboard.auth import token_is_valid
from src.dashboard.app import app


def request(method: str, url: str, *, body: bytes = b"", headers: dict | None = None, cookies: dict | None = None):
    import asyncio
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    header_list = []
    for key, value in (headers or {}).items():
        header_list.append((key.lower().encode(), value.encode()))
    if cookies:
        cookie_header = "; ".join(f"{key}={value}" for key, value in cookies.items())
        header_list.append((b"cookie", cookie_header.encode()))
    header_list.append((b"content-length", str(len(body)).encode()))

    scope = {
        "type": "http",
        "asgi": {"spec_version": "2.3", "version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": parts.path or "/",
        "raw_path": (parts.path or "/").encode(),
        "query_string": parts.query.encode(),
        "headers": header_list,
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            await asyncio.Future()
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    messages = []

    async def send(message):
        messages.append(message)

    asyncio.run(app(scope, receive, send))
    start = next(message for message in messages if message["type"] == "http.response.start")
    payload = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    response_headers = {}
    set_cookies = []
    for key, value in start["headers"]:
        name = key.decode().lower()
        text = value.decode()
        if name == "set-cookie":
            set_cookies.append(text)
            continue
        response_headers[name] = text
    return start["status"], response_headers, payload, set_cookies


def absorb_cookies(jar: dict, set_cookies: list[str]) -> None:
    for raw in set_cookies:
        pair, _, rest = raw.partition(";")
        name, _, value = pair.partition("=")
        if "max-age=0" in rest.lower():
            jar.pop(name, None)
        else:
            jar[name] = value


class DashboardAuthTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cookies: dict[str, str] = {}

    def call(self, method: str, url: str, *, data: dict | None = None, json_body: dict | None = None, headers: dict | None = None):
        headers = dict(headers or {})
        body = b""
        if data is not None:
            body = urlencode(data).encode()
            headers["content-type"] = "application/x-www-form-urlencoded"
        elif json_body is not None:
            body = json.dumps(json_body).encode()
            headers["content-type"] = "application/json"
        status, headers_out, payload, set_cookies = request(
            method,
            url,
            body=body,
            headers=headers,
            cookies=self.cookies,
        )
        absorb_cookies(self.cookies, set_cookies)
        return status, headers_out, payload.decode(), set_cookies

    def test_public_demo_link_skips_login(self) -> None:
        with mock.patch.dict(os.environ, {"SURO_PUBLIC_DEMO": "1"}):
            status, _headers, body, _cookies = self.call("GET", "/")
            self.assertEqual(status, 200)
            self.assertIn("全台系分析", body)
            self.assertNotIn("パスワード", body)
            status, headers, _body, _cookies = self.call("GET", "/login")
            self.assertEqual(status, 302)
            self.assertEqual(headers["location"], "/demo-secret")
            status, _headers, body, _cookies = self.call("GET", "/dem\u2026ecret")
            self.assertEqual(status, 200)
            self.assertIn("全台系分析", body)
            self.assertNotIn("パスワード", body)
            status, _headers, body, _cookies = self.call("GET", "/api/judgments")
            self.assertEqual(status, 401)
            self.assertEqual(json.loads(body)["detail"], "ログインが必要です")

    def test_render_link_stays_behind_login_until_open_mode(self) -> None:
        with mock.patch.dict(os.environ, {"RENDER": "true", "DASHBOARD_OPEN": ""}, clear=False):
            status, headers, body, _cookies = self.call("GET", "/")
            self.assertEqual(status, 303)
            self.assertIn("/login", headers["location"])
            self.assertNotIn("店舗別ダッシュボード", body)
        with mock.patch.dict(os.environ, {"DASHBOARD_OPEN": "1"}, clear=False):
            status, _headers, body, _cookies = self.call("GET", "/")
            self.assertEqual(status, 200)
            self.assertIn("店舗別ダッシュボード", body)
            self.assertNotIn("ライセンスキー / パスワード", body)
            status, headers, _body, _cookies = self.call("GET", "/login")
            self.assertEqual(status, 302)
            self.assertEqual(headers["location"], "/")
            status, _headers, body, _cookies = self.call("GET", "/api/judgments")
            self.assertEqual(status, 200)
            self.assertIn("machines", body)
            status, _headers, body, _cookies = self.call("GET", "/api/hall-catalog")
            self.assertEqual(status, 200)
            status, _headers, _body, _cookies = self.call("POST", "/api/judge")
            self.assertEqual(status, 401)
            status, headers, _body, _cookies = self.call("GET", "/master-panel-x7k3q9")
            self.assertEqual(status, 303)
            self.assertIn("/login", headers["location"])

    def test_dashboard_requires_login(self) -> None:
        status, headers, _body, _cookies = self.call("GET", "/")
        self.assertEqual(status, 303)
        self.assertIn("/login", headers["location"])

        status, _headers, body, _cookies = self.call("GET", "/api/judgments")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["detail"], "ログインが必要です")

        status, _headers, _body, _cookies = self.call("POST", "/api/judge")
        self.assertEqual(status, 401)

    def test_old_admin_path_is_not_found(self) -> None:
        status, headers, body, _cookies = self.call("GET", "/admin")
        self.assertEqual(status, 404)
        self.assertNotIn("location", {key.lower(): value for key, value in headers.items()})
        self.assertIn("Not Found", body)
        status, _headers, body, _cookies = self.call("GET", "/api/admin/masters")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["detail"], "Not Found")

        status, headers, _body, _cookies = self.call("GET", "/master-panel-x7k3q9")
        self.assertEqual(status, 303)
        self.assertIn("/login", headers["location"])

    def test_public_pages_stay_open(self) -> None:
        status, _headers, body, _cookies = self.call("GET", "/lp")
        self.assertEqual(status, 200)
        self.assertIn("スロ極", body)
        self.assertNotIn("スロプロ", body)
        self.assertIn("合同機種", body)
        self.assertIn("あたり末尾", body)
        self.assertIn("2,980", body)
        self.assertIn("4つのクロス分析", body)
        self.assertIn("末尾0の平均推測設定", body)
        self.assertIn("店舗別ダッシュボード", body)
        self.assertIn("某店舗", body)
        self.assertIn("〇〇店と〇〇店合同", body)
        self.assertIn("〇〇グループ合同", body)
        self.assertIn("台別内訳", body)
        self.assertIn("公式LINEで10日間無料で試す", body)
        self.assertIn("見られる機種も、これから増えていきます。", body)
        self.assertIn("新台が出たときも、そのタイミングで追加できるものは追加していく予定です。", body)
        self.assertIn("【本日10/20 正式リリース開始！】", body)
        self.assertIn("10日間無料体験、実施中。", body)
        self.assertIn("【ご利用に関するご注意・免責事項】", body)
        self.assertIn("特定の設定や出玉結果等を保証・断定するものではありません。", body)
        self.assertNotIn("リリース日変更に関するお知らせ", body)
        self.assertIn("10日完全無料", body)
        self.assertIn("解約の手順", body)
        self.assertIn("公式LINEのメニューから「解約手続き」を選ぶ", body)
        self.assertIn("解約方法について", body)
        self.assertNotIn("本文に「解約希望」と書いて送信する", body)
        self.assertNotIn("受け付けた時点で、次回以降の請求が止まる", body)
        self.assertNotIn("Stripeで申し込む", body)
        self.assertNotIn("15,000", body)
        self.assertNotIn("20,000", body)
        self.assertNotIn("重ねる視点", body)
        self.assertIn(">東京<", body)
        self.assertIn(">佐賀<", body)
        self.assertNotIn(">京都<", body)
        self.assertNotIn(">熊本<", body)
        self.assertIn("各都道府県の対応ホールを確認する", body)
        from src.dashboard.app import _line_official_url

        self.assertNotIn('href="/checkout"', body)
        self.assertGreaterEqual(body.count(f'href="{_line_official_url()}"'), 4)
        self.assertNotIn("buy.stripe.com", body)
        self.assertNotIn("js.stripe.com", body)
        self.assertIn('id="inquiry-form"', body)
        self.assertIn("/api/lp-inquiry", body)
        self.assertNotIn("sk_live_", body)
        self.assertNotIn("sk_test_", body)
        status, _headers, health_body, _cookies = self.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(health_body)["ok"], True)
        status, headers, checkout_body, _cookies = self.call("GET", "/api/lp-checkout")
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], "https://lin.ee/7XxLBBy")
        self.assertNotIn("buy.stripe.com", checkout_body)
        status, _headers, _body, _cookies = self.call("GET", "/legal/privacy")
        self.assertEqual(status, 200)
        status, _headers, tokusho, _cookies = self.call("GET", "/legal/tokusho")
        self.assertEqual(status, 200)
        self.assertIn("未設定（環境変数 SELLER_NAME）", tokusho)
        with mock.patch.dict(os.environ, {"SELLER_NAME": "テスト販売者", "SELLER_EMAIL": "seller@example.com"}):
            status, _headers, tokusho, _cookies = self.call("GET", "/legal/tokusho")
        self.assertIn("テスト販売者", tokusho)
        self.assertIn("seller@example.com", tokusho)
        self.assertNotIn("__SELLER_NAME__", tokusho)
        status, _headers, _body, _cookies = self.call("POST", "/api/lp-inquiry", json_body={})
        self.assertEqual(status, 400)

    def test_password_matches_as_text_or_integer(self) -> None:
        self.assertTrue(passwords_match("2026"))
        self.assertTrue(passwords_match("02026", "2026"))
        self.assertFalse(passwords_match("2027"))

    def test_dashboard_password_ignores_surrounding_quotes(self) -> None:
        from src.dashboard.auth import dashboard_password

        with mock.patch.dict(os.environ, {"DASHBOARD_PASSWORD": '"quoted-secret"'}):
            self.assertEqual(dashboard_password(), "quoted-secret")
            self.assertTrue(passwords_match("quoted-secret"))

    def test_login_sets_week_long_cookie(self) -> None:
        status, headers, _body, set_cookies = self.call("POST", "/login", data={"password": "2026", "next": "/"})
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], "/")
        external_status, external_headers, _body, _cookies = self.call(
            "POST",
            "/login",
            data={"password": "2026", "next": "https://evil.example"},
        )
        self.assertEqual(external_status, 303)
        self.assertEqual(external_headers["location"], "/")
        self.assertTrue(token_is_valid(self.cookies.get("dashboard_session")))
        cookie_header = set_cookies[0]
        self.assertIn("Max-Age=604800", cookie_header)
        self.assertIn("HttpOnly", cookie_header)

        status, _headers, body, _cookies = self.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("店舗別ダッシュボード", body)
        self.assertIn("ログアウト", body)
        self.assertNotIn("再判定する", body)
        self.assertNotIn('href="/master-panel-x7k3q9/halls"', body)

        status, _headers, _body, _cookies = self.call("GET", "/api/judgments")
        self.assertEqual(status, 200)

    def test_master_key_opens_dashboard_when_shared_password_is_off(self) -> None:
        status, _headers, body, _cookies = self.call("GET", "/login")
        self.assertEqual(status, 200)
        self.assertIn('name="email" type="text"', body)
        self.assertNotIn('type="email"', body)
        with mock.patch.dict(os.environ, {"MEMBER_DISABLE_SHARED_PASSWORD": "1", "STRIPE_WEBHOOK_SECRET": "whsec_test"}):
            status, headers, body, _cookies = self.call(
                "POST",
                "/login",
                data={"email": "2026", "password": "2026", "next": "/"},
            )
            self.assertEqual(status, 303)
            self.assertEqual(headers["location"], "/")
            self.assertTrue(token_is_valid(self.cookies.get("dashboard_session")))
            status, _headers, body, _cookies = self.call("GET", "/")
            self.assertEqual(status, 200)
            self.assertIn("店舗別ダッシュボード", body)
            self.cookies.clear()
            status, _headers, body, _cookies = self.call(
                "POST",
                "/login",
                data={"email": "2026", "password": "nope", "next": "/"},
            )
            self.assertEqual(status, 200)
            self.assertNotIn("dashboard_session", self.cookies)
            self.assertNotIn("店舗別ダッシュボード", body)

    def test_wrong_password_does_not_open_dashboard(self) -> None:
        status, _headers, body, set_cookies = self.call("POST", "/login", data={"password": "nope", "next": "/"})
        self.assertEqual(status, 200)
        self.assertIn("パスワードが違います", body)
        self.assertNotIn("dashboard_session", self.cookies)
        self.assertEqual(set_cookies, [])

    def test_forged_or_expired_cookie_is_rejected(self) -> None:
        self.assertFalse(token_is_valid("v1.1.not-a-signature"))
        expired = issue_token(now=0)
        self.assertFalse(token_is_valid(expired, now=8 * 24 * 60 * 60))
        self.cookies["dashboard_session"] = expired
        status, _headers, _body, _cookies = self.call("GET", "/api/watch-config")
        self.assertEqual(status, 401)

    def test_logout_clears_session(self) -> None:
        self.call("POST", "/login", data={"password": "2026"})
        status, headers, _body, _cookies = self.call("POST", "/logout")
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], "/login")
        self.assertNotIn("dashboard_session", self.cookies)
        status, _headers, _body, _cookies = self.call("GET", "/api/judgments")
        self.assertEqual(status, 401)

    def test_master_panel_requires_token_beyond_the_dashboard_session(self) -> None:
        with mock.patch.dict(os.environ, {"MASTER_PANEL_TOKEN": "panel-token-value"}):
            self.call("POST", "/login", data={"password": "2026"})
            status, _headers, body, _cookies = self.call("GET", "/master-panel-x7k3q9")
            self.assertEqual(status, 200)
            self.assertIn("管理者トークン", body)
            self.assertNotIn("機種マスター管理", body)
            status, _headers, body, _cookies = self.call("GET", "/master-panel-x7k3q9/hall-rows")
            self.assertEqual(status, 200)
            self.assertNotIn("Traceback", body)
            status, _headers, body, _cookies = self.call(
                "GET",
                "/master-panel-x7k3q9/masters",
                headers={"X-Master-Token": "wrong"},
            )
            self.assertEqual(status, 401)
            self.assertEqual(json.loads(body)["detail"], "認証が必要です")
            self.assertNotIn("Traceback", body)
            status, _headers, body, _cookies = self.call(
                "GET",
                "/master-panel-x7k3q9",
                headers={"Authorization": "Bearer panel-token-value"},
            )
            self.assertEqual(status, 200)
            self.assertIn("機種マスター管理", body)
            status, headers, _body, _cookies = self.call(
                "POST",
                "/master-panel-x7k3q9/unlock",
                data={"master_token": "panel-token-value", "next": "/master-panel-x7k3q9/halls"},
            )
            self.assertEqual(status, 303)
            self.assertEqual(headers["location"], "/master-panel-x7k3q9/halls")
            self.assertIn("master_panel_session", self.cookies)
            status, _headers, body, _cookies = self.call("GET", "/master-panel-x7k3q9/halls")
            self.assertEqual(status, 200)
            self.assertIn("ホール管理", body)
            self.cookies["master_panel_session"] = "m1.1.forged"
            status, _headers, body, _cookies = self.call("GET", "/master-panel-x7k3q9/masters")
            self.assertEqual(status, 401)
            self.assertNotIn("forged", body)
            status, _headers, body, _cookies = self.call("GET", "/master-panel-x7k3q9/hall-rows")
            self.assertEqual(status, 200)

    def test_secret_demo_is_public_and_not_linked(self) -> None:
        status, headers, body, _cookies = self.call("GET", "/demo-secret")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("x-robots-tag"), "noindex, nofollow")
        self.assertNotIn("__DEMO_DATA__", body)
        self.assertNotIn("fetch(", body)
        self.assertNotIn("/api/judgments", body)
        for label in ("末尾分析", "並び分析", "全台系分析", "合同機種分析"):
            self.assertIn(label, body)
        self.assertIn("スマスロ モンスターハンターライズ：サンブレイク", body)
        self.assertIn("Lリコリス・リコイル", body)
        self.assertIn("L真打吉宗", body)
        self.assertIn("スマスロ 戦国乙女4", body)
        self.assertNotIn('href="/"', body)
        self.assertNotIn('href="/lp"', body)

        status, headers, _body, _cookies = self.call("GET", "/pr-demo")
        self.assertEqual(status, 302)
        self.assertEqual(headers["location"], "/demo-secret")

        status, _headers, body, _cookies = self.call("GET", "/lp")
        self.assertEqual(status, 200)
        self.assertNotIn("demo-secret", body)
        self.assertNotIn("pr-demo", body)

        self.call("POST", "/login", data={"password": "2026"})
        status, _headers, body, _cookies = self.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertNotIn("demo-secret", body)
        self.assertNotIn("pr-demo", body)

    def test_password_comes_from_environment(self) -> None:
        with mock.patch.dict(os.environ, {"DASHBOARD_PASSWORD": "secret-value"}):
            self.assertTrue(passwords_match("secret-value"))
            self.assertFalse(passwords_match("2026"))
            status, _headers, body, _cookies = self.call("POST", "/login", data={"password": "2026"})
            self.assertEqual(status, 200)
            self.assertIn("パスワードが違います", body)
            status, headers, _body, _cookies = self.call("POST", "/login", data={"password": "secret-value"})
            self.assertEqual(status, 303)
            self.assertEqual(headers["location"], "/")


if __name__ == "__main__":
    unittest.main()
