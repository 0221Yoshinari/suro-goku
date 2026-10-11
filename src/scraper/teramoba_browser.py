"""認証済み Playwright セッション上でテラモバ2の台データを DOM / ページ内 fetch から取得する。"""

from __future__ import annotations

import asyncio
import os
import re
import time
import urllib.parse
from html import unescape
from typing import Any

from src.scraper.browser import browser_session, hall_switch_pause, teramoba_browser_profile
from src.scraper.halls import Hall
from src.scraper.targets import is_watched
from src.scraper.teramoba import (
    INERTIA_PAGE_RE,
    TeramobaError,
    TeramobaMaintenance,
    _maybe_decrypt,
    _normalize_unit,
    is_maintenance_page,
    target_from_hall,
)
from src.scraper.teramoba_session import _click_agree, _click_turnstile

LIST_WAIT_MS = int(float(os.getenv("TERAMOBA_LIST_WAIT_SEC") or "90") * 1000)
STAND_WAIT_MS = int(float(os.getenv("TERAMOBA_STAND_WAIT_SEC") or "25") * 1000)
UNLOCK_WAIT_MS = int(float(os.getenv("TERAMOBA_UNLOCK_WAIT_SEC") or "90") * 1000)
LIST_READY = ".wrap-machine-name"
STAND_READY = "th:has-text('台番号')"
GATE_READY = f"{STAND_READY}, #submittos, iframe[src*='challenges.cloudflare.com']"


def _browser_headless() -> bool:
    return (os.getenv("TERAMOBA_BROWSER_HEADLESS") or os.getenv("SCRAPER_HEADLESS") or "0") == "1"


async def _turnstile_ready(page) -> bool:
    """同意ボタンは Turnstile トークンが入ってから押す。空のまま押すと「認証中…」で止まる。"""
    try:
        return bool(
            await page.evaluate(
                """() => {
                    const frame = document.querySelector("iframe[src*='challenges.cloudflare.com'], iframe[src*='turnstile']");
                    if (!frame) return true;
                    const token = document.querySelector('[name="cf-turnstile-response"]');
                    return !!(token && String(token.value || '').length > 0);
                }"""
            )
        )
    except Exception:
        return False


async def _js_click_agree(page) -> bool:
    """画面外にはみ出した「同意する」も、スクロール後に DOM から直接押す。"""
    if not await _turnstile_ready(page):
        return False
    try:
        clicked = await page.evaluate(
            """() => {
                const btn = document.querySelector('#submittos')
                    || [...document.querySelectorAll('button')].find((el) => (el.innerText || '').trim() === '同意する');
                if (!btn || btn.disabled) return false;
                const label = (btn.innerText || '').trim();
                if (label.includes('認証中')) return false;
                btn.scrollIntoView({block: 'center', inline: 'nearest'});
                btn.click();
                return true;
            }"""
        )
    except Exception:
        return False
    if clicked:
        print("  同意するを JS で押しました", flush=True)
    return bool(clicked)


async def _dismiss_protection(page) -> None:
    if await _js_click_agree(page):
        return
    if "protection_redirect" not in (page.url or "") and "利用規約" not in ((await page.title()) or ""):
        return
    await _click_agree(page)


async def _await_selector(page, selector: str, timeout_ms: int) -> bool:
    try:
        await page.wait_for_selector(selector, timeout=timeout_ms, state="visible")
        return True
    except Exception:
        return False


