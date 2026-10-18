"""P'sCUBE のスロット台データ。HTML表と、DMMぱちタウン連携版の JSON を読む。"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from typing import Any
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

from src.scraper.base import FetchResult, PROJECT_ROOT
from src.scraper.log_safe import emit, failure_reason
from src.scraper.browser import chrome_client_identity
from src.scraper.deadline import hall_timeout_sec, run_within
from src.scraper.halls import Hall, format_duration, hall_data_dir, halls_for_source, sleep_hall_gap
from src.scraper.pace import failure_record, get_pacer, is_pressure, request_gap, rotate_user_agent, summarize_failure
from src.scraper.smart_retry import is_deferrable
from src.scraper.html_tables import HtmlBlock, parse_document
from src.scraper.targets import is_watched

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_SKIP_DAI = {"", "平均", "合計", "計", "台番号", "台番"}
_NORMAL_HEADERS = ("通常G", "通常Ｇ", "通常ゲーム", "通常回転", "通常スタート")
_TOTAL_HEADERS = ("累計スタート", "総回転数", "総スタート", "累計ゲーム", "回転数", "スタート回数", "スタート", "G数")
_BB_HEADERS = ("BB回数", "BB", "BIG回数", "BIG")
_RB_HEADERS = ("RB回数", "RB", "REG回数", "REG")
_ART_HEADERS = ("ART回数", "ART", "AT回数", "AT", "AT/ART回数")
_DIFF_HEADERS = ("差枚", "差玉", "出玉")
_NAME_HEADERS = ("機種名", "機種")
_LINK_HINTS = ("slot", "slo", "cgi", "dai", "kishu", "machine", "dedama", "type=2", "ps=s", "mm_id")
_BRANCH_PAIR_RE = re.compile(r"""['"]([A-Za-z0-9_-]+)['"]\s*:\s*['"]([A-Za-z0-9_-]+)['"]""")
_LIST_LITERAL_RE = re.compile(r"""['"]([^'"]*list\.json)['"]""")
_FETCH_TEMPLATE_RE = re.compile(r"fetchJson\(\s*`([^`]*list\.json)`")


class PscubeError(RuntimeError):
    """P'sCUBE の取得失敗。"""


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    if isinstance(value, int):
        return value
    text = str(value).replace(",", "").replace("，", "").replace("+", "").strip()
    if not text or text in {"--", "-", "―"}:
        return None
    match = re.search(r"-?\d+", text)
    return int(match.group(0)) if match else None


def _ratio_from_counts(games: int | None, hits: int | None) -> str | None:
    if games is None or not hits:
        return None
    return f"1/{round(games / hits)}"


def _header_index(headers: list[str], names: tuple[str, ...]) -> int | None:
    normalized = [re.sub(r"\s+", "", cell) for cell in headers]
    for name in names:
        key = re.sub(r"\s+", "", name)
        for index, cell in enumerate(normalized):
            if cell == key:
                return index
    return None


def _cell(row: list[str], index: int | None) -> str:
    if index is None or index >= len(row):
        return ""
    return row[index]


def _dai_no(value: str) -> str | None:
    if value.strip() in _SKIP_DAI:
        return None
    match = re.search(r"\d+", value)
    return match.group(0) if match else None


def _row_from_counts(
    *,
    machine_name: str,
    dai_no: str,
    games: int | None,
    bb: int | None,
    rb: int | None,
    art: int | None,
    hall_name: str,
    diff: int | None = None,
    synthetic: str | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "machine_name": machine_name,
        "dai_no": dai_no,
        "total_games": games,
        "bb": bb,
        "rb": rb,
        "合成確率": synthetic or _ratio_from_counts(games, (bb or 0) + (rb or 0) or None),
        "BB確率": _ratio_from_counts(games, bb),
        "RB確率": _ratio_from_counts(games, rb),
        "art": art,
        "hall_name": hall_name,
    }
    if diff is not None:
        row["差玉"] = diff
    return row


def parse_pscube_tables(html: str, hall_name: str, *, fallback_name: str = "") -> list[dict[str, Any]]:
    """台番号と通常G（無ければ総回転・スタート）のある表を台行にする。"""
    found: list[dict[str, Any]] = []
    heading = fallback_name
    for block in parse_document(html):
        if block.kind == "heading" and block.text:
            heading = block.text
            continue
        if block.kind != "table" or len(block.rows) < 2:
            continue
        headers = block.rows[0]
        dai_index = _header_index(headers, ("台番号", "台番"))
        normal_index = _header_index(headers, _NORMAL_HEADERS)
        total_index = _header_index(headers, _TOTAL_HEADERS)
        game_index = normal_index if normal_index is not None else total_index
        if dai_index is None or game_index is None:
            continue
        name_index = _header_index(headers, _NAME_HEADERS)
        bb_index = _header_index(headers, _BB_HEADERS)
        rb_index = _header_index(headers, _RB_HEADERS)
        art_index = _header_index(headers, _ART_HEADERS)
        diff_index = _header_index(headers, _DIFF_HEADERS)
        for cells in block.rows[1:]:
            dai = _dai_no(_cell(cells, dai_index))
            if not dai:
                continue
            name = _cell(cells, name_index).strip() or heading
            if not name:
                continue
            found.append(
                _row_from_counts(
                    machine_name=name,
                    dai_no=dai,
                    games=_as_int(_cell(cells, game_index)),
                    bb=_as_int(_cell(cells, bb_index)),
                    rb=_as_int(_cell(cells, rb_index)),
                    art=_as_int(_cell(cells, art_index)),
                    hall_name=hall_name,
                    diff=_as_int(_cell(cells, diff_index)),
                )
            )
    return found


def parse_assist_json(list_payload: dict[str, Any], dai_payload: dict[str, Any], hall_name: str) -> list[dict[str, Any]]:
    """DMMぱちタウン連携版の list.json / dai.json から当日行を作る。"""
    types = [item for item in (list_payload.get("types") or []) if isinstance(item, dict)]
    slot_types = {
        item.get("type")
        for item in types
        if str(item.get("group_name") or "").upper() == "S" or "スロ" in str(item.get("type_name") or "")
    }
    mdata = dai_payload.get("mdata") or {}
    names: dict[str, str] = {}
    if isinstance(mdata, dict):
        for item in mdata.values():
            if isinstance(item, dict) and item.get("mm_id"):
                names[str(item["mm_id"])] = str(item.get("mname") or "").strip()
    units = dai_payload.get("dai") or {}
    if not isinstance(units, dict):
        return []
    rows: list[dict[str, Any]] = []
    for dai_no, unit in units.items():
        if not isinstance(unit, dict):
            continue
        if slot_types and unit.get("type") not in slot_types:
            continue
        name = names.get(str(unit.get("mm_id") or ""))
        history = unit.get("rows") or []
        latest = history[0] if history and isinstance(history[0], dict) else None
        if not name or not latest:
            continue
        number = _dai_no(str(dai_no))
        if not number:
            continue
        bonus = str(latest.get("bonus_rate") or "").strip()
        rows.append(
            _row_from_counts(
                machine_name=name,
                dai_no=number,
                games=_as_int(latest.get("start")),
                bb=_as_int(latest.get("bb_num")),
                rb=_as_int(latest.get("reg_num")),
                art=_as_int(latest.get("art_num")),
                hall_name=hall_name,
                diff=_as_int(latest.get("diff")),
                synthetic=bonus if bonus and bonus != "-" else None,
            )
        )
    return rows


def _metric(values: dict[str, Any], *titles: str) -> int | None:
    for title in titles:
        if title not in values or values[title] is False:
            continue
        return _as_int(values[title])
    return None


def parse_netcube_unit(payload: dict[str, Any], hall_name: str, *, machine_name: str = "") -> dict[str, Any] | None:
    """台詳細 JSON の当日列からゲーム数・BIG・REG・AT/ART を読む。"""
    ki = payload.get("Ki") if isinstance(payload.get("Ki"), dict) else {}
    dai = payload.get("Dai") if isinstance(payload.get("Dai"), dict) else {}
    name = str(machine_name or ki.get("name") or "").strip()
    number = _dai_no(str(dai.get("cd_dai") or ""))
    if not name or not number:
        return None
    values: dict[str, Any] = {}
    for row in payload.get("Data") or []:
        if not isinstance(row, dict):
            continue
        title = re.sub(r"\s+", "", str(row.get("title") or ""))
        items = row.get("item") or []
        values[title] = items[0] if items else None
    games = _metric(values, "累計ゲーム", "ゲーム数", "総回転数")
    return _row_from_counts(
        machine_name=name,
        dai_no=number,
        games=games,
        bb=_metric(values, "BIG", "BB"),
        rb=_metric(values, "REG", "RB"),
        art=_metric(values, "AT/ART", "ART", "AT"),
        hall_name=hall_name,
    )


def netcube_slot_units(payload: dict[str, Any]) -> list[tuple[str, str]]:
    """機種一覧 JSON から、監視対象スロットの（機種名, 台番号）を出す。"""
    found: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in payload.get("Ki") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("cd_ps") or "") not in {"2", "S"} and item.get("cd_ps") != 2:
            continue
        name = str(item.get("nmk_kisyu") or "").strip()
        if not name or not is_watched(name):
            continue
        listing = item.get("list") if isinstance(item.get("list"), dict) else {}
        for dai in listing.get("cd_dai") or []:
            number = _dai_no(str(dai))
            if not number:
                continue
            key = (name, number)
            if key in seen:
                continue
            seen.add(key)
            found.append(key)
    return found


