"""スロ極のツール画面を、選ばれたホールで開いて切り抜く。"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image

from src.analyzer.judgment import PROJECT_ROOT
from src.poster.labels import norm_hall

PHONE_WIDTH = 390
PHONE_HEIGHT = 844
PHONE_SCALE = 3
# スマホ幅だと末尾は2列。高さを足して、0〜9の5段が縦に収まるようにする。
DIGIT_WIDTH = 390
DIGIT_HEIGHT = 1680
DIGIT_SCALE = 2


class CaptureError(RuntimeError):
    """ツール画面を画像にできなかった。"""


_FRAME_DIGIT = """(digit) => {
    const wanted = String(digit);
    const section = document.querySelector('#section-hall-digits');
    const cards = section ? [...section.querySelectorAll('.hall-digit-card')] : [];
    const card = cards.find((node) => {
        const label = node.querySelector('.hall-digit-num');
        const raw = (label && label.textContent ? label.textContent : '').replace(/\\s/g, '');
        return raw === '末尾' + wanted;
    }) || cards[Number(digit)];
    if (!card) return { ok: false, count: cards.length };
    const dock = document.getElementById('nav-dock');
    const toggle = document.getElementById('nav-dock-toggle');
    const top = card.getBoundingClientRect().top + window.scrollY;
    const destination = Math.max(0, top);
    window.scrollTo(0, destination);
    document.documentElement.scrollTop = destination;
    document.body.scrollTop = destination;
    const label = card.querySelector('.hall-digit-num');
    return {
        ok: true,
        count: cards.length,
        y: window.scrollY,
        top: destination,
        height: document.documentElement.scrollHeight,
        label: label && label.textContent ? label.textContent : '',
    };
}"""


_FRAME_DIGIT = """(digit) => {
    const wanted = String(digit);
    const section = document.querySelector('#section-hall-digits');
    const cards = section ? [...section.querySelectorAll('.hall-digit-card')] : [];
    const card = cards.find((node) => {
        const label = node.querySelector('.hall-digit-num');
        const raw = (label && label.textContent ? label.textContent : '').replace(/\\s/g, '');
        return raw === '末尾' + wanted;
    }) || cards[Number(digit)];
    if (!card) return { ok: false, count: cards.length };
    const dock = document.getElementById('nav-dock');
    const toggle = document.getElementById('nav-dock-toggle');
    if (dock && toggle && !dock.classList.contains('is-collapsed')) toggle.click();
    const bar = document.getElementById('nav-dock')?.offsetHeight || 0;
    const top = card.getBoundingClientRect().top + window.scrollY - bar - 8;
    const destination = Math.max(0, top);
    window.scrollTo(0, destination);
    document.documentElement.scrollTop = destination;
    document.body.scrollTop = destination;
    const label = card.querySelector('.hall-digit-num');
    return {
        ok: true,
        count: cards.length,
        y: window.scrollY,
        top: destination,
        height: document.documentElement.scrollHeight,
        label: label && label.textContent ? label.textContent : '',
    };
}"""


def capture_juggler_island(
    hall_name: str,
    family_label: str,
    dest: Path,
    *,
    base_url: str | None = None,
) -> None:
    """ジャグラーモードで、指定ホールの島カードを切り抜く。"""
    capture_juggler_islands([(hall_name, family_label)], dest.parent, base_url=base_url)
    shot = dest.parent / "01.png"
    if shot != dest:
        shot.replace(dest)


def capture_juggler_islands(
    targets: list[tuple[str, str]],
    dest_dir: Path,
    *,
    base_url: str | None = None,
) -> list[Path]:
    """1ブラウザで、島カードだけを順に撮る。"""
    if not targets:
        return []
    url, process = _resolve_dashboard(base_url)
    dest_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    try:
        saved = _shoot_juggler_batch(url, targets, dest_dir)
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
    return saved


def capture_hall(
    hall_name: str,
    dest: Path,
    *,
    base_url: str | None = None,
    view: str = "hall",
    focus: str = "section-all-high",
    digit: int | None = None,
    machine_name: str | None = None,
) -> None:
    """対象ホールのスロ極をスマホ表示で開き、カードが収まる範囲を output へ保存する。

    view が hall のときは全台系の機種カード、digits のときは末尾カードを撮る。
    """
    url, process = _resolve_dashboard(base_url)
    try:
        _shoot(url, hall_name, dest, view=view, focus=focus, digit=digit, machine_name=machine_name)
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


_FRAME_JUGGLER = """(label) => {
    const hide = (sel) => {
        document.querySelectorAll(sel).forEach((node) => { node.style.display = 'none'; });
    };
    hide('#nav-dock');
    hide('#section-roster');
    hide('#juggler-summary');
    hide('#juggler-area-hot');
    hide('#juggler-meta');
    hide('#juggler-empty');
    const cards = [...document.querySelectorAll('#juggler-islands .juggler-island')];
    const card = cards.find((node) => {
        const title = node.querySelector('.juggler-island-head h3');
        return title && title.textContent.trim() === label;
    });
    if (!card) return { ok: false, count: cards.length };
    cards.forEach((node) => {
        if (node !== card) node.style.display = 'none';
    });
    document.querySelectorAll('.juggler-bucket').forEach((bucket) => {
        bucket.style.display = bucket.contains(card) ? 'block' : 'none';
    });
    card.querySelectorAll('details.juggler-units').forEach((node) => { node.open = false; });
    card.querySelectorAll('.juggler-score').forEach((node) => { node.style.display = 'none'; });
    card.querySelectorAll('.juggler-slump, .juggler-mini-slump').forEach((node) => {
        node.style.display = 'none';
    });
    card.querySelectorAll('.juggler-metric').forEach((cell) => {
        const label = cell.querySelector('span')?.textContent?.trim() || '';
        if (label === '合算確率' || label === 'REG確率') {
            cell.querySelectorAll('small').forEach((node) => { node.style.display = 'none'; });
            return;
        }
        cell.style.display = 'none';
    });
    card.scrollIntoView({ block: 'start', inline: 'nearest' });
    const rect = card.getBoundingClientRect();
    const pad = 10;
    const top = Math.max(0, rect.top - pad);
    const height = Math.min(window.innerHeight - top - 4, rect.height + pad * 2);
    if (height < 120) return { ok: false, count: cards.length, reason: 'short' };
    return {
        ok: true,
        count: cards.length,
        clip: { x: 0, y: top, width: window.innerWidth, height: height },
    };
}"""


def _shoot_juggler_batch(base_url: str, targets: list[tuple[str, str]], dest_dir: Path) -> list[Path]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise CaptureError("Playwright が入っていません") from exc
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    first_hall = targets[0][0]
    prefecture = _prefecture_for(first_hall)
    init = (
        "localStorage.setItem('slgoku-prefecture', "
        + json.dumps(prefecture or "fukuoka")
        + "); localStorage.setItem('slgoku-area', 'all');"
        + "localStorage.setItem('slgoku-juggler-mode', '1');"
        + _FIND_CARD
    )
    saved: list[Path] = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": PHONE_WIDTH, "height": 2400},
                device_scale_factor=PHONE_SCALE,
                is_mobile=True,
                has_touch=True,
                user_agent=(
                    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
                ),
                locale="ja-JP",
            )
            page = context.new_page()
            page.add_init_script(init)
            page.goto(base_url.rstrip("/") + "/", wait_until="domcontentloaded", timeout=120000)
            _login_if_needed(page)
            page.wait_for_selector("#hall-select", timeout=90000)
            _ensure_juggler_mode(page)
            for index, (hall_name, family_label) in enumerate(targets, start=1):
                prefecture = _prefecture_for(hall_name)
                if prefecture:
                    page.evaluate(
                        """(pref) => {
                            localStorage.setItem('slgoku-prefecture', pref);
                            const select = document.querySelector('#prefecture-select');
                            if (select && [...select.options].some((o) => o.value === pref)) {
                                select.value = pref;
                                select.dispatchEvent(new Event('change', { bubbles: true }));
                            }
                        }""",
                        prefecture,
                    )
                    page.wait_for_timeout(500)
                    _ensure_juggler_mode(page)
                _select_hall(page, hall_name)
                _ensure_juggler_mode(page)
                page.wait_for_function(
                    """() => {
                        const body = document.body;
                        return body && !body.classList.contains('is-loading-judgments');
                    }""",
                    timeout=120000,
                )
                page.wait_for_selector("body.is-juggler-mode #section-juggler .juggler-island", timeout=120000)
                page.wait_for_function(
                    """(label) => {
                        return [...document.querySelectorAll('#juggler-islands .juggler-island')].some((node) => {
                            const title = node.querySelector('.juggler-island-head h3');
                            return title && title.textContent.trim() === label;
                        });
                    }""",
                    arg=family_label,
                    timeout=120000,
                )
                framed = page.evaluate(_FRAME_JUGGLER, family_label)
                if not framed.get("ok"):
                    detail = framed.get("reason") or framed.get("count", 0)
                    raise CaptureError(
                        f"ジャグラー島が見つかりません: {hall_name} / {family_label}（{detail}）"
                    )
                clip = framed.get("clip")
                dest = dest_dir / f"{index:02d}.png"
                shot = page.screenshot(type="png", clip=clip, animations="disabled")
                image = Image.open(BytesIO(shot)).convert("RGB")
                temporary = dest.with_suffix(dest.suffix + ".tmp")
                image.save(temporary, format="PNG", optimize=True)
                temporary.replace(dest)
                if not dest.is_file() or dest.stat().st_size == 0:
                    raise CaptureError(f"ジャグラー島の画像を保存できません: {hall_name}")
                saved.append(dest)
            browser.close()
    except PlaywrightTimeout as exc:
        raise CaptureError("ジャグラー画面の表示がタイムアウトしました") from exc
    return saved


def _ensure_juggler_mode(page) -> None:
    page.evaluate(
        """() => {
            localStorage.setItem('slgoku-juggler-mode', '1');
            document.body.classList.add('is-juggler-mode');
            const toggle = document.getElementById('juggler-mode');
            if (toggle && toggle.getAttribute('aria-pressed') !== 'true') {
                toggle.click();
            }
        }"""
    )
    page.wait_for_timeout(200)


def _select_hall(page, hall_name: str) -> None:
    page.wait_for_function(
        """(name) => {
            const body = document.body;
            if (!body || body.classList.contains('is-loading-judgments')) return false;
            const select = document.querySelector('#hall-select');
            if (!select) return false;
            return [...select.options].some((option) => option.value === name || option.textContent.trim() === name);
        }""",
        arg=hall_name,
        timeout=120000,
    )
    page.evaluate(
        """(name) => {
            const select = document.querySelector('#hall-select');
            const option = [...select.options].find((item) => item.value === name || item.textContent.trim() === name);
            if (!option) throw new Error('hall missing');
            select.value = option.value;
            select.dispatchEvent(new Event('change', { bubbles: true }));
        }""",
        hall_name,
    )
    page.wait_for_timeout(500)


def _shoot(
    base_url: str,
    hall_name: str,
    dest: Path,
    *,
    view: str,
    focus: str,
    digit: int | None,
    machine_name: str | None,
) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise CaptureError("Playwright が入っていません") from exc

    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    prefecture = _prefecture_for(hall_name)
    init = (
        "localStorage.setItem('slgoku-prefecture', "
        + json.dumps(prefecture or "fukuoka")
        + "); localStorage.setItem('slgoku-area', 'all');"
        + _FIND_CARD
    )
    try:
        _shoot_page(
            sync_playwright,
            PlaywrightTimeout,
            init,
            base_url,
            hall_name,
            dest,
            view,
            focus,
            digit,
            machine_name,
        )
    except PlaywrightTimeout as exc:
        raise CaptureError(f"店舗画面の表示がタイムアウトしました: {hall_name}") from exc


def _shoot_page(
    sync_playwright,
    playwright_timeout,
    init: str,
    base_url: str,
    hall_name: str,
    dest: Path,
    view: str,
    focus: str,
    digit: int | None,
    machine_name: str | None,
) -> None:
    del playwright_timeout
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except Exception as exc:
            raise CaptureError(f"ブラウザを起動できませんでした: {exc.__class__.__name__}") from exc
        portrait = view == "digits"
        context = browser.new_context(
            viewport={
                "width": DIGIT_WIDTH if portrait else PHONE_WIDTH,
                "height": DIGIT_HEIGHT if portrait else PHONE_HEIGHT,
            },
            device_scale_factor=DIGIT_SCALE if portrait else PHONE_SCALE,
            is_mobile=True,
            has_touch=True,
            user_agent=(
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
            ),
            locale="ja-JP",
        )
        page = context.new_page()
        page.add_init_script(init)
        page.goto(base_url.rstrip("/") + "/", wait_until="domcontentloaded", timeout=60000)
        _login_if_needed(page)
        page.wait_for_selector("#hall-select", timeout=90000)
        ready = page.wait_for_function(
            """(name) => {
                const body = document.body;
                if (!body || body.classList.contains('is-loading-judgments')) return false;
                const select = document.querySelector('#hall-select');
                if (!select) return false;
                return [...select.options].some((option) => option.value === name || option.textContent.trim() === name);
            }""",
            arg=hall_name,
            timeout=90000,
        )
        if ready is None:
            raise CaptureError(f"店舗画面に {hall_name} が出ませんでした")
        page.evaluate(
            """(name) => {
                const select = document.querySelector('#hall-select');
                const option = [...select.options].find((item) => item.value === name || item.textContent.trim() === name);
                select.value = option.value;
                select.dispatchEvent(new Event('change', { bubbles: true }));
            }""",
            hall_name,
        )
        page.wait_for_timeout(400)
        if view == "digits":
            _collapse_dock(page)
            _open_digit_board(page, hall_name, digit)
            if digit is not None:
                page.evaluate(_FRAME_DIGIT, digit)
                visible = page.evaluate(
                    """() => {
                        const cards = [...document.querySelectorAll('#section-hall-digits .hall-digit-card')];
                        const shown = cards.filter((card) => {
                            const box = card.getBoundingClientRect();
                            return box.bottom > 0 && box.top < window.innerHeight;
                        }).map((card) => (card.querySelector('.hall-digit-num') || {}).textContent || '');
                        return {
                            y: window.scrollY,
                            height: document.documentElement.scrollHeight,
                            shown,
                        };
                    }"""
                )
                if str(digit) not in "".join(visible.get("shown") or []):
                    raise CaptureError(f"末尾{digit}が画面内にありません: {visible}")
        else:
            _scroll_to_machine(page, machine_name, focus)
        clip = _card_clip(page, view, focus, digit, machine_name)
        shot = page.screenshot(type="png", clip=clip) if clip else page.screenshot(type="png")
        browser.close()
    image = Image.open(BytesIO(shot)).convert("RGB")
    dest.parent.mkdir(parents=True, exist_ok=True)
    temporary = dest.with_suffix(dest.suffix + ".tmp")
    image.save(temporary, format="PNG", optimize=True)
    temporary.replace(dest)


def _card_clip(
    page,
    view: str,
    focus: str,
    digit: int | None,
    machine_name: str | None,
) -> dict[str, float] | None:
    """選んだカードの上端から、そのカードの下端までを返す。次のカードは含めない。"""
    if view == "digits":
        box = page.evaluate(_CLIP_DIGIT, digit if digit is not None else -1)
    else:
        box = page.evaluate(_CLIP_HALL, machine_name or "")
    if not isinstance(box, dict):
        return None
    try:
        clip = {
            "x": float(box["x"]),
            "y": float(box["y"]),
            "width": float(box["width"]),
            "height": float(box["height"]),
        }
    except (KeyError, TypeError, ValueError):
        return None
    if clip["width"] < 40 or clip["height"] < 80:
        return None
    return clip


_CLIP_HALL = """(name) => {
    const dock = document.getElementById('nav-dock');
    if (dock) dock.style.display = 'none';
    const card = window.__suroFindCard(name);
    if (!card) return null;
    const title = card.querySelector('h3') || card;
    const destination = Math.max(0, title.getBoundingClientRect().top + window.scrollY - 10);
    window.scrollTo(0, destination);
    document.documentElement.scrollTop = destination;
    const rect = card.getBoundingClientRect();
    const top = Math.max(0, rect.top - 8);
    const bottom = Math.min(window.innerHeight, rect.bottom + 4);
    if (bottom - top < 80) return null;
    return { x: 0, y: top, width: window.innerWidth, height: bottom - top };
}"""


_CLIP_DIGIT = """(digit) => {
    const dock = document.getElementById('nav-dock');
    if (dock) dock.style.display = 'none';
    const grid = document.querySelector('#section-hall-digits .hall-digit-grid');
    const cards = grid ? [...grid.querySelectorAll('.hall-digit-card')] : [];
    if (!grid || !cards.length) return null;
    const wanted = String(digit);
    const focus = cards.find((node) => {
        const label = node.querySelector('.hall-digit-num');
        const raw = (label && label.textContent ? label.textContent : '').replace(/\\s/g, '');
        return raw === '末尾' + wanted;
    }) || null;
    const pad = 14;
    const gridTop = grid.getBoundingClientRect().top + window.scrollY;
    const gridBottom = grid.getBoundingClientRect().bottom + window.scrollY;
    const span = gridBottom - gridTop + pad * 2;
    let destination = Math.max(0, gridTop - pad);
    if (span > window.innerHeight && focus) {
        const focusTop = focus.getBoundingClientRect().top + window.scrollY;
        const focusBottom = focusTop + focus.getBoundingClientRect().height;
        const mid = (focusTop + focusBottom) / 2;
        destination = Math.max(0, mid - window.innerHeight / 2);
        destination = Math.min(destination, Math.max(0, focusTop - 20));
        destination = Math.max(destination, Math.max(0, focusBottom + 20 - window.innerHeight));
    }
    window.scrollTo(0, destination);
    document.documentElement.scrollTop = destination;
    document.body.scrollTop = destination;
    const rect = grid.getBoundingClientRect();
    let top = Math.max(0, rect.top - pad);
    let bottom = Math.min(window.innerHeight, rect.bottom + pad);
    if (focus) {
        const box = focus.getBoundingClientRect();
        top = Math.min(top, Math.max(0, box.top - 6));
        bottom = Math.max(bottom, Math.min(window.innerHeight, box.bottom + 6));
    }
    if (bottom - top < 80) return null;
    return { x: 0, y: top, width: window.innerWidth, height: bottom - top };
}"""


def _open_digit_board(page, hall_name: str, digit: int | None) -> None:
    tab = page.locator("#tab-digits")
    if tab.count() == 0:
        raise CaptureError("末尾分析のタブが画面にありません")
    tab.click()
    page.wait_for_function(
        """(name) => {
            const select = document.querySelector('#digit-hall-select');
            if (!select) return false;
            return [...select.options].some((option) => option.value === name || option.textContent.trim() === name);
        }""",
        arg=hall_name,
        timeout=30000,
    )
    page.evaluate(
        """(name) => {
            const select = document.querySelector('#digit-hall-select');
            const option = [...select.options].find((item) => item.value === name || item.textContent.trim() === name);
            select.value = option.value;
            select.dispatchEvent(new Event('change', { bubbles: true }));
        }""",
        hall_name,
    )
    page.wait_for_selector("#section-hall-digits .hall-digit-card", timeout=30000)
    page.wait_for_timeout(900)
    if digit is None:
        _scroll_into_view(page, "#section-hall-digits")
        return
    framed = page.evaluate(_FRAME_DIGIT, digit)
    page.wait_for_timeout(400)
    framed = page.evaluate(_FRAME_DIGIT, digit)
    label = str((framed or {}).get("label") or "")
    if not framed or not framed.get("ok") or str(digit) not in label:
        count = (framed or {}).get("count", 0)
        raise CaptureError(
            f"末尾{digit}のカードへ移動できませんでした（カード{count}枚、位置{framed}）"
        )


_FIND_CARD = """
window.__suroFindCard = (name) => {
    const norm = (value) => String(value || '').normalize('NFKC').replace(/\\s/g, '');
    const strip = (value) => norm(value).replace(/^(?:\\d{1,3})?[LSPlsp](?=[\\u3040-\\u30ff\\u4e00-\\u9fff])/, '');
    const wanted = strip(name);
    const cards = [...document.querySelectorAll('#high-cards .card, #mid-cards .card, #base-cards .card')];
    const titled = cards.map((node) => ({
        node,
        title: strip(node.querySelector('h3')?.textContent || ''),
    })).filter((item) => item.title);
    const hit = titled.find((item) => item.title === wanted)
        || titled.find((item) => wanted && (item.title.includes(wanted) || (wanted.length >= 4 && item.title.length >= 4 && wanted.includes(item.title))));
    return hit ? hit.node : null;
};
"""


def _collapse_dock(page) -> None:
    page.evaluate(
        """() => {
            const dock = document.getElementById('nav-dock');
            if (dock) dock.style.display = 'none';
        }"""
    )
    page.wait_for_timeout(150)


def _scroll_to_machine(page, machine_name: str | None, focus: str) -> None:
    _collapse_dock(page)
    moved = page.evaluate(
        """(name) => {
            const card = window.__suroFindCard ? window.__suroFindCard(name) : null;
            if (!card) return false;
            const title = card.querySelector('h3') || card;
            const top = Math.max(0, title.getBoundingClientRect().top + window.scrollY);
            window.scrollTo(0, top);
            document.documentElement.scrollTop = top;
            document.body.scrollTop = top;
            return true;
        }""",
        machine_name or "",
    )
    if not moved:
        _scroll_to_hall_cards(page, focus)


def _scroll_to_hall_cards(page, focus: str) -> None:
    selector = page.evaluate(
        """(focus) => {
            const ids = [focus, 'section-all-high', 'section-high-56', 'section-high-base', 'section-list'];
            for (const id of ids) {
                const section = document.getElementById(id);
                if (!section) continue;
                if (id === 'section-list' || section.querySelector('.card')) return '#' + id;
            }
            return '';
        }""",
        focus,
    )
    if selector:
        _scroll_into_view(page, selector)


def _scroll_into_view(page, selector: str) -> None:
    page.evaluate(
        """(selector) => {
            const dock = document.getElementById('nav-dock');
            const toggle = document.getElementById('nav-dock-toggle');
            if (dock && toggle && !dock.classList.contains('is-collapsed')) {
                toggle.click();
            }
            const node = document.querySelector(selector);
            if (!node) return;
            const bar = document.getElementById('nav-dock')?.offsetHeight || 0;
            const top = node.getBoundingClientRect().top + window.scrollY - bar - 12;
            window.scrollTo(0, Math.max(0, top));
        }""",
        selector,
    )


def _login_if_needed(page) -> None:
    if page.locator("#email").count() == 0:
        return
    email = (os.environ.get("SURO_DASHBOARD_EMAIL") or "").strip()
    password = (os.environ.get("SURO_DASHBOARD_PASSWORD") or os.environ.get("DASHBOARD_PASSWORD") or "").strip()
    if not email:
        raise CaptureError("ログイン画面です。SURO_DASHBOARD_EMAIL を設定するか、DASHBOARD_OPEN=1 で開いてください。")
    page.fill("#email", email)
    if password and page.locator("input[name=password]").count():
        page.fill("input[name=password]", password)
    page.locator("button[type=submit]").click()
    page.wait_for_selector("#hall-select", timeout=30000)


def _prefecture_for(hall_name: str) -> str | None:
    try:
        from src.scraper.halls import load_halls
    except Exception:
        return None
    target = norm_hall(hall_name)
    for hall in load_halls(include_disabled=True):
        labels = [hall.name, *hall.aliases]
        if any(norm_hall(label) == target for label in labels):
            return hall.prefecture or None
    return None


def _resolve_dashboard(base_url: str | None) -> tuple[str, subprocess.Popen | None]:
    configured = (base_url or os.environ.get("SURO_DASHBOARD_URL") or "").strip()
    if configured:
        if not _healthy(configured):
            raise CaptureError(f"スロ極の画面に接続できません: {configured}")
        return configured.rstrip("/"), None
    local = "http://127.0.0.1:8000"
    if _healthy(local):
        return local, None
    return _start_local_dashboard()


def _start_local_dashboard() -> tuple[str, subprocess.Popen]:
    port = _free_port()
    env = os.environ.copy()
    env["DASHBOARD_OPEN"] = "1"
    env["TUNNEL_ENABLED"] = "0"
    env["X_MONITOR_DISABLE"] = "1"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "src.dashboard.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 40
    while time.time() < deadline:
        if process.poll() is not None:
            raise CaptureError("スロ極の画面を起動できませんでした")
        if _healthy(url):
            return url, process
        time.sleep(0.3)
    process.terminate()
    raise CaptureError("スロ極の画面が起動しませんでした")


def _healthy(url: str) -> bool:
    try:
        response = httpx.get(url.rstrip("/") + "/health", timeout=2)
    except httpx.HTTPError:
        return False
    return response.status_code == 200


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