async def _unlock_until_stand(page, timeout_ms: int) -> bool:
    """台詳細テーブルが出るまで、同意と Turnstile 枠だけをイベントで待つ。"""
    if await _await_selector(page, STAND_READY, 400):
        return True
    deadline = time.monotonic() + max(1.0, timeout_ms / 1000)
    clicked_agree = False
    while time.monotonic() < deadline:
        remain_ms = max(1, int((deadline - time.monotonic()) * 1000))
        if await _await_selector(page, STAND_READY, min(remain_ms, 2000)):
            return True
        if not clicked_agree:
            clicked_agree = await _js_click_agree(page)
            if not clicked_agree and "protection_redirect" in (page.url or ""):
                state = await page.evaluate(
                    """() => {
                        const btn = document.querySelector('#submittos');
                        return btn ? {disabled: !!btn.disabled, text: (btn.innerText || '').trim()} : null;
                    }"""
                )
                print(f"  同意ボタン待機: {state}", flush=True)
        elif await page.locator("iframe[src*='challenges.cloudflare.com'], iframe[src*='turnstile']").count():
            await _click_turnstile(page)
            if await _await_selector(page, STAND_READY, min(remain_ms, 20000)):
                return True
            clicked_agree = True
    return await _await_selector(page, STAND_READY, 500)


async def _machine_rows(page) -> list[dict[str, str]]:
    return await page.evaluate(
        """() => [...document.querySelectorAll('tr.teram2-link-block')].map((tr) => ({
            name: (tr.querySelector('.wrap-machine-name')?.innerText || '').trim(),
            units: (tr.querySelector('#unit')?.parentElement?.innerText || '').trim(),
        })).filter((row) => row.name)"""
    )


async def _apply_turnstile_token(page, target) -> bool:
    """CapSolver のトークンを Cookie・ヘッダー・検証APIへ載せてから描画を待つ。"""
    hall_id = int(target.hall_id or 0)
    if not hall_id:
        return False
    try:
        challenge = await page.evaluate(
            """async (hallId) => {
                const res = await fetch(`/n-api/common/captcha/challenge?hall_id=${hallId}`, {
                    credentials: 'include',
                    headers: { 'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json' },
                });
                try { return await res.json(); } catch { return null; }
            }""",
            hall_id,
        )
    except Exception:
        challenge = None
    providers = challenge.get("providers") if isinstance(challenge, dict) else None
    site_key = ""
    provider = "turnstile"
    for item in providers or []:
        if isinstance(item, dict) and item.get("site_key"):
            site_key = str(item["site_key"])
            provider = str(item.get("provider") or provider)
            if item.get("provider") == "turnstile":
                break
    if not site_key:
        return False
    from src.scraper.capsolver import CapSolverError, solve_turnstile

    print(f"  ブラウザで CapSolver トークンを適用します hall_id={hall_id}", flush=True)
    try:
        token = await asyncio.to_thread(
            solve_turnstile, target.list_url, site_key, action="scraping_protection"
        )
    except CapSolverError as exc:
        print(f"  CapSolver では確認を解けませんでした: {exc}", flush=True)
        return False
    origin = target.base_url.rstrip("/")
    try:
        await page.context.add_cookies(
            [
                {
                    "name": "cf-turnstile-response",
                    "value": token,
                    "url": origin + "/",
                }
            ]
        )
    except Exception as exc:
        print(f"  Cookie のブラウザ登録をページ内セットに切り替えます: {exc}", flush=True)
    await page.set_extra_http_headers({"cf-turnstile-response": token})
    await page.evaluate(
        """async ({ token, hallId, provider }) => {
            const input = document.querySelector('[name="cf-turnstile-response"]');
            if (input) input.value = token;
            document.cookie = `cf-turnstile-response=${token}; path=/`;
            if (typeof window.__tsCallback === 'function') window.__tsCallback(token);
            const xsrf = (document.cookie.split(';').map((part) => part.trim()).find((part) => part.startsWith('XSRF-TOKEN=')) || '').slice('XSRF-TOKEN='.length);
            await fetch('/n-api/common/captcha/verify', {
                method: 'POST',
                credentials: 'include',
                headers: {
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                    'X-Requested-With': 'XMLHttpRequest',
                    'cf-turnstile-response': token,
                    ...(xsrf ? { 'X-XSRF-TOKEN': decodeURIComponent(xsrf) } : {}),
                },
                body: JSON.stringify({ hall_id: hallId, provider, token }),
            });
        }""",
        {"token": token, "hallId": hall_id, "provider": provider},
    )
    print("  cf-turnstile-response を Cookie とリクエストヘッダーへセットしました", flush=True)
    await page.wait_for_timeout(3000)
    return True


