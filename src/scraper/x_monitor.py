"""Xアカウントのイベント告知を定期取得し、本日のイベントへ無人登録する。"""

from __future__ import annotations

import fcntl
import html
import json
import os
import random
import re
import threading
import time
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from datetime import date as date_cls
from datetime import time as time_cls
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src.analyzer.judgment import PROJECT_ROOT
from src.dashboard.events import JST
from src.dashboard.events import apply_auto_events
from src.dashboard.events import group_hall_names
from src.scraper.halls import GROUP_LABELS
from src.scraper.halls import PREFECTURE_BY_LABEL
from src.scraper.halls import PREFECTURE_CATALOG
from src.scraper.halls import load_halls

CONFIG_PATH = PROJECT_ROOT / "config" / "x_monitors.json"
STATE_PATH = PROJECT_ROOT / "data" / "logs" / "x_monitor_state.json"
DAILY_STATUS_PATH = PROJECT_ROOT / "data" / "events" / "x_monitor_status.json"
LOG_PATH = PROJECT_ROOT / "data" / "logs" / "x_monitor.log"
LOCK_PATH = PROJECT_ROOT / "data" / "logs" / "x_monitor.lock"
DEFAULT_INTERVAL_MIN = 60
DEFAULT_GAP_MIN_SEC = 30
DEFAULT_GAP_MAX_SEC = 180
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_RSS_CONTENT = {"content": "http://purl.org/rss/1.0/modules/content/"}
_DIGIT_RE = re.compile(r"【\s*(\d+)\s*がつく日】")
_DATE_YMD_RE = re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_DATE_MD_RE = re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_SLASH_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})\s*/\s*(\d{1,2})(?!\d)")
_CHUNK_RE = re.compile(r"[0-9]+|[A-Za-z]+|[ァ-ヶー]+|[一-龯々]+")
_DECHAU_ROW_RE = re.compile(
    r'<span class="days">([^<]+)</span>\s*<span class="prefs">([^<]+)</span>.*?<h3>\s*([^<]+?)\s*</h3>.*?<h4>(.*?)</h4>',
    re.S,
)
_SECTION_LINES = {"注目のイベント", "注目の特定日", "周年", "月一", "スケジュール"}
_INLINE_SPLIT_RE = re.compile(r"\s*[:：]\s*")
_BRACKET_RE = re.compile(r"^(.+?)【([^】]+)】$")
_STOP_MARKS = ("明日の画像", "本日の画像", "今日の画像")
_IGNORE_LINES = {"明日のオシホール", "本日のオシホール", "今日のオシホール", "表示を押すと目次が出ます。"}
_COVERAGE_WORDS = ("取材", "来店", "景品", "収録", "入荷")
_GROUP_WORDS = (
    ("ワンダーランド", "wonderland"),
    ("プラザ", "plaza"),
    ("玉屋", "tamaya"),
    ("123", "123"),
    ("dstation", "dstation"),
    ("dステーション", "dstation"),
    ("ディーステーション", "dstation"),
)

_thread: threading.Thread | None = None
_stop = threading.Event()


def load_monitor_config(path: Path | None = None) -> dict:
    config_path = path or CONFIG_PATH
    if not config_path.is_file():
        return {"interval_min": DEFAULT_INTERVAL_MIN, "accounts": []}
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return {"interval_min": DEFAULT_INTERVAL_MIN, "accounts": []}
    accounts = [item for item in (payload.get("accounts") or []) if isinstance(item, dict)]
    schedule = payload.get("schedule") if isinstance(payload.get("schedule"), dict) else {}
    regions = [item for item in (payload.get("regions") or []) if isinstance(item, dict)]
    fetch = payload.get("fetch") if isinstance(payload.get("fetch"), dict) else {}
    try:
        interval = int(payload.get("interval_min") or DEFAULT_INTERVAL_MIN)
    except (TypeError, ValueError):
        interval = DEFAULT_INTERVAL_MIN
    return {
        "interval_min": max(5, interval),
        "accounts": accounts,
        "schedule": schedule,
        "regions": regions,
        "fetch": fetch,
    }


def account_handle(account: dict) -> str:
    return str(account.get("handle") or account.get("id") or "").strip().lstrip("@")


def account_prefectures(account: dict, config: dict | None = None) -> list[str]:
    """アカウント固有の県が無ければ、所属エリアの県リストを使う。"""
    raw = account.get("prefectures")
    if isinstance(raw, list) and any(str(item).strip() for item in raw):
        return [str(item).strip() for item in raw if str(item).strip()]
    region_id = str(account.get("region") or "").strip()
    payload = config if config is not None else load_monitor_config()
    for region in payload.get("regions") or []:
        if str(region.get("id") or "") != region_id:
            continue
        values = region.get("prefectures") or []
        return [str(item).strip() for item in values if str(item).strip()]
    return []


def enabled_accounts(config: dict | None = None) -> list[dict]:
    payload = config if config is not None else load_monitor_config()
    rows = []
    for account in payload.get("accounts") or []:
        if not isinstance(account, dict) or not account.get("enabled", True):
            continue
        if not account_handle(account):
            continue
        rows.append(account)
    return rows