def _challenge_page(html: str) -> bool:
    title = html[:4000]
    return "Just a moment" in title or "しばらくお待ちください" in title or "cf-mitigated" in html or "<title>認証確認中" in title


def discover_json_urls(html: str, page_url: str) -> tuple[str | None, str | None]:
    """ページ内の list.json 参照から list / dai の URL を決める。"""
    literal = _LIST_LITERAL_RE.search(html)
    if literal and "${" not in literal.group(1):
        list_url = urljoin(page_url, literal.group(1))
        return list_url, list_url.replace("list.json", "dai.json")
    template = _FETCH_TEMPLATE_RE.search(html)
    pairs = dict(_BRANCH_PAIR_RE.findall(html))
    if template and "${branchId}" in template.group(1) and pairs:
        parts = [part for part in urlparse(page_url).path.split("/") if part]
        project = parts[-2] if len(parts) >= 2 else (parts[-1] if parts else "")
        branch = pairs.get(project)
        if branch:
            path = template.group(1).replace("${branchId}", branch)
            list_url = urljoin(page_url, path)
            return list_url, list_url.replace("list.json", "dai.json")
    return None, None


def candidate_json_urls(page_url: str) -> tuple[str, str]:
    parsed = urlparse(page_url)
    path = parsed.path
    if not path.endswith("/"):
        path = path.rsplit("/", 1)[0] + "/"
    base = f"{parsed.scheme}://{parsed.netloc}{path}"
    return urljoin(base, "list.json"), urljoin(base, "dai.json")