async def _wait_machine_list(page, target, *, timeout_ms: int) -> bool:
    await _dismiss_protection(page)
    try:
        await page.wait_for_selector("tr.teram2-link-block", timeout=min(timeout_ms, 8000))
        return True
    except Exception:
        pass
    await _apply_turnstile_token(page, target)
    try:
        await page.wait_for_selector("tr.teram2-link-block", timeout=timeout_ms)
        return True
    except Exception:
        return False


async def _parse_standlist(page) -> list[dict[str, Any]]:
    await page.wait_for_selector("th:has-text('台番号')", timeout=20000)
    payload = await page.evaluate(
        """() => {
            const table = document.querySelector('table');
            if (!table) return { headers: [], rows: [] };
            const headers = [...table.querySelectorAll('th')].map((th) => th.innerText.replace(/\\s+/g, ' ').trim());
            const rows = [...table.querySelectorAll('tr')]
                .map((tr) => [...tr.querySelectorAll('td')].map((td) => td.innerText.replace(/\\s+/g, ' ').trim()))
                .filter((row) => row.length >= 2);
            return { headers, rows };
        }"""
    )
    headers = [str(h) for h in (payload.get("headers") or [])]
    index = {name: i for i, name in enumerate(headers)}
    units: list[dict[str, Any]] = []
    for row in payload.get("rows") or []:
        cells = [str(c) for c in row]
        def col(*names: str) -> str:
            for name in names:
                pos = index.get(name)
                if pos is not None and pos < len(cells):
                    return cells[pos]
            return ""
        rack = col("台番号")
        if not rack or not re.search(r"\d", rack):
            continue
        units.append(
            {
                "rack_no": rack,
                "bonus_1": col("BB"),
                "bonus_2": col("RB"),
                "bonus_3": col("ART"),
                "all_game_count": col("総回転数", "総ゲーム"),
            }
        )
    return units


async def _return_to_list(page, list_url: str) -> None:
    await page.goto(list_url, wait_until="domcontentloaded", timeout=60000)
    await _await_selector(page, LIST_READY, 20000)


async def _open_machine(page, machine_name: str, list_url: str, *, timeout_ms: int) -> list[dict[str, Any]]:
    row = page.locator("tr.teram2-link-block").filter(
        has=page.get_by_text(machine_name, exact=True)
    )
    if await row.count() == 0:
        return []
    await row.first.click()
    if not await _unlock_until_stand(page, timeout_ms):
        await _return_to_list(page, list_url)
        return []
    try:
        units = await _parse_standlist(page)
    except Exception:
        units = []
    await _return_to_list(page, list_url)
    return units


async def _page_json(page, path: str) -> Any:
    return await page.evaluate(
        """async (path) => {
            const res = await fetch(path, {
                credentials: 'include',
                headers: { 'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json' },
            });
            const text = await res.text();
            try { return { status: res.status, data: JSON.parse(text) }; }
            catch { return { status: res.status, data: null }; }
        }""",
        path,
    )


async def _page_text(page, url: str) -> str:
    payload = await page.evaluate(
        """async (url) => {
            const res = await fetch(url, { credentials: 'include' });
            return await res.text();
        }""",
        url,
    )
    return str(payload or "")


def _inertia_props(html: str) -> dict[str, Any]:
    match = INERTIA_PAGE_RE.search(html)
    if not match or "protection_redirect" in html:
        return {}
    import json

    payload = json.loads(unescape(match.group(1)))
    props = payload.get("props") if isinstance(payload, dict) else None
    return props if isinstance(props, dict) else {}