def fetch_gap_seconds(config: dict | None = None) -> tuple[float, float]:
    """情報源と情報源のあいだに空ける秒数。数秒から数分が初期値で、設定で数十分まで広げられる。"""
    payload = config if config is not None else load_monitor_config()
    raw = payload.get("fetch") if isinstance(payload.get("fetch"), dict) else {}

    def _num(key: str, default: float) -> float:
        try:
            value = raw.get(key)
            return float(default if value is None else value)
        except (TypeError, ValueError):
            return default

    lo = max(0.0, _num("gap_min_sec", DEFAULT_GAP_MIN_SEC))
    hi = max(lo, _num("gap_max_sec", DEFAULT_GAP_MAX_SEC))
    return lo, hi


def pause_between_accounts(config: dict | None = None) -> float:
    lo, hi = fetch_gap_seconds(config)
    delay = random.uniform(lo, hi) if hi > 0 else 0.0
    if delay > 0:
        _log(f"次の情報源まで {delay:.0f} 秒待機")
        time.sleep(delay)
    return delay


def monitor_status() -> dict:
    if not STATE_PATH.is_file():
        config = load_monitor_config()
        return {
            "interval_min": config["interval_min"],
            "accounts": [str(item.get("handle") or "") for item in config["accounts"] if item.get("enabled", True)],
        }
    try:
        payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def schedule_settings(config: dict | None = None) -> dict:
    payload = config if config is not None else load_monitor_config()
    raw = payload.get("schedule") if isinstance(payload.get("schedule"), dict) else {}

    def _num(key: str, default: int) -> int:
        try:
            value = raw.get(key)
            return int(default if value is None else value)
        except (TypeError, ValueError):
            return default

    hour = min(23, max(0, _num("hour", 18)))
    minute = min(59, max(0, _num("minute", 30)))
    retry = max(15, _num("retry_minutes", 60))
    last_hour = min(23, max(hour, _num("last_hour", 23)))
    return {"hour": hour, "minute": minute, "retry_minutes": retry, "last_hour": last_hour}


