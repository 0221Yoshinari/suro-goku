"""Playwright 共通: 人間相当の間隔、Stealth、永続プロファイル。"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import shutil
import subprocess
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()
from contextlib import asynccontextmanager
from pathlib import Path
from dataclasses import dataclass
from typing import Any, AsyncIterator

PROJECT_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
TERAMOBA_BROWSER_PROFILE = PROJECT_ROOT / "data" / "teramoba" / ".browser_profile"
LEGACY_TERAMOBA_PROFILE = PROJECT_ROOT / "data" / "teramoba" / ".auth" / "chrome-profile"
STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = window.chrome || { runtime: {}, loadTimes: function() {}, csi: function() {} };
Object.defineProperty(navigator, 'languages', { get: () => ['ja-JP', 'ja', 'en-US', 'en'] });
Object.defineProperty(navigator, 'platform', { get: () => 'MacIntel' });
Object.defineProperty(navigator, 'plugins', {
  get: () => [{ name: 'Chrome PDF Plugin' }, { name: 'Chrome PDF Viewer' }, { name: 'Native Client' }],
});
"""
CHROME_BINARIES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
)


def detect_chrome_version() -> tuple[str, str]:
    """インストール済み Google Chrome の major / full バージョン。"""
    override = (os.getenv("SCRAPER_CHROME_VERSION") or "").strip()
    if override:
        major = override.split(".", 1)[0]
        return major, override
    for binary in CHROME_BINARIES:
        if not Path(binary).is_file():
            continue
        try:
            out = subprocess.check_output([binary, "--version"], text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            continue
        match = re.search(r"(\d+)\.(\d+)\.(\d+)\.(\d+)", out)
        if match:
            full = match.group(0)
            return match.group(1), full
        match = re.search(r"(\d+)\.", out)
        if match:
            return match.group(1), f"{match.group(1)}.0.0.0"
    return "131", "131.0.0.0"


def chrome_client_identity() -> dict[str, str]:
    major, full = detect_chrome_version()
    ua = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
    )
    sec_ch_ua = f'"Google Chrome";v="{major}", "Chromium";v="{major}", "Not.A/Brand";v="24"'
    return {
        "major": major,
        "full": full,
        "user_agent": ua,
        "sec_ch_ua": sec_ch_ua,
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"macOS"',
    }


def teramoba_browser_profile() -> Path:
    dest = Path((os.getenv("TERAMOBA_BROWSER_PROFILE") or "").strip() or TERAMOBA_BROWSER_PROFILE)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists() and LEGACY_TERAMOBA_PROFILE.exists():
        def _ignore(directory: str, names: list[str]) -> list[str]:
            return [n for n in names if n.startswith("Singleton") or n in {"DevToolsActivePort", "lockfile"}]

        shutil.copytree(LEGACY_TERAMOBA_PROFILE, dest, ignore=_ignore)
        print(f"既存プロファイルを引き継ぎ: {LEGACY_TERAMOBA_PROFILE} → {dest}", flush=True)
    dest.mkdir(parents=True, exist_ok=True)
    return dest


async def hall_switch_pause(*, lo: float | None = None, hi: float | None = None) -> float:
    if lo is None and hi is None:
        from src.scraper.halls import pause_range

        lo, hi = pause_range()
    delay = random.uniform(lo if lo is not None else 1.0, hi if hi is not None else 3.0)
    print(f"  次店舗まで {delay:.1f}s 待機", flush=True)
    await asyncio.sleep(delay)
    return delay


def pause_range_seconds() -> tuple[float, float]:
    from src.scraper.halls import pause_range

    return pause_range()


async def human_pause(reason: str = "") -> float:
    """ページ遷移のあいだ。正常時は 1〜3 秒、遮断の兆候があるあいだはバックオフ。"""
    from src.scraper.pace import get_pacer

    delay = get_pacer().interval()
    label = f"（{reason}）" if reason else ""
    print(f"    {delay:.1f}秒待機{label}", flush=True)
    await asyncio.sleep(delay)
    return delay


async def short_pause() -> None:
    await asyncio.sleep(random.uniform(0.6, 1.4))


async def type_like_human(locator, text: str) -> None:
    await locator.click()
    await asyncio.sleep(random.uniform(0.2, 0.5))
    await locator.fill("")
    delay = random.randint(45, 95)
    await locator.type(text, delay=delay)


async def apply_stealth(page) -> str:
    """playwright-stealth があれば使い、無い場合も webdriver 隠蔽を入れる。"""
    plugin = "none"
    try:
        from playwright_stealth import stealth_async

        await stealth_async(page)
        plugin = "playwright-stealth"
    except Exception:
        try:
            from playwright_stealth import Stealth

            await Stealth().apply_stealth_async(page)
            plugin = "playwright-stealth-Stealth"
        except Exception:
            plugin = "init-script"
    try:
        await page.add_init_script(STEALTH_INIT_SCRIPT)
    except Exception:
        pass
    return plugin


