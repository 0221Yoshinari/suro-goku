"""テラモバ2の初回規約同意を headed Playwright で通し、Cookie を保存する。"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

from src.scraper.browser import (
    browser_session,
    hall_switch_pause,
    save_session_snapshot,
    teramoba_browser_profile,
)
from src.scraper.halls import Hall, halls_for_source
from src.scraper.targets import is_watched
from src.scraper.teramoba import target_from_hall

AGREE_BUTTONS = (
    "#submittos",
    "button:text-is('同意する')",
    "text=利用規約に同意してはじめる",
    "text=利用規約に同意",
    "text=同意してはじめる",
)
AGREE_CHECKS = (
    "input[type='checkbox']",
    "label:has-text('同意')",
)


async def _click_agree(page) -> bool:
    try:
        clicked = await page.evaluate(
            """() => {
                const btn = document.querySelector('#submittos')
                    || [...document.querySelectorAll('button')].find((el) => (el.innerText || '').trim() === '同意する');
                if (!btn || btn.disabled) return false;
                btn.scrollIntoView({block: 'center', inline: 'nearest'});
                btn.click();
                return true;
            }"""
        )
    except Exception:
        clicked = False
    if clicked:
        print("  同意するを JS で押しました", flush=True)
        return True
    for selector in AGREE_CHECKS:
        loc = page.locator(selector)
        try:
            count = await loc.count()
        except Exception:
            continue
        for index in range(min(count, 6)):
            box = loc.nth(index)
            try:
                if await box.is_visible():
                    await box.click(timeout=1500)
                    print("  チェックボックスを押しました", flush=True)
            except Exception:
                continue
    for selector in AGREE_BUTTONS:
        loc = page.locator(selector)
        try:
            if await loc.count() == 0:
                continue
            target = loc.first
            if await target.is_visible():
                await target.click(timeout=2500)
                print(f"  同意ボタンを押しました ({selector})", flush=True)
                return True
        except Exception:
            continue
    return False


async def _challenge(page, hall_id: int) -> dict:
    return await page.evaluate(
        """async (hallId) => {
            const res = await fetch(`/n-api/common/captcha/challenge?hall_id=${hallId}`, {
                credentials: 'include',
                headers: { 'X-Requested-With': 'XMLHttpRequest' },
            });
            try { return await res.json(); } catch { return { ok: false, status: res.status }; }
        }""",
        hall_id,
    )


async def _machine_count(page, hall_id: int, kind_code: str) -> int:
    payload = await page.evaluate(
        """async ({ hallId, kind }) => {
            const res = await fetch(
                `/n-api/rack_info/search_kind?hall_id=${hallId}&kind_code=${encodeURIComponent(kind)}&place=`,
                { credentials: 'include', headers: { 'X-Requested-With': 'XMLHttpRequest' } },
            );
            try {
                const data = await res.json();
                return Array.isArray(data) ? data.length : 0;
            } catch {
                return 0;
            }
        }""",
        {"hallId": hall_id, "kind": kind_code},
    )
    return int(payload or 0)


async def _has_agree_cookie(context, hall_id: int) -> bool:
    name = f"agreeServiceTerm[{hall_id}]"
    cookies = await context.cookies()
    return any(item.get("name") == name and str(item.get("value")) == "true" for item in cookies)


async def _click_turnstile(page) -> bool:
    for frame in page.frames:
        url = frame.url or ""
        if "challenges.cloudflare.com" not in url and "turnstile" not in url:
            continue
        for selector in ("input[type='checkbox']", "body"):
            try:
                loc = frame.locator(selector).first
                await loc.click(timeout=2000)
                print("  Turnstile 枠をクリックしました", flush=True)
                return True
            except Exception:
                continue
    loc = page.locator("iframe[src*='turnstile'], iframe[src*='challenges.cloudflare.com']")
    try:
        if await loc.count():
            await loc.first.click(timeout=2000)
            print("  Turnstile iframe をクリックしました", flush=True)
            return True
    except Exception:
        pass
    return False


async def _dom_machine_count(page) -> int:
    try:
        return await page.locator("tr.teram2-link-block").count()
    except Exception:
        return 0


async def _wait_unlocked(page, context, hall_id: int, kind_code: str, wait_sec: float) -> bool:
    deadline = time.monotonic() + max(5.0, wait_sec)
    last_verify = None
    clicked_agree = False
    last_cf = 0.0
    print("  画面で利用規約に同意し、CAPTCHA が出たらチェックを入れてください。", flush=True)
    print("  台一覧（DOM）が表示されるまで待ちます。", flush=True)
    while time.monotonic() < deadline:
        rows = await _dom_machine_count(page)
        if rows > 0:
            print(f"  解除確認: DOM 機種行 {rows}", flush=True)
            return True
        try:
            count = await _machine_count(page, hall_id, kind_code)
        except Exception:
            count = 0
        if count > 0:
            print(f"  解除確認: API 機種 {count}", flush=True)
            return True
        if not clicked_agree:
            clicked_agree = await _click_agree(page)
        now = time.monotonic()
        if now - last_cf > 6:
            if await _click_turnstile(page):
                last_cf = now
        try:
            challenge = await _challenge(page, hall_id)
        except Exception:
            challenge = {}
        should_verify = bool(isinstance(challenge, dict) and challenge.get("should_verify"))
        if should_verify != last_verify:
            print(f"  captcha should_verify={should_verify}", flush=True)
            last_verify = should_verify
        remaining = int(deadline - time.monotonic())
        if remaining in (120, 90, 60, 30, 15):
            print(f"  同意完了を待っています（残り約{remaining}秒）", flush=True)
        await page.wait_for_timeout(1200)
    return (await _dom_machine_count(page)) > 0


async def _first_watched_name(page) -> str:
    rows = await page.evaluate(
        """() => [...document.querySelectorAll('tr.teram2-link-block')].map((tr) =>
            (tr.querySelector('.wrap-machine-name')?.innerText || '').trim()
        ).filter(Boolean)"""
    )
    for name in rows or []:
        if is_watched(str(name)):
            return str(name)
    return ""


async def _establish_detail_session(page, context, hall: Hall, *, wait_sec: float) -> bool:
    """一覧の次に1機種目の protection_redirect を開き、台番号が出るまで手動同意を待つ。"""
    target = target_from_hall(hall)
    if await page.locator("tr.teram2-link-block").count() == 0:
        await page.goto(target.list_url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(800)
    name = await _first_watched_name(page)
    if not name:
        print("  監視対象の1機種目が見つかりません", flush=True)
        return False
    print(f"\n===== 詳細認証: {hall.name} / {name} =====", flush=True)
    row = page.locator("tr.teram2-link-block").filter(has=page.get_by_text(name, exact=True))
    if await row.count() == 0:
        print("  機種行をクリックできません", flush=True)
        return False
    await row.first.click()
    await page.wait_for_timeout(1200)
    print(f"  開いた: {page.url}", flush=True)
    print("  Turnstile の「私はロボットではありません」にチェックを入れ、「同意する」を押してください。", flush=True)
    print("  台番号の表が出るまで、この画面のまま待ちます。", flush=True)
    deadline = time.monotonic() + max(30.0, wait_sec)
    while time.monotonic() < deadline:
        if await page.locator("th", has_text="台番号").count():
            from src.scraper.teramoba_browser import _parse_standlist

            units = await _parse_standlist(page)
            print(f"  詳細通過: {name} / {len(units)} 台", flush=True)
            if units:
                sample = units[0]
                print(
                    "  サンプル: "
                    f"台番号={sample.get('rack_no')} BB={sample.get('bonus_1')} "
                    f"RB={sample.get('bonus_2')} ART={sample.get('bonus_3')} "
                    f"総回転数={sample.get('all_game_count')}",
                    flush=True,
                )
            await save_session_snapshot(context, page, teramoba_browser_profile())
            return True
        remaining = int(deadline - time.monotonic())
        if remaining in (120, 90, 60, 30, 15):
            print(f"  詳細の同意を待っています（残り約{remaining}秒 / {page.url}）", flush=True)
        await page.wait_for_timeout(1000)
    print("  制限時間内に台詳細へ進めませんでした", flush=True)
    return False


async def unlock_hall(page, context, hall: Hall, *, wait_sec: float) -> bool:
    target = target_from_hall(hall)
    print(f"\n===== 規約同意: {hall.name} ({target.department}) =====", flush=True)
    print(f"  開く: {target.list_url}", flush=True)
    await page.goto(target.list_url, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(1200)
    hall_id = target.hall_id
    if not hall_id:
        match = re.search(r'name="hall_id" content=(\d+)', await page.content())
        hall_id = int(match.group(1)) if match else None
    if not hall_id:
        print("  hall_id を特定できませんでした", flush=True)
        return False
    if await _wait_unlocked(page, context, hall_id, target.kind_code, wait_sec):
        return await _establish_detail_session(page, context, hall, wait_sec=wait_sec)
    print("  制限時間内に台一覧の DOM を確認できませんでした", flush=True)
    frames = [frame.url for frame in page.frames]
    print(f"  frames: {frames}", flush=True)
    shot = Path("data/teramoba/debug-last-hall.png")
    shot.parent.mkdir(parents=True, exist_ok=True)
    try:
        await page.screenshot(path=str(shot), full_page=True)
        print(f"  画面を保存: {shot}", flush=True)
    except Exception as exc:
        print(f"  画面保存に失敗: {exc}", flush=True)
    return False


async def refresh_teramoba_session(
    *,
    wait_sec: float = 120.0,
    halls: list[Hall] | None = None,
    skip_unlocked: bool = True,
) -> Path:
    targets = halls if halls is not None else halls_for_source("teramoba")
    if not targets:
        raise RuntimeError("source=teramoba の有効ホールがありません")
    profile = teramoba_browser_profile()
    os.environ.setdefault("PLAYWRIGHT_CHANNEL", "chrome")
    print("headed ブラウザでテラモバ2の規約同意を順に通します。", flush=True)
    print("CAPTCHA が出た店舗は、画面上でチェックを完了してください。", flush=True)
    print(f"永続プロファイル: {profile}", flush=True)

    ok: list[str] = []
    failed: list[str] = []
    skipped: list[str] = []
    async with browser_session(headless=False, user_data_dir=profile) as session:
        page = session.page
        context = session.context
        for index, hall in enumerate(targets, start=1):
            target = target_from_hall(hall)
            print(f"\n[{index}/{len(targets)}] {hall.name}", flush=True)
            if skip_unlocked:
                try:
                    await page.goto(target.list_url, wait_until="domcontentloaded", timeout=60000)
                    await page.wait_for_timeout(800)
                    rows = await _dom_machine_count(page)
                    if rows > 0:
                        print(f"  一覧は解除済み（DOM 機種行 {rows}）。詳細認証へ進みます", flush=True)
                        if await _establish_detail_session(page, context, hall, wait_sec=wait_sec):
                            skipped.append(hall.name)
                        else:
                            failed.append(hall.name)
                        if index < len(targets):
                            await hall_switch_pause()
                        continue
                except Exception as exc:
                    print(f"  事前確認をスキップして同意画面へ: {exc}", flush=True)
            if await unlock_hall(page, context, hall, wait_sec=wait_sec):
                ok.append(hall.name)
            else:
                failed.append(hall.name)
            if index < len(targets):
                await hall_switch_pause()

    print(
        f"\n規約同意完了: 成功 {len(ok)} / 既存解除 {len(skipped)} / 失敗 {len(failed)} / 対象 {len(targets)}",
        flush=True,
    )
    if failed:
        print("失敗: " + " / ".join(failed), flush=True)
    return profile