def follow_links(blocks: list[HtmlBlock], page_url: str, hall_url: str) -> list[str]:
    hall = urlparse(hall_url)
    prefix = hall.path if hall.path.endswith("/") else hall.path.rsplit("/", 1)[0] + "/"
    found: list[str] = []
    seen: set[str] = set()
    for block in blocks:
        if block.kind != "link":
            continue
        absolute = urljoin(page_url, block.href)
        parsed = urlparse(absolute)
        if parsed.netloc != hall.netloc or not parsed.path.startswith(prefix):
            continue
        if parsed.path.endswith((".css", ".js", ".png", ".jpg", ".gif", ".svg", ".ico")):
            continue
        blob = (absolute + " " + block.text).lower()
        if not any(hint in blob for hint in _LINK_HINTS):
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        found.append(absolute)
    return found[:40]


class PscubeClient:
    def __init__(self) -> None:
        from src.scraper.proxy_config import build_client_opener

        self.jar = urllib.request.HTTPCookieProcessor()
        self.opener = build_client_opener(self.jar)
        self.sid = str(uuid.uuid4())
        self.user_agent = rotate_user_agent("desktop")

    def get(self, url: str, *, referer: str | None = None, accept: str = "text/html") -> tuple[int, str, str]:
        identity = chrome_client_identity()
        headers = {
            "User-Agent": self.user_agent,
            "Accept": accept,
            "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
            "sec-ch-ua": identity["sec_ch_ua"],
            "sec-ch-ua-mobile": identity["sec_ch_ua_mobile"],
            "sec-ch-ua-platform": identity["sec_ch_ua_platform"],
            "Upgrade-Insecure-Requests": "1",
        }
        if referer:
            headers["Referer"] = referer
        request = urllib.request.Request(url, headers=headers)
        from src.scraper.deadline import request_timeout_sec

        pacer = get_pacer("pscube")
        pacer.sleep(request_gap(pacer))
        with pacer.limit(wait_sec=request_timeout_sec()):
            last_status = 0
            last_body = ""
            for attempt in range(2):
                try:
                    with self.opener.open(request, timeout=request_timeout_sec()) as response:
                        body = response.read().decode("utf-8", errors="replace")
                        status = response.status
                        final = response.geturl()
                except urllib.error.HTTPError as exc:
                    body = exc.read().decode("utf-8", errors="replace")
                    status = exc.code
                    final = url
                    if is_pressure(status, body) and attempt < 1:
                        pacer.mark_pressure()
                        last_status, last_body = status, body
                        continue
                    return status, final, body
                except urllib.error.URLError as exc:
                    if is_pressure(None, str(exc.reason)) and attempt < 1:
                        pacer.mark_pressure()
                        continue
                    raise PscubeError(f"接続できません {url}: {exc.reason}") from exc
                if is_pressure(status, body):
                    pacer.mark_pressure()
                    last_status, last_body = status, body
                    if attempt < 1:
                        continue
                    return status, final, body
                pacer.mark_success()
                return status, final, body
        return last_status, url, last_body

    def get_json(self, url: str, referer: str) -> dict[str, Any]:
        joiner = "&" if "?" in url else "?"
        target = url if "sid=" in url else f"{url}{joiner}sid={self.sid}"
        status, _final, body = self.get(target, referer=referer, accept="application/json")
        if status != 200:
            raise PscubeError(f"JSON 取得失敗 HTTP {status}: {url}")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise PscubeError(f"JSON を解析できません: {url}") from exc
        if not isinstance(payload, dict):
            raise PscubeError(f"JSON の形が想定外です: {url}")
        return payload

    def get_html(self, url: str, referer: str | None = None) -> str:
        status, _final, body = self.get(url, referer=referer)
        if status == 200 and not _challenge_page(body):
            return body
        emit(f"  HTTP取得では開けないためブラウザで再試行します ({status})", flush=True)
        return _browser_html(url)