def context_options(
    storage_state: Path | None = None,
    *,
    user_agent: str | None = None,
    native_chrome: bool = False,
) -> dict[str, Any]:
    identity = chrome_client_identity()
    ua = user_agent or os.getenv("SCRAPER_USER_AGENT") or identity["user_agent"]
    headers = {
        "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
        "sec-ch-ua": identity["sec_ch_ua"],
        "sec-ch-ua-mobile": identity["sec_ch_ua_mobile"],
        "sec-ch-ua-platform": identity["sec_ch_ua_platform"],
        "sec-ch-ua-full-version-list": (
            f'"Google Chrome";v="{identity["full"]}", "Chromium";v="{identity["full"]}", '
            '"Not.A/Brand";v="10.0.0.4"'
        ),
    }
    options: dict[str, Any] = {
        "locale": "ja-JP",
        "timezone_id": "Asia/Tokyo",
        "color_scheme": "light",
    }
    if native_chrome:
        # 公式 Chrome の UA / Client Hints / ウィンドウサイズをそのまま使う。
        options["no_viewport"] = True
        if user_agent:
            options["user_agent"] = user_agent
    else:
        options["extra_http_headers"] = headers
        options["viewport"] = {"width": 1440, "height": 900}
        options["screen"] = {"width": 1440, "height": 900}
        options["user_agent"] = ua or DEFAULT_USER_AGENT
    if storage_state is not None and storage_state.is_file():
        options["storage_state"] = str(storage_state)
    return options


SESSION_SNAPSHOT_NAME = "session_snapshot.json"


def session_snapshot_path(profile: Path) -> Path:
    return profile / SESSION_SNAPSHOT_NAME


async def save_session_snapshot(context, page, profile: Path) -> Path:
    """Cookie・localStorage・sessionStorage をプロファイル内へ書き出す。"""
    state = await context.storage_state()
    web = await page.evaluate(
        """() => ({
            origin: location.origin,
            href: location.href,
            userAgent: navigator.userAgent,
            viewport: { width: window.innerWidth, height: window.innerHeight },
            localStorage: Object.fromEntries(Object.entries(localStorage)),
            sessionStorage: Object.fromEntries(Object.entries(sessionStorage)),
        })"""
    )
    origin = str(web.get("origin") or "")
    payload = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "href": web.get("href"),
        "user_agent": web.get("userAgent"),
        "viewport": web.get("viewport"),
        "cookies": state.get("cookies") or [],
        "origins": state.get("origins") or [],
        "local_storage": [{"origin": origin, "items": web.get("localStorage") or {}}],
        "session_storage": [{"origin": origin, "items": web.get("sessionStorage") or {}}],
    }
    path = session_snapshot_path(profile)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    cookies = payload["cookies"]
    local_n = len((payload["local_storage"][0]["items"] or {}))
    session_n = len((payload["session_storage"][0]["items"] or {}))
    print(
        f"ストレージを保存: {path} / cookies {len(cookies)} / localStorage {local_n} / sessionStorage {session_n}",
        flush=True,
    )
    return path


def _storage_restore_script(payload: dict[str, Any]) -> str:
    pack = {
        "local_storage": payload.get("local_storage") or [],
        "session_storage": payload.get("session_storage") or [],
    }
    return (
        "(() => {\n"
        f"  const pack = {json.dumps(pack, ensure_ascii=False)};\n"
        "  const apply = (rows, store) => {\n"
        "    const origin = location.origin;\n"
        "    const hit = (rows || []).find((row) => row && (row.origin === origin || origin.endsWith(String(row.origin || '').replace(/^https?:\\/\\//, ''))));\n"
        "    if (!hit || !hit.items) return;\n"
        "    for (const [key, value] of Object.entries(hit.items)) {\n"
        "      try { store.setItem(key, String(value)); } catch (e) {}\n"
        "    }\n"
        "  };\n"
        "  try { apply(pack.local_storage, localStorage); } catch (e) {}\n"
        "  try { apply(pack.session_storage, sessionStorage); } catch (e) {}\n"
        "})();\n"
    )


async def install_session_restore(context, profile: Path) -> None:
    path = session_snapshot_path(profile)
    if not path.is_file():
        print("保存済みストレージスナップショットはありません", flush=True)
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ストレージスナップショットを読めません: {exc}", flush=True)
        return
    cookies = [item for item in (payload.get("cookies") or []) if isinstance(item, dict) and item.get("name")]
    if cookies:
        try:
            await context.add_cookies(cookies)
        except Exception as exc:
            print(f"Cookie の復元を一部スキップ: {exc}", flush=True)
    await context.add_init_script(_storage_restore_script(payload))
    print(
        f"ストレージを復元: cookies {len(cookies)} / UA {payload.get('user_agent') or 'プロファイル既定'}",
        flush=True,
    )


def proxy_settings() -> dict[str, str] | None:
    """Playwright 用プロキシ。環境変数が無ければ config/proxy_config.json を使う。"""
    from src.scraper.proxy_config import playwright_proxy

    return playwright_proxy()


