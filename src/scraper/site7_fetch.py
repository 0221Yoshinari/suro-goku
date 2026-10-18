"""サイトセブン（データロボ）のライブ取得。セッション再利用と人間相当の間隔でアクセスする。"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dotenv import load_dotenv

from src.scraper.base import FetchResult
from src.scraper.log_safe import emit, failure_reason
from src.scraper.deadline import hall_timeout_sec
from src.scraper.browser import browser_session, human_pause, short_pause, type_like_human
from src.scraper.halls import (
    configured_halls,
    format_duration,
    hall_data_dir,
    hall_patterns,
    halls_for_source,
    max_workers,
    worker_user_agent,
)
from src.scraper.targets import is_watched, match_target

load_dotenv()

EMAIL = os.getenv("SITE7_EMAIL")
PASSWORD = os.getenv("SITE7_PASSWORD")
TARGET_URL = os.getenv("SITE7_HALL_URL")
USE_HALL_URL = (os.getenv("SITE7_USE_HALL_URL") or "").strip() in ("1", "true", "yes")
_LOGIN_LOCK = asyncio.Lock()

LOGIN_URL = "https://www.d-deltanet.com/pc/MypageLoginTop.do"
MYPAGE_URL = "https://www.d-deltanet.com/pc/PCCreditAuth.do?skskb=3"
BASE = "https://www.d-deltanet.com/pc/"
BUSY_WAIT_MS = int(os.getenv("SITE7_BUSY_WAIT_MS") or "8000")
BUSY_RETRIES = int(os.getenv("SITE7_BUSY_RETRIES") or "6")
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STORAGE_STATE = PROJECT_ROOT / "data" / "site7" / ".auth" / "storage_state.json"

NUM_RE = re.compile(r"[\d.]+")


class SessionExpired(RuntimeError):
    """ログイン画面へ戻った、またはホールデータに必要なセッションが切れた。"""


CAPTCHA_MSG = "【要対応】サイトセブン側でCAPTCHA認証が発生しました。ブラウザで認証を通してください"
_CAPTCHA_WAIT_SEC = 0.0


class CaptchaRequired(RuntimeError):
    """サイトセブン側の人間認証（CAPTCHA）が必要。"""

    def __init__(self, message: str = CAPTCHA_MSG) -> None:
        super().__init__(message)


def set_captcha_wait_sec(seconds: float) -> None:
    """CAPTCHA 検出時に手動解除を待つ秒数。0 なら即失敗。"""
    global _CAPTCHA_WAIT_SEC
    _CAPTCHA_WAIT_SEC = max(0.0, float(seconds))


def captcha_wait_sec() -> float:
    env_raw = (os.getenv("SITE7_CAPTCHA_WAIT_SEC") or "").strip()
    if env_raw:
        try:
            return max(0.0, float(env_raw))
        except ValueError:
            pass
    return _CAPTCHA_WAIT_SEC


def storage_state_path() -> Path:
    raw = os.getenv("SITE7_STORAGE_STATE")
    return Path(raw) if raw else DEFAULT_STORAGE_STATE


def _ratio(value):
    text = str(value).strip().replace(",", "")
    if not text or text in ("--", "-", "―"):
        return None
    if text.startswith("1/"):
        return text
    match = NUM_RE.search(text)
    return f"1/{match.group(0)}" if match else None


def _int(value):
    text = str(value).strip().replace(",", "").replace("▲", "-").replace("△", "+")
    if not text or text in ("--", "-", "―"):
        return None
    match = re.search(r"-?\d+", text)
    return int(match.group(0)) if match else None


async def accept_cookie(page):
    btn = page.locator("button:has-text('承諾する')")
    if await btn.count():
        try:
            await btn.first.click(timeout=3000)
        except Exception:
            pass


async def is_captcha(page) -> bool:
    try:
        text = await page.inner_text("body")
    except Exception:
        text = ""
    if "私はロボットではありません" in text or "ロボットではありません" in text:
        return True
    selectors = (
        "iframe[src*='recaptcha']",
        "iframe[title*='reCAPTCHA']",
        "iframe[src*='hcaptcha']",
        ".g-recaptcha",
        "#g-recaptcha",
        "text=私はロボットではありません",
    )
    for selector in selectors:
        try:
            if await page.locator(selector).count():
                return True
        except Exception:
            continue
    return False


async def wait_if_captcha(page, *, timeout_sec: float | None = None) -> bool:
    """CAPTCHA があればブラウザ操作を待つ。出現しなければ False。"""
    wait_sec = captcha_wait_sec() if timeout_sec is None else timeout_sec
    if wait_sec <= 0 or not await is_captcha(page):
        return False
    emit(
        f"CAPTCHA を検出。ブラウザで「私はロボットではありません」を解除してください（最大 {wait_sec:.0f} 秒）",
        flush=True,
    )
    deadline = time.monotonic() + wait_sec
    next_notice = time.monotonic()
    while time.monotonic() < deadline:
        now = time.monotonic()
        if now >= next_notice:
            emit(f"  残り {deadline - now:.0f} 秒。チェックボックスをクリックしてください。", flush=True)
            next_notice = now + 10
        await asyncio.sleep(2)
        if not await is_captcha(page):
            emit("CAPTCHA 解除を確認しました。", flush=True)
            await asyncio.sleep(1.5)
            return True
    emit(CAPTCHA_MSG, flush=True)
    raise CaptchaRequired()


async def guard_page(page) -> None:
    if await is_captcha(page):
        if captcha_wait_sec() > 0:
            await wait_if_captcha(page)
            if await is_captcha(page):
                emit(CAPTCHA_MSG, flush=True)
                raise CaptchaRequired()
        else:
            emit(CAPTCHA_MSG, flush=True)
            raise CaptchaRequired()
    if await is_account_restricted(page):
        raise RuntimeError(RESTRICTED_MSG)


async def is_busy(page):
    text = await page.inner_text("body")
    return "混み合" in text


async def is_account_restricted(page):
    text = await page.inner_text("body")
    needles = (
        "利用を一時的に停止",
        "禁止行為に該当",
        "自動取得ツール",
        "プログラムや自動取得",
        "識別番号",
    )
    return any(needle in text for needle in needles)


RESTRICTED_MSG = (
    "サイトセブンが自動取得を検知したため、ホールデータを表示していません。"
    "時間をおいて再実行してください。"
)


def _force_login() -> bool:
    return (os.getenv("SITE7_FORCE_LOGIN") or "").strip() in ("1", "true", "yes")


async def page_requires_login(page) -> bool:
    url = page.url or ""
    if "MypageLoginTop" in url:
        return True
    if await page.locator("#mypage_email").count():
        return True
    if await page.locator("#login-form").count() and await page.locator("#mypage_pass").count():
        return True
    return False


async def session_looks_logged_in(page) -> bool:
    """ログイン後のマイページは MypageLogin.do のことがある。フォーム有無とマイホールで判定する。"""
    if await page_requires_login(page):
        return False
    try:
        await page.locator("#myhall a").first.wait_for(timeout=8000)
    except Exception:
        return False
    return await page.locator("#myhall a").count() > 0


async def login(page):
    emit("サイトセブン ログインページへアクセス中...")
    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
    await accept_cookie(page)
    await human_pause("ログイン画面")
    await guard_page(page)
    await page.locator("#mypage_email").wait_for(timeout=15000)
    await type_like_human(page.locator("#mypage_email"), EMAIL or "")
    await short_pause()
    await type_like_human(page.locator("#mypage_pass"), PASSWORD or "")
    await short_pause()
    try:
        async with page.expect_navigation(timeout=30000):
            await page.locator("#login-form .btn-login a").click()
    except Exception:
        if await page.locator("#login-form .btn-login a").count():
            await page.locator("#login-form .btn-login a").click()
    await human_pause("ログイン送信後")
    await guard_page(page)
    await page.goto(MYPAGE_URL, wait_until="domcontentloaded", timeout=60000)
    await accept_cookie(page)
    await human_pause("ログイン後マイページ")
    await guard_page(page)
    if not await session_looks_logged_in(page):
        raise RuntimeError("ログインに失敗しました。メールアドレスまたはパスワードを確認してください。")
    emit(f"ログイン完了: {page.url}", flush=True)


async def ensure_session(page, storage_path: Path, *, allow_login: bool = True) -> None:
    """保存済みCookieでマイページへ直接入る。ログインはセッション切れ時のみ。"""
    if _force_login() or not storage_path.is_file():
        if not allow_login:
            raise SessionExpired("ログインが必要ですが、同時ログインを避けるためこのワーカーでは実行しません。")
        async with _LOGIN_LOCK:
            await login(page)
        return
    emit(f"保存済みセッションを使用: {storage_path}", flush=True)
    await page.goto(MYPAGE_URL, wait_until="domcontentloaded", timeout=60000)
    await accept_cookie(page)
    await human_pause("セッション確認")
    await guard_page(page)
    if await session_looks_logged_in(page):
        emit("セッション有効。再ログインを省略してマイページを利用します。", flush=True)
        return
    if await page_requires_login(page):
        if not allow_login:
            raise SessionExpired("セッション無効です。同時ログインを避けるためこのワーカーでは再ログインしません。")
        emit("セッション無効のため再ログインします。", flush=True)
        async with _LOGIN_LOCK:
            await login(page)
        return
    if not allow_login:
        raise SessionExpired("セッション無効です。同時ログインを避けるためこのワーカーでは再ログインしません。")
    emit("マイホールが見つからないため再ログインします。", flush=True)
    async with _LOGIN_LOCK:
        await login(page)


async def _try_open_slot_tab(page) -> None:
    if await page.locator("table.slot").count():
        return
    candidates = (
        page.locator("a").filter(has_text=re.compile(r"^スロット$")),
        page.locator("input[type='button'][value*='スロット']"),
        page.locator("input[type='submit'][value*='スロット']"),
    )
    for loc in candidates:
        if not await loc.count():
            continue
        try:
            await loc.first.click(timeout=3000)
            await short_pause()
            return
        except Exception:
            continue


async def _hall_debug(page) -> str:
    try:
        info = await page.evaluate(
            """() => ({
                slot: !!document.querySelector('table.slot'),
                listClick: typeof listClick,
                loginForm: !!document.querySelector('#mypage_email'),
            })"""
        )
        return f"slot={info.get('slot')} listClick={info.get('listClick')} loginForm={info.get('loginForm')}"
    except Exception:
        return "状態を確認できません"


async def wait_for_hall_ready(page, hall_url: str) -> bool:
    await guard_page(page)
    if await page_requires_login(page):
        raise SessionExpired("ホールページがログイン画面です")
    for attempt in range(1, BUSY_RETRIES + 1):
        await guard_page(page)
        if await page_requires_login(page):
            raise SessionExpired("ホール取得中にログイン画面へ戻りました")
        if "HallSelectLink" not in (page.url or "") or await is_busy(page):
            emit(f"    ホール再読込 ({attempt}/{BUSY_RETRIES})", flush=True)
            await human_pause("混雑待ち")
            await page.goto(hall_url, wait_until="domcontentloaded", timeout=60000)
            await human_pause("ホール再表示")
        await _try_open_slot_tab(page)
        try:
            await page.locator("table.slot").first.wait_for(state="visible", timeout=5000)
        except Exception:
            pass
        has_slot = await page.locator("table.slot").count()
        has_click = await page.evaluate("() => typeof listClick === 'function'")
        if has_slot and has_click and not await is_busy(page):
            return True
        emit(
            f"    ホール未準備 ({attempt}/{BUSY_RETRIES}) slot={has_slot} listClick={has_click}",
            flush=True,
        )
        await page.wait_for_timeout(BUSY_WAIT_MS)
        await page.goto(hall_url, wait_until="domcontentloaded", timeout=60000)
        await human_pause("ホール再表示")
    emit(f"    ホール一覧が開けません: {await _hall_debug(page)}", flush=True)
    return False


async def open_hall(page, hall_name: str | None = None):
    """マイホール一覧から指定ホールを開く。"""
    halls = configured_halls()
    target = (hall_name or (halls[0] if halls else "")).strip()
    if not target:
        raise RuntimeError("巡回対象ホールが config/halls.json にありません。")
    if USE_HALL_URL and TARGET_URL and len(configured_halls()) == 1:
        emit("ホールページへ移動中（SITE7_HALL_URL）...")
        await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=60000)
        await human_pause("ホール指定URL")
        if not await is_busy(page) and await page.locator("table.slot").count():
            return
        emit("指定URLは利用できないため、マイホールから開き直します。")

    await page.goto(MYPAGE_URL, wait_until="domcontentloaded")
    await human_pause("マイページ")
    await guard_page(page)
    try:
        await page.locator("#myhall").wait_for(timeout=20000)
    except Exception:
        pass
    match = (os.getenv("SITE7_HALL_MATCH") or target).strip()
    hall_link = None
    for pattern in (match, *hall_patterns(target)):
        loc = page.locator("#myhall a").filter(has_text=re.compile(re.escape(pattern)))
        if await loc.count():
            hall_link = loc
            break
    if hall_link is None or not await hall_link.count():
        hall_link = page.locator("a").filter(has_text=re.compile(re.escape(match)))
    if not await hall_link.count():
        emit("マイページからホールリンクを特定できませんでした", flush=True)
        raise RuntimeError(f"マイホールに「{match}」が見つかりません")
    href = await hall_link.first.get_attribute("href")
    if not href:
        raise RuntimeError(f"マイホール「{match}」のリンクが見つかりません。")
    url = href if href.startswith("http") else BASE + href
    emit(f"マイホールへ移動（{target}）: {url}")
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    await human_pause("ホール一覧")
    await guard_page(page)
    if await is_busy(page):
        raise RuntimeError("ホールページが混雑中です。しばらく待って再実行してください。")


EXTRACT_TABLE_JS = """(machineName) => {
    const norm = (s) => (s || '').replace(/\\s+/g, '').replace(/[Ａ-Ｚａ-ｚ０-９]/g, (ch) =>
        String.fromCharCode(ch.charCodeAt(0) - 0xFEE0)
    );
    const heading = (document.body.innerText.match(/(.+?)\\s*【\\d+】スロ/) || [])[1];
    const name = (heading || machineName || '').replace(/\\s+/g, ' ').trim();
    const isTodayTable = (table) => [...table.querySelectorAll('input[type="button"]')].some((el) => {
        const value = norm(el.value || '');
        const code = el.getAttribute('onclick') || '';
        return /累計ゲーム|BB回数|ART回数|AT回数/.test(value) && /,\\s*0\\s*\\)/.test(code);
    });
    const classify = (h) => {
        if (/累計ゲーム|総ゲーム/.test(h)) return 'games';
        if (/BB確率/.test(h)) return 'bbp';
        if (/RB確率|REG確率/.test(h)) return 'rbp';
        if (/ART確率|AT確率/.test(h)) return 'artp';
        if (/合成/.test(h)) return 'syn';
        if (/ART回数|AT回数|ART|初当/.test(h)) return 'art';
        if (/BB回数/.test(h) || (h.includes('BB') && !h.includes('確率'))) return 'bb';
        if (/RB回数|REG回数/.test(h) || (h.includes('RB') && !h.includes('確率'))) return 'rb';
        return null;
    };
    const tables = [...document.querySelectorAll('table')].filter((table) =>
        table.rows.length > 2 && table.querySelector('input[value="BB回数"], input[value="累計ゲーム"], input[value="ART回数"]')
    );
    const todayTables = tables.filter(isTodayTable);
    const targets = todayTables.length ? todayTables : tables.slice(0, 1);
    const rows = [];
    for (const table of targets) {
        const idx = {};
        const headerRow = table.rows[0];
        for (let i = 0; i < headerRow.cells.length; i++) {
            const cell = headerRow.cells[i];
            const inp = cell.querySelector('input');
            const h = norm((inp && inp.value) || cell.innerText || '');
            const key = classify(h);
            if (key && idx[key] == null) idx[key] = i;
        }
        for (const tr of [...table.rows].slice(1)) {
            const cells = [...tr.cells].map((c) => c.innerText.replace(/\\s+/g, ' ').trim());
            if (cells.length < 4) continue;
            const dai = (tr.querySelector('.num') || {}).innerText || cells[0];
            const at = (i) => (i != null && cells[i] != null) ? cells[i] : '';
            rows.push({
                machine_name: name,
                dai_no: (dai || '').trim(),
                total_games: at(idx.games),
                bb: at(idx.bb),
                rb: at(idx.rb),
                '合成確率': at(idx.syn),
                'BB確率': at(idx.bbp),
                'RB確率': at(idx.rbp),
                art: at(idx.art),
            });
        }
    }
    return rows;
}"""


async def extract_list_page(page, machine_name):
    return await page.evaluate(EXTRACT_TABLE_JS, machine_name)


async def reveal_art_counts(page) -> bool:
    """ART回数ヘッダをクリックし、初期表示の「--」を実数に切り替える。"""
    def _click():
        return page.evaluate(
            """() => {
                const buttons = [...document.querySelectorAll('table input[value="ART回数"], table input[value="AT回数"]')];
                const today = buttons.filter((el) => /,\\s*0\\s*\\)/.test(el.getAttribute('onclick') || ''));
                const target = (today[0] || buttons[0]);
                if (!target) return false;
                target.click();
                return true;
            }"""
        )

    try:
        async with page.expect_navigation(timeout=12000):
            clicked = await _click()
        if not clicked:
            return False
    except Exception:
        clicked = await _click()
        if not clicked:
            return False
    try:
        await page.wait_for_function(
            """() => {
                const table = [...document.querySelectorAll('table')].find((t) =>
                    t.rows.length > 2 && t.querySelector('input[value="ART回数"], input[value="AT回数"]')
                );
                if (!table) return false;
                const header = table.rows[0];
                let artIdx = -1;
                for (let i = 0; i < header.cells.length; i++) {
                    const inp = header.cells[i].querySelector('input');
                    const h = ((inp && inp.value) || header.cells[i].innerText || '');
                    if (/ART|AT回数|初当/.test(h)) artIdx = i;
                }
                if (artIdx < 0) return false;
                const val = ((table.rows[1].cells[artIdx] || {}).innerText || '').trim();
                return val !== '' && val !== '--';
            }""",
            timeout=12000,
        )
        return True
    except Exception:
        return False


def _normalize_rows(rows, label):
    out = []
    for row in rows:
        dai = str(row.get("dai_no") or "").strip()
        if dai in ("", "平均", "合計", "計"):
            continue
        out.append(
            {
                "machine_name": row.get("machine_name") or label,
                "dai_no": row.get("dai_no"),
                "total_games": _int(row.get("total_games")),
                "bb": _int(row.get("bb")),
                "rb": _int(row.get("rb")),
                "合成確率": _ratio(row.get("合成確率")),
                "BB確率": _ratio(row.get("BB確率")),
                "RB確率": _ratio(row.get("RB確率")),
                "art": _int(row.get("art")),
            }
        )
    return out


async def fetch_slot_machines(page):
    hall_url = page.url.split("#")[0]
    await guard_page(page)
    if await page_requires_login(page):
        raise SessionExpired("スロット一覧の取得前にログイン画面へ戻りました")
    if not await wait_for_hall_ready(page, hall_url):
        raise RuntimeError("ホールのスロット一覧を開けませんでした。")
    await page.evaluate(
        """() => {
            const th = document.querySelector('table.slot th[id]');
            if (th) location.hash = '#' + th.id;
        }"""
    )
    await short_pause()
    models = await page.evaluate(
        """() => [...document.querySelectorAll('table.slot input[name=select]')].map(el => {
            const span = el.closest('td') && el.closest('td').querySelector('span');
            return {
                onclick: el.getAttribute('onclick') || '',
                label: ((span && span.innerText) || '').replace(/（\\d+）/, '').trim()
            };
        })"""
    )
    max_models = int(os.getenv("SITE7_MAX_MODELS") or "0")
    if max_models > 0:
        models = models[:max_models]
    watched_models = [m for m in models if is_watched(m.get("label"))]
    only = (os.getenv("SITE7_ONLY_PATTERN") or "").strip()
    if only:
        watched_models = [m for m in watched_models if only in (m.get("label") or "")]
        emit(f"SITE7_ONLY_PATTERN={only} → {len(watched_models)} 機種", flush=True)
    skipped = len(models) - len(watched_models)
    emit(f"スロット機種数: {len(models)} / 監視対象: {len(watched_models)}（除外 {skipped}）", flush=True)
    machines = []

    for i, model in enumerate(watched_models):
        label = model["label"]
        emit(f"  [{i + 1}/{len(watched_models)}] {label}", flush=True)
        if not await wait_for_hall_ready(page, hall_url):
            emit("    ホールページが復帰しないためスキップ", flush=True)
            continue

        await human_pause("機種クリック前")
        try:
            async with page.expect_navigation(timeout=25000):
                await page.evaluate("(code) => { eval(code); }", model["onclick"])
            await human_pause("機種データ表示")
            await page.locator(
                'input[value="累計ゲーム"], input[value="BB回数"], input[value="ART回数"]'
            ).first.wait_for(timeout=20000)
            await guard_page(page)
        except Exception as e:
            emit(f"    一覧を開けませんでした: {failure_reason(e)}", flush=True)
            await wait_for_hall_ready(page, hall_url)
            continue

        if await is_busy(page):
            emit("    混雑のためスキップ", flush=True)
        else:
            watched = match_target(label)
            if watched and watched.type == "AT":
                await human_pause("ART列クリック前")
                loaded = await reveal_art_counts(page)
                emit(f"    ART列: {'取得' if loaded else '未表示（--の可能性）'}", flush=True)
                await human_pause("ART列表示後")
            rows = _normalize_rows(await extract_list_page(page, label), label)
            machines.extend(rows)
            art_n = sum(1 for r in rows if r.get("art") is not None)
            emit(f"    取得 {len(rows)} 台 / ARTあり {art_n}", flush=True)

        await human_pause("ホールへ戻る前")
        await page.goto(hall_url, wait_until="domcontentloaded", timeout=60000)
        await human_pause("ホール一覧復帰")

    return machines


async def _relogin(page, storage_path: Path) -> None:
    emit("ログイン状態が外れているため再ログインし、Cookie を更新します。", flush=True)
    async with _LOGIN_LOCK:
        await login(page)
        storage_path.parent.mkdir(parents=True, exist_ok=True)
        await page.context.storage_state(path=str(storage_path))
        shared = storage_state_path()
        if storage_path.resolve() != shared.resolve():
            shutil.copy2(storage_path, shared)
        emit(f"セッションを保存: {storage_path}", flush=True)


async def _fetch_one_hall(page, hall: str, storage_path: Path) -> list[dict]:
    last_error: Exception | None = None
    for attempt in range(1, 3):
        try:
            await guard_page(page)
            if await page_requires_login(page):
                await _relogin(page, storage_path)
            await open_hall(page, hall)
            await guard_page(page)
            if await page_requires_login(page):
                raise SessionExpired("ホール移動後にログイン画面へ戻りました")
            rows = await fetch_slot_machines(page)
            for row in rows:
                row["hall_name"] = hall
            return rows
        except CaptchaRequired:
            raise
        except SessionExpired as exc:
            last_error = exc
            emit(f"  {hall}: セッション切れを検出。再ログインして再試行します ({attempt}/2)", flush=True)
            await _relogin(page, storage_path)
        except RuntimeError as exc:
            last_error = exc
            message = str(exc)
            if "自動取得" in message or "一時停止" in message or "CAPTCHA" in message:
                raise
            if attempt < 2 and "マイホール" in message:
                emit(f"  {hall}: {message} 再ログインして再試行します ({attempt}/2)", flush=True)
                await _relogin(page, storage_path)
                continue
            raise
    raise last_error or RuntimeError(f"{hall} の取得に失敗しました")


async def refresh_site7_session(*, wait_sec: float = 60.0, hall: str | None = None) -> Path:
    """画面表示（headed）でマイページ/ホールを開き、CAPTCHA 解除後の Cookie を保存する。"""
    set_captcha_wait_sec(wait_sec)
    os.environ["SITE7_CAPTCHA_WAIT_SEC"] = str(wait_sec)
    storage_path = storage_state_path()
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    emit("headed モードでブラウザを開きます。CAPTCHA が出たら手動で解除してください。", flush=True)
    emit(f"Cookie 保存先: {storage_path}", flush=True)

    async with browser_session(storage_path, headless=False) as session:
        page = session.page
        await ensure_session(page, storage_path, allow_login=True)
        halls = configured_halls()
        target = (hall or "").strip() or (halls[0] if halls else "")
        if target:
            emit(f"ホール画面を開きます: {target}", flush=True)
            await open_hall(page, target)
            await wait_if_captcha(page, timeout_sec=wait_sec)
        if await page_requires_login(page):
            raise RuntimeError("ログインできませんでした。メールアドレスまたはパスワードを確認してください。")
        session.save_storage = True
        emit("認証通過後の Cookie を上書き保存します。以降の headless 実行で再利用できます。", flush=True)

    emit(f"セッション更新完了: {storage_path}", flush=True)
    return storage_path


async def fetch_site7_data() -> FetchResult | None:
    halls = [hall.name for hall in halls_for_source("site7")]
    if not halls:
        emit("サイトセブン対象ホールがありません。", flush=True)
        return FetchResult(
            source="site7",
            hall_name="",
            machines=[],
            extra={"skipped": True, "target_halls": configured_halls()},
        )
    if not EMAIL or not PASSWORD:
        emit("エラー: .env に SITE7_EMAIL または SITE7_PASSWORD が設定されていません。")
        return None

    workers = max_workers()
    started = time.monotonic()
    emit(f"巡回ホール: {' → '.join(halls)}", flush=True)
    emit(
        f"安全設定: 同時上限{workers} / Cookie再利用 / 正常時 1–3秒 / 遮断時は15→30→60秒",
        flush=True,
    )
    storage_path = storage_state_path()
    storage_path.parent.mkdir(parents=True, exist_ok=True)

    combined: list[dict] = []
    fetched_halls: list[str] = []
    failed_halls: list[str] = []
    failed_reasons: list[str] = []
    from src.scraper.pace import failure_record, get_pacer, is_pressure

    failure_details: list[dict[str, str]] = []

    async with browser_session(storage_path, user_agent=worker_user_agent(1)) as session:
        page = session.page
        await ensure_session(page, storage_path, allow_login=True)
        session.save_storage = True

        for index, hall in enumerate(halls, start=1):
            emit(f"\n===== [{index}/{len(halls)}] {hall} =====", flush=True)
            try:
                rows = await asyncio.wait_for(
                    _fetch_one_hall(page, hall, storage_path),
                    timeout=hall_timeout_sec(),
                )
                if not rows:
                    emit(f"  {hall}: 0件のためスキップ", flush=True)
                    failed_halls.append(hall)
                    failed_reasons.append(f"{hall}: 0件")
                    failure_details.append(failure_record(hall, "0件"))
                else:
                    hall_result = FetchResult(source="site7", hall_name=hall, machines=rows)
                    hall_path = hall_result.write_incoming(hall_data_dir(hall))
                    emit(f"  {hall}: {len(rows)} 件を {hall_path} に保存", flush=True)
                    combined.extend(rows)
                    fetched_halls.append(hall)
                    get_pacer().mark_success()
            except CaptchaRequired:
                get_pacer().mark_pressure()
                raise
            except Exception as e:
                emit(f"  {hall} の取得失敗: {failure_reason(e)}", flush=True)
                if is_pressure(None, str(e)):
                    get_pacer().mark_pressure()
                failed_halls.append(hall)
                failed_reasons.append(f"{hall}: {failure_reason(e)}")
                failure_details.append(failure_record(hall, e))
            await human_pause("次のホールへ")

    elapsed_sec = time.monotonic() - started
    if not combined:
        emit("取得0件のため既存の incoming.json は上書きしません。", flush=True)
        detail = " / ".join(failed_reasons)
        raise RuntimeError(
            "サイトセブンの取得に失敗したため incoming.json は更新していません。"
            + (f" {detail}" if detail else "")
        )

    ordered = [hall for hall in halls if hall in fetched_halls]
    label = ordered[0] if len(ordered) == 1 else "複数店舗"
    result = FetchResult(
        source="site7",
        hall_name=label,
        machines=combined,
        extra={
            "halls": ordered,
            "target_halls": halls,
            "failed_halls": [hall for hall in halls if hall in failed_halls],
            "failure_details": failure_details,
            "elapsed_sec": round(elapsed_sec, 1),
            "elapsed_label": format_duration(elapsed_sec),
            "workers": workers,
        },
    )
    output_file = result.write_incoming()
    emit(
        f"データ取得完了: {output_file}（対象: {', '.join(halls)} / 成功: {', '.join(ordered) or 'なし'} / "
        f"合計 {len(combined)} 件 / {format_duration(elapsed_sec)}）",
        flush=True,
    )
    return result


def main() -> None:
    from src.run_gate import exit_if_halted

    exit_if_halted("サイトセブン取得")
    asyncio.run(fetch_site7_data())


if __name__ == "__main__":
    main()