_TURNSTILE_HOOK = """
(() => {
  const timer = setInterval(() => {
    if (!window.turnstile || !window.turnstile.render || window.__tsHooked) return;
    window.__tsHooked = true;
    const orig = window.turnstile.render.bind(window.turnstile);
    window.turnstile.render = (container, params) => {
      window.__tsCallback = params && params.callback;
      return orig(container, params);
    };
    clearInterval(timer);
  }, 20);
})();
"""

_LINE_GATE_PHRASES = (
    "公式LINE追加されている方のみ見ることができます",
    "LINE連携が必要",
    "LINEから開いてください",
    "LINE連携のお願い",
)

# 案内モーダルは台表の上に被さり、未連携のときは .nc-main を空にする。
# 閉じる／DOMから外したあと、空の上書きだけを無視して台表を残す。
_LINE_GATE_JS = """() => {
  const phrases = %s;
  const matches = (text) => phrases.some((phrase) => String(text || '').includes(phrase));
  let clicked = 0;
  let removed = 0;
  const cancel = document.querySelector('#lineModalCancel');
  if (cancel) {
    cancel.click();
    clicked += 1;
  }
  const buttons = [...document.querySelectorAll('button, a, [role="button"]')];
  for (const button of buttons) {
    const label = (button.innerText || button.getAttribute('aria-label') || '').trim();
    if (!label || label.length > 12) continue;
    if (label !== '閉じる' && label !== 'あとで' && label !== '×' && label !== '✕' && label.toLowerCase() !== 'close') continue;
    const host = button.closest('#line-auth-block, #line-connect-modal, #popup, [role="dialog"]');
    if (!host && !matches(button.parentElement && button.parentElement.innerText)) continue;
    button.click();
    clicked += 1;
  }
  const ids = ['line-auth-block', 'line-connect-modal'];
  for (const id of ids) {
    const el = document.getElementById(id);
    if (!el) continue;
    el.remove();
    removed += 1;
  }
  const nodes = [...document.querySelectorAll('div, section, aside')];
  for (const el of nodes) {
    if (el === document.body || el === document.documentElement) continue;
    if (el.querySelector && el.querySelector('.nc-main table, table')) {
      const own = [...el.childNodes].filter((node) => node.nodeType === 1 && matches(node.innerText || '')).length;
      if (!own && !el.id) continue;
    }
    const text = (el.innerText || '').slice(0, 500);
    if (!matches(text)) continue;
    const style = window.getComputedStyle(el);
    const overlay = style.position === 'fixed' || style.position === 'absolute' || Number(style.zIndex) >= 1000 || el.getAttribute('role') === 'dialog' || el.id === 'popup';
    if (!overlay) continue;
    if (el.querySelector && el.querySelector('.nc-main, .nc-content')) continue;
    el.remove();
    removed += 1;
  }
  return { clicked, removed };
}""" % json.dumps(list(_LINE_GATE_PHRASES), ensure_ascii=False)

_LINE_GATE_INIT = """
(() => {
  if (window.__lineGateGuard) return;
  window.__lineGateGuard = true;
  const desc = Object.getOwnPropertyDescriptor(Element.prototype, 'innerHTML');
  if (desc && desc.set && desc.get) {
    const orig = desc.set;
    Object.defineProperty(Element.prototype, 'innerHTML', {
      configurable: true,
      enumerable: desc.enumerable,
      get: desc.get,
      set(value) {
        const empty = typeof value === 'string' && value.trim() === '';
        const cls = String(this.className || '');
        if (empty && (cls.includes('nc-main') || cls.includes('nc-content'))) return;
        return orig.call(this, value);
      },
    });
  }
  const dismiss = %s;
  const run = () => { try { dismiss(); } catch (e) {} };
  const start = () => {
    if (!document.documentElement) return;
    new MutationObserver(run).observe(document.documentElement, { childList: true, subtree: true });
    run();
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
""" % _LINE_GATE_JS