def _proxy_log_label(proxy: dict[str, str]) -> str:
    from src.scraper.proxy_config import proxy_log_label

    return proxy_log_label(proxy)


@dataclass
class BrowserRun:
    context: Any
    page: Any
    save_storage: bool = False


_BLOCKED_TYPES = {"image", "stylesheet", "font", "media"}
_BLOCKED_URL_TOKENS = (
    "doubleclick",
    "googlesyndication",
    "google-analytics",
    "googletagmanager",
    "facebook.net",
    "adservice",
    "adsystem",
    "scorecardresearch",
)


async def block_heavy_routes(context) -> None:
    """画像・CSS・フォント・メディアと広告スクリプトを落とし、本文とAPIだけ読む。"""

    async def _handle(route) -> None:
        request = route.request
        url = request.url.casefold()
        if request.resource_type in _BLOCKED_TYPES or any(token in url for token in _BLOCKED_URL_TOKENS):
            await route.abort()
            return
        await route.continue_()

    await context.route("**/*", _handle)


@asynccontextmanager
async def browser_session(
    storage_state: Path | None = None,
    *,
    headless: bool | None = None,
    user_agent: str | None = None,
    user_data_dir: Path | None = None,
) -> AsyncIterator[BrowserRun]:
    from playwright.async_api import async_playwright

    if headless is None:
        headless = (os.getenv("SCRAPER_HEADLESS") or os.getenv("SITE7_HEADLESS") or "1") != "0"

    async with async_playwright() as playwright:
        print("ブラウザを起動中...", flush=True)
        identity = chrome_client_identity()
        # chromium_sandbox=True でないと Playwright が --no-sandbox を足し、
        # 公式 Chrome が「サポートされていないコマンドラインフラグ」を出して
        # Cloudflare Turnstile に Bot 判定される。
        launch_kwargs: dict[str, Any] = {
            "headless": headless,
            "chromium_sandbox": True,
            "args": [
                "--start-maximized",
            ],
            "ignore_default_args": [
                "--enable-automation",
                "--enable-unsafe-swiftshader",
            ],
        }
        channel = (os.getenv("PLAYWRIGHT_CHANNEL") or "").strip()
        if channel:
            launch_kwargs["channel"] = channel
            print(
                f"Chrome channel={channel} / UA Chrome/{identity['major']} / sec-ch-ua={identity['sec_ch_ua']}",
                flush=True,
            )
        proxy = proxy_settings()
        if proxy:
            launch_kwargs["proxy"] = proxy
            print(f"プロキシ: {_proxy_log_label(proxy)}", flush=True)

        stealth_announced = False
        native_chrome = bool(channel) and user_data_dir is not None

        async def _stealth_page(page) -> None:
            nonlocal stealth_announced
            if native_chrome:
                try:
                    await page.add_init_script(
                        "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
                    )
                except Exception:
                    pass
                if not stealth_announced:
                    print("Stealth: native-chrome (webdriver のみ隠蔽)", flush=True)
                    stealth_announced = True
                page.on("dialog", lambda dialog: asyncio.create_task(dialog.accept()))
                return
            plugin = await apply_stealth(page)
            page.on("dialog", lambda dialog: asyncio.create_task(dialog.accept()))
            if not stealth_announced:
                print(f"Stealth: {plugin}", flush=True)
                stealth_announced = True

        if user_data_dir is not None:
            user_data_dir.mkdir(parents=True, exist_ok=True)
            context_kwargs = context_options(None, user_agent=user_agent, native_chrome=True)
            print(f"永続プロファイル: {user_data_dir}", flush=True)
            context = await playwright.chromium.launch_persistent_context(
                str(user_data_dir),
                **launch_kwargs,
                **context_kwargs,
            )
            context.set_default_timeout(15000)
            context.set_default_navigation_timeout(15000)
            await block_heavy_routes(context)
            if not native_chrome:
                await context.add_init_script(STEALTH_INIT_SCRIPT)
            else:
                await context.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
                )
            await install_session_restore(context, user_data_dir)
            page = context.pages[0] if context.pages else await context.new_page()
            await _stealth_page(page)
            run = BrowserRun(context=context, page=page)
            browser = None
        else:
            browser = await playwright.chromium.launch(**launch_kwargs)
            context = await browser.new_context(**context_options(storage_state, user_agent=user_agent))
            context.set_default_timeout(15000)
            context.set_default_navigation_timeout(15000)
            await block_heavy_routes(context)
            await context.add_init_script(STEALTH_INIT_SCRIPT)
            page = await context.new_page()
            await _stealth_page(page)
            run = BrowserRun(context=context, page=page)
        try:
            yield run
        finally:
            if storage_state is not None and run.save_storage:
                storage_state.parent.mkdir(parents=True, exist_ok=True)
                await context.storage_state(path=str(storage_state))
                print(f"セッションを保存: {storage_state}", flush=True)
            await context.close()
            if browser is not None:
                await browser.close()