def read_daily_status() -> dict:
    if not DAILY_STATUS_PATH.is_file():
        return {}
    try:
        payload = json.loads(DAILY_STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_daily_status(payload: dict) -> None:
    DAILY_STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    DAILY_STATUS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def next_attempt_at(now: datetime | None = None) -> datetime:
    """次にXへアクセスしてよい時刻。完了済みなら翌日の初回時刻。"""
    current = _as_jst(now)
    settings = schedule_settings()
    status = read_daily_status()
    if status.get("completed_on") == current.date().isoformat():
        return _slot_at(current.date() + timedelta(days=1), settings)
    first = _slot_at(current.date(), settings)
    if current < first:
        return first
    last_attempt = _parse_stamp(status.get("last_attempt_at"))
    last_slot = datetime.combine(
        current.date(),
        time_cls(settings["last_hour"], settings["minute"]),
        tzinfo=JST,
    )
    slot = first
    while slot <= last_slot:
        already = last_attempt is not None and last_attempt >= slot
        if not already:
            return current if slot <= current else slot
        slot += timedelta(minutes=settings["retry_minutes"])
    return _slot_at(current.date() + timedelta(days=1), settings)


def attempt_due(now: datetime | None = None) -> bool:
    current = _as_jst(now)
    return next_attempt_at(current) <= current + timedelta(seconds=1)


def sync_x_events(*, force: bool = False) -> dict:
    """監視アカウントの最新告知を読み、イベントファイルへ反映する。"""
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        from src.run_gate import acquisition_allowed

        if not acquisition_allowed():
            return {"skipped": True, "reason": "安全待機中"}
    lock = _acquire_lock()
    if lock is None:
        return {"skipped": True, "reason": "前回の監視が継続中です"}
    try:
        return _sync_unlocked(force=force)
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


def ensure_background_monitor() -> None:
    """ダッシュボード常駐中、毎日18:30から翌日分が取れるまで監視する。"""
    global _thread
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("X_MONITOR_DISABLE") == "1":
        return
    from src.run_gate import acquisition_allowed

    if not acquisition_allowed():
        _log("Xイベント監視は安全待機中のため起動しません")
        return
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    settings = schedule_settings()
    gap_lo, gap_hi = fetch_gap_seconds()
    _log(
        "Xイベント監視を常駐開始"
        f"（毎日 {settings['hour']:02d}:{settings['minute']:02d}、未取得なら{settings['retry_minutes']}分おき、"
        f"情報源の間隔 {gap_lo:.0f}–{gap_hi:.0f} 秒）"
    )
    _thread = threading.Thread(target=_loop, name="x-event-monitor", daemon=True)
    _thread.start()


def is_announcement(title: str, body: str) -> bool:
    head = f"{title}\n{body[:600]}"
    if "結果" in title:
        return False
    if "スケジュール" in title and "オシホール" not in title and "すろいべ" not in title:
        return False
    if any(mark in head for mark in ("明日のオシホール", "本日のオシホール", "今日のオシホール")):
        return True
    if "すろいべ" in title or "取材一覧" in head:
        return True
    if _SLASH_DATE_RE.search(title) and re.search(r"[都道府県]", title):
        return True
    return _DIGIT_RE.search(head) is not None and (_DATE_YMD_RE.search(head) is not None or _DATE_MD_RE.search(head) is not None)


def event_date_from_post(title: str, body: str, published: datetime) -> str | None:
    stamp = published.astimezone(JST)
    blob = f"{title}\n{body[:500]}"
    matched = _DATE_YMD_RE.search(blob)
    if matched:
        return _safe_date(int(matched.group(1)), int(matched.group(2)), int(matched.group(3)))
    matched = _DATE_MD_RE.search(blob)
    if matched:
        return _safe_date(stamp.year, int(matched.group(1)), int(matched.group(2)))
    matched = _SLASH_DATE_RE.search(title) or _SLASH_DATE_RE.search(blob)
    if matched:
        return _safe_date(stamp.year, int(matched.group(1)), int(matched.group(2)))
    if "明日" in title or "明日のオシホール" in blob[:120]:
        return (stamp.date() + timedelta(days=1)).isoformat()
    if any(word in title for word in ("本日", "今日")):
        return stamp.date().isoformat()
    return None


def parse_announcement(
    text: str,
    *,
    event_date: str,
    handle: str,
    source_url: str = "",
    prefectures: list[str] | None = None,
) -> list[dict]:
    """告知本文からホールまたは系列ごとのイベントを作る。"""
    preferred = [str(item) for item in (prefectures or []) if str(item).strip()]
    if "取材一覧" in text[:1200]:
        return _parse_bulletin(
            text,
            event_date=event_date,
            handle=handle,
            source_url=source_url,
            prefectures=preferred,
        )
    digit = ""
    section_coverage = ""
    current: dict | None = None
    grouped: dict[str, dict] = {}

    def flush() -> None:
        nonlocal current
        if current is None:
            return
        if not current["titles"] and current.get("section"):
            current["titles"] = [str(current["section"])]
        _store_target(grouped, current, event_date, handle, source_url, digit, preferred)
        current = None

    for line in _announcement_lines(text):
        digit_match = _DIGIT_RE.fullmatch(line.replace(" ", ""))
        if digit_match:
            flush()
            digit = f"{digit_match.group(1)}がつく日"
            section_coverage = ""
            continue
        section = _coverage_section(line)
        if section or _is_section_line(line):
            flush()
            found = _digit_from_section(line)
            if found:
                digit = found
                section_coverage = ""
            elif section:
                section_coverage = section
                digit = ""
            continue
        if section_coverage and not _is_coverage_line(line):
            flush()
            group_id = match_group_banner(line)
            hall_id = None if group_id else match_hall_id(line, prefectures=preferred)
            if group_id or hall_id:
                current = {
                    "group": group_id or "",
                    "hall_id": hall_id or "",
                    "label": line,
                    "titles": [],
                    "section": section_coverage,
                }
            continue
        inline = _inline_target(line, prefectures=preferred)
        if inline is not None:
            flush()
            _store_target(grouped, inline, event_date, handle, source_url, digit, preferred)
            continue
        group_id = match_group_banner(line)
        hall_id = None if group_id else match_hall_id(line, prefectures=preferred)
        if group_id or hall_id:
            flush()
            current = {"group": group_id or "", "hall_id": hall_id or "", "label": line, "titles": []}
            continue
        if _is_untracked_place(line):
            flush()
            current = {"group": "", "hall_id": "", "label": line, "titles": []}
            continue
        if current is not None and _is_title_line(line):
            current["titles"].append(line)
    flush()
    return list(grouped.values())


def _parse_bulletin(
    text: str,
    *,
    event_date: str,
    handle: str,
    source_url: str,
    prefectures: list[str],
) -> list[dict]:
    """東スロ形式。【N日】は特定日、☆見出しのあとの・行は取材。"""
    digit = ""
    current_title = ""
    grouped: dict[str, dict] = {}
    for line in _announcement_lines(text):
        bracket = re.fullmatch(r"【\s*(.+?)\s*】", line)
        if line.startswith("★") or bracket:
            found = _digit_from_section(bracket.group(1) if bracket else line)
            if found:
                digit = found
            current_title = ""
            continue
        if line.startswith("☆"):
            current_title = line.lstrip("☆").strip()
            continue
        if line.startswith(("・", "●", "▶", "▷")):
            payload = line[1:].strip()
        elif digit and not current_title and "。" not in line and len(line) <= 40:
            payload = line
        else:
            continue
        for name in _hall_name_parts(payload, prefectures):
            hall_id = match_hall_id(name, prefectures=prefectures)
            if not hall_id:
                continue
            titles = [current_title] if current_title else []
            target = {"group": "", "hall_id": hall_id, "label": name, "titles": titles}
            _store_target(grouped, target, event_date, handle, source_url, "" if current_title else digit, prefectures)
    return list(grouped.values())


def match_hall_id(value: str, prefectures: list[str] | None = None) -> str | None:
    """有効ホールの店名・別名へ最長一致で hall_id を付ける。同じ長さなら情報源の県を優先する。"""
    needle = _core_label(value)
    if len(needle) < 3:
        return None
    preferred = {str(item) for item in (prefectures or []) if str(item).strip()}
    best_id = None
    best_len = 0
    best_pref = False
    for hall in load_halls(include_disabled=False):
        labels = [hall.name, *hall.aliases, hall.id]
        for label in labels:
            key = _core_label(label)
            if len(key) < 3 or not _same_place(needle, key):
                continue
            in_pref = bool(preferred) and hall.prefecture in preferred
            if len(key) > best_len or (len(key) == best_len and in_pref and not best_pref):
                best_id = hall.id
                best_len = len(key)
                best_pref = in_pref
    if best_id or not preferred:
        return best_id
    return _unique_chunk_match(needle, preferred)


def match_group_banner(value: str) -> str | None:
    text = _norm_label(value)
    if "全店" not in text:
        return None
    pairs = list(_GROUP_WORDS)
    for group_id, label in GROUP_LABELS.items():
        if group_id == "other":
            continue
        token = label.replace("グループ", "").replace("系列", "").strip()
        if token:
            pairs.append((token, group_id))
    ranked = sorted(pairs, key=lambda item: len(_norm_label(item[0])), reverse=True)
    for word, group_id in ranked:
        key = _norm_label(word)
        if key and key in text:
            return group_id
    return None


def fetch_account_posts(account: dict, cache: dict | None = None) -> list[dict]:
    parser = str(account.get("parser") or "").strip()
    feed_url = str(account.get("feed_url") or "").strip()
    if parser == "dechau":
        prefs = account.get("prefectures") if isinstance(account.get("prefectures"), list) else []
        return fetch_dechau_posts(feed_url or "https://www.dechau.com/", prefs, cache)
    posts = fetch_feed_posts(feed_url, cache)
    announcements = [post for post in posts if is_announcement(post["title"], post["body"])]
    if announcements:
        return announcements
    handle = str(account.get("handle") or "").strip().lstrip("@")
    if handle:
        announcements = [post for post in fetch_x_html_posts(handle) if is_announcement(post["title"], post["body"])]
    if announcements:
        return announcements
    if account.get("browser") and handle:
        return [post for post in fetch_x_browser_posts(handle) if is_announcement(post["title"], post["body"])]
    return []


def fetch_feed_posts(feed_url: str, cache: dict | None = None) -> list[dict]:
    if not feed_url:
        return []
    key = f"feed:{feed_url}"
    if cache is not None and key in cache:
        return list(cache[key])
    posts = posts_from_feed_xml(_http_text(feed_url))
    if cache is not None:
        cache[key] = posts
    return list(posts)


def posts_from_feed_xml(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    if _tag_local(root.tag) == "feed":
        return _atom_entries(root)
    posts = []
    for item in root.findall("./channel/item")[:12]:
        title = html.unescape(item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").split("?", 1)[0].strip()
        published = _parse_stamp(item.findtext("pubDate") or "")
        raw = item.findtext("content:encoded", default="", namespaces=_RSS_CONTENT) or item.findtext("description") or ""
        posts.append({"title": title, "url": link, "published": published, "body": _html_to_text(raw)})
    return posts


def fetch_dechau_posts(feed_url: str, prefectures: list[str] | None, cache: dict | None = None) -> list[dict]:
    url = feed_url or "https://www.dechau.com/"
    key = f"dechau:{url}"
    if cache is not None and key in cache:
        page = cache[key]
    else:
        page = _http_text(url)
        if cache is not None:
            cache[key] = page
    return parse_dechau_html(page, prefectures)


def parse_dechau_html(page: str, prefectures: list[str] | None = None, today: date_cls | None = None) -> list[dict]:
    """でちゃう！公開スケジュールから、指定県かつ今日から7日以内の行だけを告知にする。"""
    anchor = today or datetime.now(JST).date()
    allowed = {str(item).strip() for item in (prefectures or []) if str(item).strip()}
    posts = []
    for day_text, pref_text, hall, title in _DECHAU_ROW_RE.findall(page):
        pref_id = PREFECTURE_BY_LABEL.get(html.unescape(pref_text).strip())
        if allowed and pref_id not in allowed:
            continue
        matched = _SLASH_DATE_RE.search(day_text)
        if not matched or not pref_id:
            continue
        event_date = _safe_date(anchor.year, int(matched.group(1)), int(matched.group(2)))
        if not event_date:
            continue
        day = date_cls.fromisoformat(event_date)
        if day < anchor or day > anchor + timedelta(days=7):
            continue
        hall_name = html.unescape(hall).strip()
        event_title = _html_to_text(title).strip() or "取材"
        if not hall_name:
            continue
        published = datetime.combine(day, time_cls(12, 0), tzinfo=JST)
        posts.append(
            {
                "title": f"{day_text.strip()} {html.unescape(pref_text).strip()} {hall_name}",
                "body": f"{hall_name}\n{event_title}",
                "url": "https://www.dechau.com/",
                "published": published,
                "prefectures": [pref_id],
            }
        )
    return posts


def fetch_x_html_posts(handle: str) -> list[dict]:
    """公開プロフィールHTMLに本文が含まれる場合だけ拾う。"""
    try:
        page = _http_text(f"https://x.com/{handle}")
    except OSError:
        return []
    chunks = re.findall(r"明日のオシホール.{0,800}|本日のオシホール.{0,800}|【\d+がつく日】.{0,800}", page)
    now = datetime.now(JST)
    return [{"title": chunk[:40], "body": chunk, "url": f"https://x.com/{handle}", "published": now} for chunk in chunks[:5]]


def fetch_x_browser_posts(handle: str) -> list[dict]:
    """feed が無いアカウント向けに、公開プロフィールをブラウザで読む。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _log(f"@{handle} のブラウザ取得をスキップ（Playwright 未導入）")
        return []
    texts: list[str] = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, channel="chrome")
            page = browser.new_page(locale="ja-JP")
            page.goto(f"https://x.com/{handle}", wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(2500)
            texts = [item.inner_text() for item in page.locator("article").all()[:8]]
            browser.close()
    except Exception as exc:
        _log(f"@{handle} のブラウザ取得に失敗: {exc}")
        return []
    now = datetime.now(JST)
    posts = []
    for text in texts:
        if "明日のオシホール" not in text and "がつく日" not in text and "取材" not in text:
            continue
        posts.append({"title": text.splitlines()[0][:80], "body": text, "url": f"https://x.com/{handle}", "published": now})
    return posts


def posts_to_events(posts: list[dict], handle: str, prefectures: list[str] | None = None) -> list[dict]:
    events: list[dict] = []
    seen: set[str] = set()
    for post in posts:
        if not is_announcement(post.get("title") or "", post.get("body") or ""):
            continue
        published = post.get("published") or datetime.now(JST)
        if not isinstance(published, datetime):
            published = datetime.now(JST)
        event_date = event_date_from_post(post.get("title") or "", post.get("body") or "", published)
        if not event_date:
            continue
        parsed = parse_announcement(
            f"{post.get('title') or ''}\n{post.get('body') or ''}",
            event_date=event_date,
            handle=handle,
            source_url=str(post.get("url") or ""),
            prefectures=_scoped_prefectures(post, prefectures),
        )
        for item in parsed:
            key = item["source_key"]
            if key in seen:
                continue
            seen.add(key)
            events.append(item)
    return events


def _sync_unlocked(*, force: bool = False) -> dict:
    now = datetime.now(JST)
    if not force and not attempt_due(now):
        nxt = next_attempt_at(now)
        return {
            "skipped": True,
            "reason": "本日の監視は完了しているか、次の試行時刻前です",
            "next_run_at": nxt.isoformat(timespec="seconds"),
        }
    _record_attempt(now)
    config = load_monitor_config()
    incoming: list[dict] = []
    used_accounts: list[str] = []
    refreshed_sources: list[str] = []
    post_count = 0
    errors: list[str] = []
    target_date = (now.date() + timedelta(days=1)).isoformat()
    done_before = _done_handles(now)
    pending = [
        account
        for account in enabled_accounts(config)
        if force or account_handle(account) not in done_before
    ]
    ready_counts: dict[str, int] = {}
    cache: dict = {}
    seen_feeds: set[str] = set()
    try:
        for index, account in enumerate(pending):
            feed = str(account.get("feed_url") or "").strip()
            if index and feed not in seen_feeds:
                pause_between_accounts(config)
            if feed:
                seen_feeds.add(feed)
            handle = account_handle(account)
            _log(f"@{handle} を取得（{index + 1}/{len(pending)}）")
            try:
                posts = fetch_account_posts(account, cache)
            except Exception as exc:
                errors.append(f"{handle}: {exc}")
                _log(f"@{handle} の取得に失敗: {exc}")
                continue
            post_count += len(posts)
            used_accounts.append(handle)
            events = posts_to_events(posts, handle, prefectures=account_prefectures(account, config))
            if posts:
                refreshed_sources.append(f"x:{handle}")
                incoming.extend(events)
            names = _target_hall_names(events, target_date)
            if names:
                ready_counts[handle] = len(names)
        result = apply_auto_events(incoming, refreshed_sources=refreshed_sources)
    except Exception as exc:
        errors.append(str(exc))
        result = {"added": 0, "updated": 0, "removed": 0}
        incoming = []
        ready_counts = {}
        _log(f"Xイベント監視に失敗: {exc}")
    else:
        for handle, count in ready_counts.items():
            _mark_account_done(now, handle, count)
        _log(
            "Xイベント監視 "
            f"accounts={','.join(used_accounts) or '-'} "
            f"posts={post_count} added={result['added']} updated={result['updated']} removed={result.get('removed', 0)}"
        )
    error = " / ".join(errors)
    hall_names = _target_hall_names(incoming, target_date)
    required = {account_handle(account) for account in enabled_accounts(config)}
    done = _done_handles(now)
    notified = False
    completed = bool(required) and required <= done
    if completed:
        counts = _done_counts(now)
        hall_count = sum(counts.values()) or len(hall_names)
        already = read_daily_status().get("notified_for") == target_date
        if force or not already:
            notified = _notify_capture(target_date, hall_count, sorted(required))
        _mark_complete(now, target_date, hall_count, sorted(required), notified or already)
    else:
        status = read_daily_status()
        status["last_result"] = "error" if error and not ready_counts else "waiting"
        status["last_error"] = error
        status["next_run_at"] = next_attempt_at(datetime.now(JST)).isoformat(timespec="seconds")
        write_daily_status(status)
    state = {
        "last_synced_at": datetime.now(JST).isoformat(timespec="seconds"),
        "interval_min": config["interval_min"],
        "accounts": used_accounts,
        "posts": post_count,
        "added": result["added"],
        "updated": result["updated"],
        "removed": result.get("removed", 0),
        "error": error,
        "target_date": target_date,
        "target_count": len(hall_names),
        "completed": completed,
        "notified": notified,
    }
    _write_state(state)
    return state


def _loop() -> None:
    _stop.wait(8)
    while not _stop.is_set():
        nxt = next_attempt_at()
        now = datetime.now(JST)
        if nxt > now + timedelta(seconds=1):
            _log(f"次回のX監視は {nxt.strftime('%Y-%m-%d %H:%M')}")
            if _wait_until(nxt):
                return
            continue
        try:
            sync_x_events()
        except Exception as exc:
            _log(f"Xイベント監視ループで例外: {exc}")
        nxt = next_attempt_at()
        if nxt <= datetime.now(JST):
            nxt = datetime.now(JST) + timedelta(minutes=5)
        if _wait_until(nxt):
            return


def _store_target(
    grouped: dict[str, dict],
    target: dict,
    event_date: str,
    handle: str,
    source_url: str,
    digit: str,
    prefectures: list[str],
) -> None:
    if not target.get("hall_id") and not target.get("group"):
        return
    event_type, title = _classify(target["label"], target["titles"], digit)
    if not title:
        return
    identity = target["hall_id"] or f"group:{target['group']}"
    key = f"x:{handle}:{event_date}:{identity}"
    current = grouped.get(key)
    if current is None:
        grouped[key] = {
            "date": event_date,
            "hall_id": target["hall_id"],
            "group": target["group"],
            "event_type": event_type,
            "event_title": title[:80],
            "source": f"x:{handle}",
            "source_key": key,
            "source_url": source_url,
            "prefectures": list(prefectures),
        }
        return
    merged_parts: list[str] = []
    for part in [*current["event_title"].split("・"), *title.split("・")]:
        if part and part not in merged_parts:
            merged_parts.append(part)
    current["event_title"] = "・".join(merged_parts)[:80]
    if event_type == "合同" or (event_type == "取材" and current["event_type"] == "特定日"):
        current["event_type"] = event_type


def _classify(label: str, titles: list[str], digit: str) -> tuple[str, str]:
    shown = "・".join(dict.fromkeys(titles))
    blob = f"{label}\n{shown}"
    if "合同" in blob:
        return "合同", shown or label
    if any(word in shown for word in _COVERAGE_WORDS):
        return "取材", shown
    if re.fullmatch(r"\d+日", shown) or "がつく日" in shown:
        return "特定日", shown
    if shown:
        return "取材", shown
    if "合同" in label or ("全店" in label and match_group_banner(label)):
        return "合同", label
    if digit:
        return "特定日", digit
    return "特定日", "特定日"


def _inline_target(line: str, prefectures: list[str] | None = None) -> dict | None:
    bracket = _BRACKET_RE.match(line)
    if bracket:
        target = _target_from_label(bracket.group(1).strip(), [bracket.group(2).strip()], prefectures=prefectures)
        if target:
            return target
    parts = _INLINE_SPLIT_RE.split(line, maxsplit=1)
    if len(parts) == 2:
        return _target_from_label(parts[0].strip(), [parts[1].strip()], prefectures=prefectures)
    return None


def _target_from_label(label: str, titles: list[str], prefectures: list[str] | None = None) -> dict | None:
    group_id = match_group_banner(label)
    hall_id = None if group_id else match_hall_id(label, prefectures=prefectures)
    if not group_id and not hall_id:
        return None
    return {"group": group_id or "", "hall_id": hall_id or "", "label": label, "titles": [title for title in titles if title]}


def _announcement_lines(text: str) -> list[str]:
    rows = []
    for raw in text.splitlines():
        line = raw.strip()
        if any(mark in line for mark in _STOP_MARKS):
            break
        if not line or line in _IGNORE_LINES:
            continue
        if line.startswith(("http://", "https://")) or "pic.twitter.com" in line or line.startswith(("—", "- ")):
            continue
        if "@" in line or "twitter.com" in line or "t.co/" in line:
            continue
        rows.append(line)
    return rows


def _is_title_line(line: str) -> bool:
    if line in _IGNORE_LINES or "。" in line or len(line) > 40:
        return False
    if line.startswith(("http://", "https://")):
        return False
    if any(mark in line for mark in ("すろいべ", "結果", "@", "スケジュール")):
        return False
    if _is_section_line(line):
        return False
    return True


def _is_untracked_place(line: str) -> bool:
    """登録ホールに無い店名。前のホールの取材名として吸い込まない。"""
    if any(word in line for word in (*_COVERAGE_WORDS, "撮")):
        return False
    if re.fullmatch(r"\d+日", line) or "がつく日" in line:
        return False
    if "合同" in line or "全店" in line:
        return True
    folded = line.casefold()
    hints = (
        "店", "プラザ", "ホール", "ランド", "玉屋", "ステーション", "アリーナ", "フェイス",
        "ゾーン", "スタジアム", "キャロル", "アプロ", "スペース", "テキサス", "ラッキー",
        "つかさ", "マルハン", "ユーコー", "ゴールデン", "ヴィーナス", "gion", "nikko", "boom",
    )
    return any(hint in folded for hint in hints)


def _scoped_prefectures(post: dict, prefectures: list[str] | None) -> list[str]:
    account_prefs = [str(item).strip() for item in (prefectures or []) if str(item).strip()]
    explicit = post.get("prefectures")
    named = [str(item).strip() for item in explicit if str(item).strip()] if isinstance(explicit, list) else []
    if not named:
        named = prefecture_ids_from_text(str(post.get("title") or ""))
    if not named:
        return account_prefs
    if not account_prefs:
        return named
    scoped = [item for item in named if item in set(account_prefs)]
    return scoped or account_prefs


def prefecture_ids_from_text(text: str) -> list[str]:
    found: list[str] = []
    for label, key in sorted(PREFECTURE_BY_LABEL.items(), key=lambda item: len(item[0]), reverse=True):
        if label in text and key not in found:
            found.append(key)
    if found:
        return found
    shorts = []
    for key, label in PREFECTURE_CATALOG:
        short = label[:-1] if label.endswith(("都", "府", "県")) else ""
        if short:
            shorts.append((short, key))
    shorts.sort(key=lambda item: len(item[0]), reverse=True)
    for short, key in shorts:
        if short in text and key not in found:
            found.append(key)
    return found


def _unique_chunk_match(needle: str, prefectures: set[str]) -> str | None:
    """県内で、表記ゆれを挟んでも読みの並びが一つに定まる店だけを採用する。"""
    parts = [part for part in _CHUNK_RE.findall(needle) if part.isdigit() or len(part) >= 2]
    if sum(len(part) for part in parts) < 5:
        return None
    if len(parts) == 1 and len(parts[0]) < 5:
        return None
    hits: list[str] = []
    for hall in load_halls(include_disabled=False):
        if hall.prefecture not in prefectures:
            continue
        for label in (hall.name, *hall.aliases):
            key = _core_label(label)
            if _chunks_in_order(key, parts):
                hits.append(hall.id)
                break
    unique = set(hits)
    if len(unique) == 1:
        return next(iter(unique))
    return None


def _chunks_in_order(haystack: str, parts: list[str]) -> bool:
    pos = 0
    for part in parts:
        found = haystack.find(part, pos)
        if found < 0:
            return False
        pos = found + len(part)
    return True


def _hall_name_parts(value: str, prefectures: list[str]) -> list[str]:
    text = value.strip()
    if not text:
        return []
    if match_hall_id(text, prefectures=prefectures):
        return [text]
    parts = [part.strip() for part in re.split(r"[・･、,/／]", text) if part.strip()]
    if len(parts) <= 1:
        return [text]
    return parts


def _coverage_section(line: str) -> str:
    """【新台入替】のように、続く店名へ共通で付く見出し。日付見出しは対象外。"""
    matched = re.fullmatch(r"【\s*(.+?)\s*】", line.strip())
    if not matched:
        return ""
    raw = matched.group(1)
    if _digit_from_section(raw):
        return ""
    text = unicodedata.normalize("NFKC", raw)
    text = re.sub(r"[\U0001F000-\U0010FFFF]", "", text)
    text = re.sub(r"[^\w一-龯ぁ-んァ-ヶー＋+]", "", text).strip()
    if not text or "がつく" in text or text.endswith("日"):
        return ""
    return text


def _is_coverage_line(line: str) -> bool:
    if line.startswith(("👉", "▶", "▷", "●", "・")):
        return True
    return any(word in line for word in (*_COVERAGE_WORDS, "撮", "祭"))


def _is_section_line(line: str) -> bool:
    compact = re.sub(r"\s+", "", line)
    if compact in _SECTION_LINES:
        return True
    return compact.endswith("のつく日") or compact.endswith("周年ホール")


def _digit_from_section(line: str) -> str:
    text = unicodedata.normalize("NFKC", line)
    if "日" not in text and "つく" not in text:
        return ""
    matched = re.search(r"(\d+)", text)
    if not matched:
        return ""
    if "つく" in text or re.search(r"\d+\s*日", text) or re.search(r"\d+\s*の日", text):
        return f"{matched.group(1)}がつく日"
    return ""


def _tag_local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _atom_entries(root: ET.Element) -> list[dict]:
    posts = []
    for item in list(root):
        if _tag_local(item.tag) != "entry":
            continue
        title = ""
        link = ""
        published = datetime.now(JST)
        body = ""
        for child in list(item):
            local = _tag_local(child.tag)
            if local == "title":
                title = html.unescape("".join(child.itertext())).strip()
            elif local == "link" and not link:
                link = (child.get("href") or child.text or "").split("?", 1)[0].strip()
            elif local in {"published", "updated", "issued", "modified"} and (child.text or "").strip():
                published = _parse_stamp(child.text or "")
            elif local == "content":
                body = _html_to_text("".join(child.itertext()))
            elif local == "summary" and not body:
                body = _html_to_text("".join(child.itertext()))
        posts.append({"title": title, "url": link, "published": published, "body": body})
        if len(posts) >= 15:
            break
    return posts


def _html_to_text(value: str) -> str:
    text = re.sub(r"(?i)<br\s*/?>", "\n", value)
    text = re.sub(r"(?i)</p>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text)


def _http_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read().decode("utf-8", "replace")


def _parse_stamp(value: str) -> datetime:
    text = (value or "").strip()
    if not text:
        return datetime.now(JST)
    try:
        stamp = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        stamp = None
    if stamp is None:
        try:
            stamp = datetime.fromisoformat(text)
        except ValueError:
            return datetime.now(JST)
    if stamp.tzinfo is None:
        return stamp.replace(tzinfo=JST)
    return stamp.astimezone(JST)


def _parse_pub_date(value: str) -> datetime:
    return _parse_stamp(value)


def _safe_date(year: int, month: int, day: int) -> str | None:
    try:
        return date_cls(year, month, day).isoformat()
    except ValueError:
        return None


def _norm_label(value: str) -> str:
    text = unicodedata.normalize("NFKC", value or "")
    text = text.replace("×", "x").replace("✕", "x")
    text = re.sub(r"[\s　・･]+", "", text)
    return text.casefold()


def _core_label(value: str) -> str:
    text = _norm_label(value)
    for prefix in ("slot", "super"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    return text


def _same_place(left: str, right: str) -> bool:
    if left == right:
        return True
    if left.startswith(right) and left[len(right):] == "店":
        return True
    if right.startswith(left) and right[len(left):] == "店":
        return True
    return False


def _as_jst(now: datetime | None) -> datetime:
    current = now or datetime.now(JST)
    if current.tzinfo is None:
        return current.replace(tzinfo=JST)
    return current.astimezone(JST)


def _slot_at(day: date_cls, settings: dict) -> datetime:
    return datetime.combine(day, time_cls(settings["hour"], settings["minute"]), tzinfo=JST)


def _parse_stamp(value: object) -> datetime | None:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=JST)
    return stamp.astimezone(JST)


def _record_attempt(now: datetime) -> None:
    status = read_daily_status()
    status["last_attempt_at"] = now.isoformat(timespec="seconds")
    status["last_result"] = "running"
    write_daily_status(status)


def _done_payload(now: datetime) -> dict:
    status = read_daily_status()
    raw = status.get("accounts_done") if isinstance(status.get("accounts_done"), dict) else {}
    today = now.date().isoformat()
    if raw.get("date") != today:
        return {"date": today, "handles": [], "counts": {}}
    handles = [str(item) for item in (raw.get("handles") or []) if str(item)]
    counts_raw = raw.get("counts") if isinstance(raw.get("counts"), dict) else {}
    counts: dict[str, int] = {}
    for key, value in counts_raw.items():
        try:
            counts[str(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return {"date": today, "handles": handles, "counts": counts}


def _done_handles(now: datetime) -> set[str]:
    return set(_done_payload(now)["handles"])


def _done_counts(now: datetime) -> dict[str, int]:
    return dict(_done_payload(now)["counts"])


def _mark_account_done(now: datetime, handle: str, hall_count: int) -> None:
    status = read_daily_status()
    done = _done_payload(now)
    if handle not in done["handles"]:
        done["handles"].append(handle)
    done["counts"][handle] = hall_count
    status["accounts_done"] = done
    write_daily_status(status)


def _mark_complete(now: datetime, target_date: str, hall_count: int, handles: list[str], notified: bool) -> None:
    settings = schedule_settings()
    status = read_daily_status()
    status.update(
        {
            "completed_on": now.date().isoformat(),
            "target_date": target_date,
            "completed_at": datetime.now(JST).isoformat(timespec="seconds"),
            "target_count": hall_count,
            "handles": handles,
            "last_result": "completed",
            "last_error": "",
            "next_run_at": _slot_at(now.date() + timedelta(days=1), settings).isoformat(timespec="seconds"),
        }
    )
    if notified:
        status["notified_for"] = target_date
    write_daily_status(status)


def _hall_name(hall_id: str) -> str:
    for hall in load_halls():
        if hall.id == hall_id:
            return hall.name
    return hall_id


def _target_hall_names(events: list[dict], target_date: str) -> set[str]:
    names: set[str] = set()
    for event in events:
        if event.get("date") != target_date:
            continue
        hall_id = str(event.get("hall_id") or "")
        group = str(event.get("group") or "")
        if hall_id:
            names.add(_hall_name(hall_id))
        elif group:
            names.update(group_hall_names(group, event.get("prefectures") or []))
    return {name for name in names if name}


def _notify_capture(target_date: str, hall_count: int, handles: list[str]) -> bool:
    try:
        from src.notify.discord import notify_event_capture

        return notify_event_capture(target_date=target_date, hall_count=hall_count, handles=handles)
    except Exception as exc:
        _log(f"Discord通知に失敗: {exc}")
        return False


def _wait_until(moment: datetime) -> bool:
    while not _stop.is_set():
        remaining = (moment - datetime.now(JST)).total_seconds()
        if remaining <= 0:
            return False
        if _stop.wait(min(remaining, 30)):
            return True
    return True


def _acquire_lock():
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = LOCK_PATH.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def _write_state(payload: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _log(message: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = f"{datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')} {message}"
    print(line, flush=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