def _launch_chrome():
    """表示付き Chrome。ヘッドレスは Cloudflare の確認で止まる。Bright Data を必ず通す。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise PscubeError("Playwright が無いため Cloudflare 越えの取得ができません") from exc
    from src.scraper.proxy_config import playwright_proxy, proxy_log_label

    proxy = playwright_proxy()
    if not proxy:
        raise PscubeError(
            "Bright Data のプロキシが未設定です。SCRAPER_PROXY_HOST または SCRAPER_PROXY_SERVER を設定してください。"
        )
    identity = chrome_client_identity()
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(
        headless=False,
        channel="chrome",
        chromium_sandbox=True,
        proxy=proxy,
        args=["--window-position=-3200,-3200", "--window-size=900,700"],
        ignore_default_args=["--enable-automation", "--enable-unsafe-swiftshader"],
    )
    emit(f"  プロキシ: {proxy_log_label(proxy)}", flush=True)
    context = browser.new_context(
        locale="ja-JP",
        timezone_id="Asia/Tokyo",
        viewport={"width": 900, "height": 700},
        user_agent=identity["user_agent"],
        extra_http_headers={
            "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
            "sec-ch-ua": identity["sec_ch_ua"],
            "sec-ch-ua-mobile": identity["sec_ch_ua_mobile"],
            "sec-ch-ua-platform": identity["sec_ch_ua_platform"],
        },
    )
    context.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")
    context.add_init_script(_TURNSTILE_HOOK)
    context.add_init_script(_LINE_GATE_INIT)
    page = context.new_page()
    return playwright, browser, page


def dismiss_line_gate(page) -> dict[str, int]:
    """公式LINE案内が出ていれば閉じるか、DOMから外す。"""
    try:
        result = page.evaluate(_LINE_GATE_JS)
    except Exception:
        return {"clicked": 0, "removed": 0}
    if not isinstance(result, dict):
        return {"clicked": 0, "removed": 0}
    clicked = int(result.get("clicked") or 0)
    removed = int(result.get("removed") or 0)
    if clicked or removed:
        emit(f"  LINE案内を閉じました（click {clicked} / remove {removed}）", flush=True)
        page.wait_for_timeout(1200)
    return {"clicked": clicked, "removed": removed}


def _apply_turnstile_token(page, url: str) -> bool:
    """確認ページに sitekey があるときだけ CapSolver のトークンを渡す。"""
    sitekey = ""
    for frame in page.frames:
        sitekey = next((part for part in frame.url.split("/") if part.startswith("0x")), "")
        if sitekey:
            break
    if not sitekey:
        return False
    from src.scraper.capsolver import CapSolverError, solve_turnstile

    emit("  CapSolver で Turnstile を更新します", flush=True)
    try:
        token = solve_turnstile(url, sitekey, action="")
    except CapSolverError as exc:
        emit(f"  CapSolver では確認を解けませんでした: {failure_reason(exc)}", flush=True)
        return False
    page.evaluate(
        """(token) => {
          const input = document.querySelector('[name="cf-turnstile-response"]');
          if (input) input.value = token;
          if (typeof window.__tsCallback === 'function') window.__tsCallback(token);
        }""",
        token,
    )
    return True


def _wait_past_challenge(page, url: str) -> None:
    page.wait_for_timeout(2500)
    title = page.title() or ""
    html = page.content()
    if not _challenge_page(html) and "しばらくお待ちください" not in title:
        return
    if not _apply_turnstile_token(page, url):
        page.wait_for_timeout(8000)
    else:
        page.wait_for_timeout(6000)
    if _challenge_page(page.content()) or "しばらくお待ちください" in (page.title() or ""):
        raise PscubeError("Cloudflare の確認を通過できず、台ページを開けませんでした")


def _access_blocked(html: str) -> bool:
    return "Ray ID" in html and any(token in html for token in ("メンテナンス", "正常に利用できない", "内部サーバエラー"))


def _browser_html(url: str) -> str:
    playwright, browser, page = _launch_chrome()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        _wait_past_challenge(page, url)
        dismiss_line_gate(page)
        page.wait_for_timeout(1500)
        html = page.content()
        if _access_blocked(html):
            raise PscubeError("P'sCUBE がプロキシ経由のアクセスをメンテナンス画面で拒否しています")
        return html
    finally:
        browser.close()
        playwright.stop()


def _hall_base(page_url: str) -> str:
    parsed = urlparse(page_url)
    path = parsed.path if parsed.path.endswith("/") else parsed.path.rsplit("/", 1)[0] + "/"
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def _is_captcha_page(page) -> bool:
    title = page.title() or ""
    url = page.url or ""
    return "CAPTCHA" in title or "nc-captcha.php" in url


def _read_unit_page(page, detail: str, captured: dict[str, Any]) -> dict[str, Any] | None:
    captured.clear()
    try:
        with page.expect_response(lambda response: "/nc-m06-001.php" in response.url, timeout=20000) as waited:
            page.goto(detail, wait_until="domcontentloaded", timeout=20000)
        if _is_captcha_page(page):
            captured["captcha"] = True
            return None
        dismiss_line_gate(page)
        response = waited.value
        captured["status"] = response.status
        captured["url"] = response.url
        if response.status == 429:
            captured["limited"] = True
            captured["retry_after"] = response.headers.get("retry-after") or ""
            return None
        if not response.ok:
            return None
        title = page.title() or ""
        if "しばらくお待ちください" in title or "Just a moment" in title:
            return None
        payload = response.json()
    except Exception as exc:
        if _is_captcha_page(page):
            captured["captcha"] = True
        captured["error"] = type(exc).__name__
        return None
    return payload if isinstance(payload, dict) else None


def _open_slot_list(page, list_url: str) -> dict[str, Any]:
    """一覧の通信を読む。確認画面のときは短く待ってやり直す。"""
    last_error = "スロット一覧を読めませんでした"
    for attempt in range(3):
        payload: Any = {}
        try:
            with page.expect_response(
                lambda response: "nc-m03-001.php" in response.url and response.status == 200,
                timeout=25000,
            ) as waited:
                page.goto(list_url, wait_until="domcontentloaded", timeout=45000)
            _wait_past_challenge(page, list_url)
            dismiss_line_gate(page)
            page.wait_for_timeout(1500)
            payload = waited.value.json()
        except Exception as exc:
            last_error = type(exc).__name__
        if isinstance(payload, dict) and payload.get("Ki"):
            return payload
        if (isinstance(payload, dict) and payload.get("redirect_captcha")) or _is_captcha_page(page):
            last_error = "確認画面が続いたため、この店舗の台データは取得できませんでした"
            emit(f"  確認画面のため一覧をやり直します ({attempt + 1}/3)", flush=True)
        else:
            emit(f"  一覧を読み直します ({attempt + 1}/3)", flush=True)
        if attempt < 2:
            page.wait_for_timeout(5000 * (attempt + 1))
    raise PscubeError(last_error)


def _unit_cache_path(hall: Hall):
    return hall_data_dir(hall.name, PROJECT_ROOT / "data" / "pscube") / "unit_cache.json"


def _business_ymd() -> str:
    return datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y%m%d")


def _load_unit_cache(hall: Hall) -> dict[str, dict[str, Any]]:
    path = _unit_cache_path(hall)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict) or str(payload.get("ymd") or "") != _business_ymd():
        return {}
    rows = payload.get("rows") or {}
    if not isinstance(rows, dict):
        return {}
    return {str(key): value for key, value in rows.items() if isinstance(value, dict)}


def _save_unit_cache(hall: Hall, rows: dict[str, dict[str, Any]]) -> None:
    path = _unit_cache_path(hall)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"ymd": _business_ymd(), "rows": rows}, ensure_ascii=False),
        encoding="utf-8",
    )


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


def _abort_unused(route) -> None:
    url = route.request.url.casefold()
    if route.request.resource_type in _BLOCKED_TYPES or any(token in url for token in _BLOCKED_URL_TOKENS):
        route.abort()
        return
    route.continue_()


_NETCUBE_STATUS: dict[str, Any] = {"limited": False, "retry_after": 0, "remaining": 0}


def _uses_netcube(hall: Hall) -> bool:
    return str((hall.source_config or {}).get("variant") or "") in {"dmm", "netcube"}


def _retry_after_seconds(captured: dict[str, Any]) -> int:
    raw = str(captured.get("retry_after") or "").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 1800


def _fetch_netcube_hall(hall: Hall) -> list[dict[str, Any]]:
    """P'sCUBE の機種一覧から、監視対象の台詳細JSONを読む。"""
    base = _hall_base(hall.source_url or "")
    list_url = urljoin(base, "cgi-bin/nc-v03-001.php?cd_ps=2")
    emit(f"  スロット一覧: {list_url}", flush=True)
    _NETCUBE_STATUS.update(limited=False, retry_after=0, remaining=0)
    playwright, browser, page = _launch_chrome()
    listed: dict[str, Any] = {}

    try:
        page.route("**/*", _abort_unused)
        page.route("**/nc-m06-003.php*", lambda route: route.abort())
        page.route("**/nc-m06-008.php*", lambda route: route.abort())
        listed["body"] = _open_slot_list(page, list_url)
        units = netcube_slot_units(listed.get("body") or {})
        if not units:
            raise PscubeError("スロット一覧から監視対象の台番号を読めませんでした")
        emit(f"  監視対象 {len(units)} 台", flush=True)
        cached = _load_unit_cache(hall)
        rows = list(cached.values())
        pending = [(name, dai) for name, dai in units if dai not in cached]
        if cached:
            emit(f"  当日キャッシュ {len(cached)} 台 / 残り {len(pending)} 台", flush=True)
        captured: dict[str, Any] = {}
        misses = 0
        for index, (name, dai) in enumerate(pending, start=1):
            detail = urljoin(base, f"cgi-bin/nc-v06-001.php?cd_dai={dai}")
            payload = _read_unit_page(page, detail, captured)
            if captured.get("captcha"):
                emit("  確認画面のため、この台をやり直します", flush=True)
                page.wait_for_timeout(5000)
                payload = _read_unit_page(page, detail, captured)
            if captured.get("captcha"):
                raise PscubeError("確認画面が続いたため、この店舗の台データは取得できませんでした")
            if captured.get("limited"):
                retry_after = _retry_after_seconds(captured)
                _NETCUBE_STATUS.update(
                    limited=True,
                    retry_after=retry_after,
                    remaining=sum(1 for _name, dai in units if dai not in cached),
                )
                emit(
                    f"  アクセス制限のため残りは次回に回します（現在 {len(rows)} 台 / Retry-After {retry_after}秒）",
                    flush=True,
                )
                break
            if payload is None and misses == 3:
                emit("  連続で読めないため少し待って再開します", flush=True)
                page.wait_for_timeout(5000)
                payload = _read_unit_page(page, detail, captured)
                if captured.get("limited"):
                    retry_after = _retry_after_seconds(captured)
                    _NETCUBE_STATUS.update(
                        limited=True,
                        retry_after=retry_after,
                        remaining=sum(1 for _name, dai in units if dai not in cached),
                    )
                    emit(
                        f"  アクセス制限のため残りは次回に回します（現在 {len(rows)} 台 / Retry-After {retry_after}秒）",
                        flush=True,
                    )
                    break
            row = parse_netcube_unit(payload or {}, hall.name, machine_name=name) if payload else None
            if row:
                rows.append(row)
                cached[str(row.get("dai_no") or dai)] = row
                _save_unit_cache(hall, cached)
                misses = 0
            else:
                misses += 1
                emit(
                    f"    [{index}/{len(pending)}] {name} {dai} を読めませんでした"
                    f" status={captured.get('status')} {captured.get('error') or ''}",
                    flush=True,
                )
            if index == 1 or index % 20 == 0 or index == len(pending):
                emit(f"    [{index}/{len(pending)}] 取得 {len(rows)} 台", flush=True)
            page.wait_for_timeout(1500)
        _save_unit_cache(hall, cached)
        return _watched(rows)
    finally:
        browser.close()
        playwright.stop()