async def _capture_session(context) -> None:
    cookies = await context.cookies()
    names = [
        str(item.get("name") or "")
        for item in cookies
        if any(token in str(item.get("name") or "").lower() for token in ("agree", "cf_", "turnstile", "captcha"))
    ]
    print(f"  セッションCookieを保持: {', '.join(names) or '同意系の名前なし（同一ブラウザで継続）'}", flush=True)


async def _units_via_api(page, hall: Hall, target, models: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """最初の認証で付いた Cookie のまま、監視対象の台一覧 API を続ける。"""
    if not target.hall_id or not models:
        return None
    first = models[0]
    label = str(first.get("machine_name") or "")
    enc = str(first.get("machine_name_enc") or urllib.parse.quote(label))
    stand_url = (
        f"{target.base_url.rstrip('/')}/{target.department}/standlist_slot/"
        f"?kind_code={urllib.parse.quote(target.kind_code)}&machine_name={enc}"
    )
    props = _inertia_props(await _page_text(page, stand_url))
    if not props:
        print("  API再利用は保護ページのままのため、クリック取得を続けます", flush=True)
        return None
    data = props.get("data") if isinstance(props.get("data"), dict) else {}
    key = str(data.get("key") or "") or None
    iv = str(data.get("iv") or "") or None
    latest = str(props.get("latest_date") or "")
    info = props.get("hall_informations") if isinstance(props.get("hall_informations"), dict) else {}
    history_day = int(info.get("history_day") or 7)
    if not latest:
        return None
    print(f"  認証済みセッションで API 取得（対象日 {latest}）", flush=True)
    machines: list[dict[str, Any]] = []
    for index, model in enumerate(models, start=1):
        name = str(model.get("machine_name") or "")
        machine_enc = str(model.get("machine_name_enc") or urllib.parse.quote(name))
        print(f"  [{index}/{len(models)}] {name}", flush=True)
        path = (
            f"/n-api/rack_info/machine_list?hall_id={target.hall_id}"
            f"&kind_code={urllib.parse.quote(target.kind_code)}"
            f"&machine_name={machine_enc}"
            f"&target_date={urllib.parse.quote(latest)}"
            f"&disp=2&place=&history_day={history_day}"
        )
        try:
            payload = await _page_json(page, path)
            body = payload.get("data") if isinstance(payload, dict) else None
            decrypted = _maybe_decrypt(body, key, iv)
        except Exception as exc:
            print(f"    API取得失敗: {exc}", flush=True)
            continue
        if not isinstance(decrypted, dict):
            print("    APIの台一覧が空です", flush=True)
            continue
        rows = []
        for key_name, unit in decrypted.items():
            if key_name == "sum" or not isinstance(unit, dict):
                continue
            unit = dict(unit)
            unit.setdefault("machine_name", name)
            row = _normalize_unit(unit, hall.name, None)
            if row:
                rows.append(row)
        print(f"    取得 {len(rows)} 台", flush=True)
        machines.extend(rows)
    return machines


def _rows_from_dom(units: list[dict[str, Any]], hall_name: str, machine_name: str) -> list[dict[str, Any]]:
    rows = []
    for unit in units:
        unit["machine_name"] = machine_name
        row = _normalize_unit(unit, hall_name, None)
        if row:
            rows.append(row)
    return rows


async def fetch_hall_via_browser(page, hall: Hall) -> list[dict[str, Any]]:
    target = target_from_hall(hall)
    started = time.monotonic()
    print(f"  Playwright でページ内取得: {target.list_url}", flush=True)
    await page.goto(target.list_url, wait_until="domcontentloaded", timeout=60000)
    html = await page.content()
    if is_maintenance_page(html):
        print("  店舗ステータス：メンテナンス中（CapSolver は使いません）", flush=True)
        raise TeramobaMaintenance("店舗ステータス：メンテナンス中")
    if not await _wait_machine_list(page, target, timeout_ms=LIST_WAIT_MS):
        body = ""
        try:
            body = await page.inner_text("body")
        except Exception:
            pass
        if "公開中の台データ情報はありません" in body or "protection_redirect" in (page.url or ""):
            raise TeramobaError("ブラウザ上でも台一覧が表示されません（Turnstile 未通過の可能性）")
        raise TeramobaError("機種一覧の DOM を取得できませんでした")

    catalog = await _machine_rows(page)
    watched = [item for item in catalog if is_watched(item.get("name"))]
    print(
        f"  DOM機種数: {len(catalog)} / 監視対象を優先: {len(watched)}（除外 {len(catalog) - len(watched)}）",
        flush=True,
    )
    if not watched:
        return []

    first_name = str(watched[0].get("name") or "")
    print(f"  [1/{len(watched)}] {first_name}（認証）", flush=True)
    try:
        first_units = await _open_machine(page, first_name, target.list_url, timeout_ms=UNLOCK_WAIT_MS)
    except Exception as exc:
        print(f"    一覧を開けませんでした: {exc}", flush=True)
        first_units = []
    machines = _rows_from_dom(first_units, hall.name, first_name)
    print(f"    取得 {len(machines)} 台", flush=True)
    if not machines:
        elapsed = time.monotonic() - started
        print(f"  所要 {elapsed:.1f} 秒 / 台データ 0 件", flush=True)
        return []

    await _capture_session(page.context)
    api_models: list[dict[str, Any]] | None = None
    if target.hall_id:
        index_payload = await _page_json(
            page,
            f"/n-api/rack_info/search_kind?hall_id={target.hall_id}"
            f"&kind_code={urllib.parse.quote(target.kind_code)}&place=",
        )
        index_rows = index_payload.get("data") if isinstance(index_payload, dict) else None
        if isinstance(index_rows, list):
            by_name = {
                str(row.get("machine_name") or ""): row
                for row in index_rows
                if isinstance(row, dict)
            }
            api_models = [by_name[str(item.get("name") or "")] for item in watched if str(item.get("name") or "") in by_name]
    reused = await _units_via_api(page, hall, target, api_models or []) if api_models else None
    if reused:
        elapsed = time.monotonic() - started
        print(f"  所要 {elapsed:.1f} 秒 / 台データ {len(reused)} 件", flush=True)
        return reused

    for index, item in enumerate(watched[1:], start=2):
        name = str(item.get("name") or "")
        print(f"  [{index}/{len(watched)}] {name}", flush=True)
        try:
            units = await _open_machine(page, name, target.list_url, timeout_ms=STAND_WAIT_MS)
        except Exception as exc:
            print(f"    一覧を開けませんでした: {exc}", flush=True)
            continue
        rows = _rows_from_dom(units, hall.name, name)
        print(f"    取得 {len(rows)} 台", flush=True)
        machines.extend(rows)
    elapsed = time.monotonic() - started
    print(f"  所要 {elapsed:.1f} 秒 / 台データ {len(machines)} 件", flush=True)
    return machines


async def fetch_halls_via_browser(halls: list[Hall]) -> dict[str, list[dict[str, Any]] | Exception]:
    if not halls:
        return {}
    os.environ.setdefault("PLAYWRIGHT_CHANNEL", "chrome")
    profile = teramoba_browser_profile()
    headless = _browser_headless()
    print(
        f"Playwright 取得を開始（{'headless' if headless else 'headed'} / {len(halls)} 店舗 / プロファイル {profile}）",
        flush=True,
    )
    results: dict[str, list[dict[str, Any]] | Exception] = {}
    async with browser_session(headless=headless, user_data_dir=profile) as session:
        for index, hall in enumerate(halls, start=1):
            print(f"\n----- ブラウザ取得 [{index}/{len(halls)}] {hall.name} -----", flush=True)
            try:
                results[hall.name] = await fetch_hall_via_browser(session.page, hall)
            except Exception as exc:
                print(f"  {hall.name} のページ内取得失敗: {exc}", flush=True)
                results[hall.name] = exc
            if index < len(halls):
                await hall_switch_pause()
    return results