def _watched(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if not is_watched(str(row.get("machine_name") or "")):
            continue
        key = (str(row.get("machine_name")), str(row.get("dai_no")))
        if key in seen:
            continue
        seen.add(key)
        kept.append(row)
    return kept


def fetch_pscube_hall(hall: Hall, client: PscubeClient | None = None) -> list[dict[str, Any]]:
    page_url = (hall.source_url or "").strip()
    if _uses_netcube(hall):
        return _fetch_netcube_hall(hall)
    session = client or PscubeClient()
    if not page_url:
        raise PscubeError(f"{hall.name}: source_url がありません")
    emit(f"P'sCUBEへアクセス: {page_url}", flush=True)
    html = session.get_html(page_url)
    list_url, dai_url = discover_json_urls(html, page_url)
    variant = str((hall.source_config or {}).get("variant") or "")
    if list_url is None and variant == "dmm":
        list_url, dai_url = candidate_json_urls(page_url)
    if list_url and dai_url:
        try:
            listed = session.get_json(list_url, page_url)
            dai = session.get_json(dai_url, page_url)
            rows = _watched(parse_assist_json(listed, dai, hall.name))
            if rows:
                emit(f"  JSONから監視対象 {len(rows)} 台", flush=True)
                return rows
        except PscubeError as exc:
            emit(f"  JSON取得をスキップ: {failure_reason(exc)}", flush=True)
    rows = _watched(parse_pscube_tables(html, hall.name))
    if rows:
        emit(f"  HTMLから監視対象 {len(rows)} 台", flush=True)
        return rows
    links = follow_links(parse_document(html), page_url, page_url)
    emit(f"  台ページ候補: {len(links)}", flush=True)
    combined: list[dict[str, Any]] = []
    for index, link in enumerate(links, start=1):
        emit(f"  [{index}/{len(links)}] {link}", flush=True)
        try:
            child = session.get_html(link, referer=page_url)
        except PscubeError as exc:
            emit(f"    開けませんでした: {failure_reason(exc)}", flush=True)
            continue
        child_list, child_dai = discover_json_urls(child, link)
        if child_list and child_dai:
            try:
                combined.extend(
                    _watched(
                        parse_assist_json(
                            session.get_json(child_list, link),
                            session.get_json(child_dai, link),
                            hall.name,
                        )
                    )
                )
            except PscubeError as exc:
                emit(f"    JSONスキップ: {failure_reason(exc)}", flush=True)
        heading = ""
        for block in parse_document(child):
            if block.kind == "heading" and block.text:
                heading = block.text
                break
        combined.extend(_watched(parse_pscube_tables(child, hall.name, fallback_name=heading)))
    deduped = _watched(combined)
    emit(f"  取得 {len(deduped)} 台", flush=True)
    return deduped


def fetch_pscube_data(halls: list[Hall] | None = None) -> FetchResult:
    if (os.getenv("PSCUBE_FETCH") or "").strip() != "1":
        emit("P'sCUBE は Cloudflare ブロックのため、今回の取得からスキップします", flush=True)
        return FetchResult(
            source="pscube",
            hall_name="P'sCUBE",
            machines=[],
            extra={
                "skipped": True,
                "reason": "Cloudflareブロックのため一時停止",
                "halls": [],
                "failed_halls": [],
            },
        )
    return _fetch_pscube_data_live(halls)


def _fetch_pscube_data_live(halls: list[Hall] | None = None) -> FetchResult:
    targets = halls if halls is not None else halls_for_source("pscube")
    if not targets:
        raise PscubeError("source=pscube の有効ホールが config/halls.json にありません")
    started = time.monotonic()
    combined: list[dict[str, Any]] = []
    fetched: list[str] = []
    failed: list[str] = []
    reasons: list[str] = []
    details: list[dict[str, str]] = []
    from src.scraper.smart_retry import get_ledger

    ledger = get_ledger()
    for hall in targets:
        if ledger.should_skip(hall.id, "pscube"):
            failed.append(hall.name)
            details.append(failure_record(hall.name, "再試行待ち"))
            emit(f"  {hall.name}: 再試行待ちのためスキップ", flush=True)
            continue
        while True:
            try:
                payload = run_within(
                    lambda hall=hall: fetch_pscube_hall(hall),
                    timeout=1800.0 if _uses_netcube(hall) else 900.0,
                    label=hall.name,
                )
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:
                safe = summarize_failure(str(exc))
                emit(f"  {hall.name} の取得失敗: {safe}", flush=True)
                failed.append(hall.name)
                reasons.append(f"{hall.name}: {safe}")
                details.append(failure_record(hall.name, safe))
                if is_deferrable(str(exc)):
                    ledger.defer(hall.id, "pscube", exc)
                break
            limited = bool(_uses_netcube(hall) and _NETCUBE_STATUS["limited"] and _NETCUBE_STATUS["remaining"] > 0)
            if payload:
                hall_result = FetchResult(source="pscube", hall_name=hall.name, machines=payload)
                hall_path = hall_result.write_incoming(hall_data_dir(hall.name, PROJECT_ROOT / "data" / "pscube"))
                emit(f"  {hall.name}: {len(payload)} 件を {hall_path} に保存", flush=True)
            if limited:
                wait = int(_NETCUBE_STATUS["retry_after"] or 1800)
                ledger.defer(hall.id, "pscube", "HTTP 429", delay_sec=wait)
                emit(f"  {hall.name}: 一時スキップ（429上限、{wait}秒後に再試行）", flush=True)
                if payload:
                    combined.extend(payload)
                    fetched.append(hall.name)
                else:
                    failed.append(hall.name)
                    reasons.append(f"{hall.name}: 429上限")
                    details.append(failure_record(hall.name, "429上限"))
                break
            if not payload:
                failed.append(hall.name)
                reasons.append(f"{hall.name}: 0 件")
                details.append(failure_record(hall.name, "0 件"))
                emit(f"  {hall.name} の取得失敗: 0 件", flush=True)
                break
            combined.extend(payload)
            fetched.append(hall.name)
            break
        sleep_hall_gap()
    elapsed = time.monotonic() - started
    if not combined:
        detail = " / ".join(reasons)
        raise PscubeError("P'sCUBE の取得に失敗したため incoming.json は更新していません。" + (f" {detail}" if detail else ""))
    return FetchResult(
        source="pscube",
        hall_name=fetched[0] if len(fetched) == 1 else "複数店舗",
        machines=combined,
        extra={
            "halls": fetched,
            "failed_halls": failed,
            "failure_details": details,
            "elapsed_sec": round(elapsed, 1),
            "elapsed_label": format_duration(elapsed),
        },
    )
