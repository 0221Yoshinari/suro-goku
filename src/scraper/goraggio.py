"""台データオンライン（daidata.goraggio.com）のスロット台一覧。"""

from __future__ import annotations

import asyncio
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from html import unescape
from http.cookiejar import CookieJar
from typing import Any
from urllib.parse import urljoin, urlparse

from src.scraper.base import FetchResult, PROJECT_ROOT
from src.scraper.deadline import run_within
from src.scraper.halls import Hall, format_duration, hall_data_dir, halls_for_source, max_workers, sleep_hall_gap
from src.scraper.html_tables import parse_document
from src.scraper.pace import failure_record, get_pacer, is_pressure, request_gap, rotate_user_agent
from src.scraper.targets import is_watched

# 台データオンラインはモバイル画面に台一覧テーブルが出る。UA はモバイルの中で回す。
TOKEN_RE = re.compile(r'name="_token" value="([^"]+)"')
_SKIP_DAI = {"", "平均", "合計", "計", "台番号"}
_GAME_HEADERS = ("累計スタート", "総回転数", "総スタート", "累計ゲーム", "回転数", "G数", "スタート")
_BB_HEADERS = ("BB回数", "BB", "BIG回数", "BIG")
_RB_HEADERS = ("RB回数", "RB", "REG回数", "REG")
_ART_HEADERS = ("ART回数", "ART", "AT回数", "AT", "AT/ART回数")


class GoraggioError(RuntimeError):
    """台データオンラインの取得失敗。"""


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    if isinstance(value, int):
        return value
    text = str(value).replace(",", "").replace("，", "").strip()
    if not text or text in {"--", "-", "―", "1/0.0", "1/0"}:
        return None
    match = re.search(r"-?\d+", text)
    return int(match.group(0)) if match else None


def _ratio_cell(value: str) -> str | None:
    text = value.strip()
    if not text or text in {"--", "-", "―"}:
        return None
    if text.startswith("1/"):
        denom = text[2:].strip()
        if denom in {"0", "0.0"}:
            return None
        return text
    try:
        number = float(text.replace(",", ""))
    except ValueError:
        return text
    if number <= 0:
        return None
    if number.is_integer():
        return f"1/{int(number)}"
    return f"1/{number}"


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


def parse_model_links(html: str, page_url: str) -> list[tuple[str, str]]:
    """機種一覧から (機種名, 台一覧URL) を返す。"""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for block in parse_document(html):
        if block.kind != "link" or "unit_list" not in block.href:
            continue
        name = block.text
        if "円" in name:
            name = re.split(r"\d+(?:\.\d+)?円", name, maxsplit=1)[0]
        name = name.strip(" |")
        href = urljoin(page_url, unescape(block.href))
        if not name or href in seen:
            continue
        seen.add(href)
        found.append((name, href))
    return found


def parse_unit_table(html: str, machine_name: str, hall_name: str) -> list[dict[str, Any]]:
    """台一覧テーブルを判定用の行にする。平均・合計行は除く。"""
    rows_out: list[dict[str, Any]] = []
    for block in parse_document(html):
        if block.kind != "table" or len(block.rows) < 2:
            continue
        headers = block.rows[0]
        dai_index = _header_index(headers, ("台番号", "台番"))
        game_index = _header_index(headers, _GAME_HEADERS)
        if dai_index is None or game_index is None:
            continue
        bb_index = _header_index(headers, _BB_HEADERS)
        rb_index = _header_index(headers, _RB_HEADERS)
        art_index = _header_index(headers, _ART_HEADERS)
        synth_index = _header_index(headers, ("合成確率",))
        bb_rate_index = _header_index(headers, ("BB確率", "BIG確率"))
        rb_rate_index = _header_index(headers, ("RB確率", "REG確率"))
        hold_index = _header_index(headers, ("最大持ち玉", "最大持玉"))
        for cells in block.rows[1:]:
            dai = _cell(cells, dai_index).strip()
            if dai in _SKIP_DAI or not re.search(r"\d", dai):
                continue
            dai_no = re.search(r"\d+", dai)
            if not dai_no:
                continue
            games = _as_int(_cell(cells, game_index))
            bb = _as_int(_cell(cells, bb_index))
            rb = _as_int(_cell(cells, rb_index))
            art = _as_int(_cell(cells, art_index))
            row: dict[str, Any] = {
                "machine_name": machine_name,
                "dai_no": dai_no.group(0),
                "total_games": games,
                "bb": bb,
                "rb": rb,
                "合成確率": _ratio_cell(_cell(cells, synth_index)) or _ratio_from_counts(games, (bb or 0) + (rb or 0) or None),
                "BB確率": _ratio_cell(_cell(cells, bb_rate_index)) or _ratio_from_counts(games, bb),
                "RB確率": _ratio_cell(_cell(cells, rb_rate_index)) or _ratio_from_counts(games, rb),
                "art": art,
                "hall_name": hall_name,
            }
            held = _as_int(_cell(cells, hold_index))
            if held is not None:
                row["最大持ち玉"] = held
            rows_out.append(row)
        if rows_out:
            return rows_out
    return rows_out


def _is_accept_page(html: str, final_url: str) -> bool:
    return "/accept" in final_url or "accept_btn" in html or "利用規約に同意する" in html


class GoraggioClient:
    def __init__(self) -> None:
        self.jar = CookieJar()
        from src.scraper.proxy_config import build_client_opener

        self.opener = build_client_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.user_agent = rotate_user_agent("mobile")

    def open(self, url: str, *, data: bytes | None = None, referer: str | None = None) -> tuple[str, str]:
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "ja,en;q=0.8",
        }
        if referer:
            headers["Referer"] = referer
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            headers["Origin"] = "https://daidata.goraggio.com"
        request = urllib.request.Request(url, data=data, headers=headers)
        from src.scraper.deadline import request_timeout_sec

        pacer = get_pacer("goraggio")
        pacer.sleep(request_gap(pacer))
        with pacer.limit(wait_sec=request_timeout_sec()):
            last_error: Exception | None = None
            for attempt in range(2):
                try:
                    with self.opener.open(request, timeout=request_timeout_sec()) as response:
                        body = response.read().decode("utf-8", errors="replace")
                        status = getattr(response, "status", 200)
                        final = response.geturl()
                except urllib.error.HTTPError as exc:
                    last_error = GoraggioError(f"HTTP {exc.code}")
                    if is_pressure(exc.code) and attempt < 1:
                        pacer.mark_pressure()
                        continue
                    raise last_error from exc
                except (urllib.error.URLError, TimeoutError) as exc:
                    reason = str(getattr(exc, "reason", exc))
                    timed = any(token in reason.casefold() for token in ("timeout", "timed out"))
                    last_error = GoraggioError("タイムアウト" if timed else "接続できません")
                    if is_pressure(None, reason) and attempt < 1:
                        pacer.mark_pressure()
                        continue
                    raise last_error from exc
                if is_pressure(status):
                    pacer.mark_pressure()
                    last_error = GoraggioError(f"HTTP {status}")
                    if attempt < 1:
                        continue
                    raise last_error
                folded = body[:400].casefold()
                if any(token in folded for token in ("captcha", "just a moment", "cf-challenge", "ロボットではありません")):
                    pacer.mark_pressure()
                    last_error = GoraggioError("CAPTCHA")
                    if attempt < 1:
                        continue
                    raise last_error
                pacer.mark_success()
                return final, body
        raise last_error or GoraggioError("接続できません")

    def accept_if_needed(self, hall_code: str, final_url: str, html: str, referer: str) -> tuple[str, str]:
        if not _is_accept_page(html, final_url):
            return final_url, html
        token = TOKEN_RE.search(html)
        if not token:
            raise GoraggioError(f"{hall_code}: 利用規約同意トークンがありません")
        payload = urllib.parse.urlencode({"_token": token.group(1)}).encode()
        action = f"https://daidata.goraggio.com/{hall_code}/accept"
        return self.open(action, data=payload, referer=referer or final_url)


def hall_code_of(hall: Hall) -> str:
    cfg = hall.source_config or {}
    code = str(cfg.get("hall_code") or "").strip()
    if code:
        return code
    parsed = urlparse(hall.source_url or "")
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        raise GoraggioError(f"{hall.name}: goraggio の店舗コードがありません")
    return parts[0]


def list_url_of(hall: Hall) -> str:
    code = hall_code_of(hall)
    cfg = hall.source_config or {}
    base = str(cfg.get("base_url") or "https://daidata.goraggio.com").rstrip("/")
    ps = str(cfg.get("ps") or "S")
    return f"{base}/{code}/list?mode=psModelNameSearch&ps={urllib.parse.quote(ps)}"


def fetch_goraggio_hall(hall: Hall, client: GoraggioClient | None = None) -> list[dict[str, Any]]:
    session = client or GoraggioClient()
    code = hall_code_of(hall)
    list_url = list_url_of(hall)
    from src.scraper.deadline import note_progress
    from src.scraper.pace import summarize_failure

    note_progress()
    print(f"台データオンラインへアクセス: {list_url}", flush=True)
    final, html = session.open(list_url, referer=f"https://daidata.goraggio.com/{code}")
    final, html = session.accept_if_needed(code, final, html, list_url)
    if _is_accept_page(html, final):
        final, html = session.open(list_url, referer=f"https://daidata.goraggio.com/{code}")
    models = parse_model_links(html, final or list_url)
    watched = [(name, url) for name, url in models if is_watched(name)]
    print(f"スロット機種数: {len(models)} / 監視対象: {len(watched)}", flush=True)
    machines: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for index, (name, url) in enumerate(watched, start=1):
        print(f"  [{index}/{len(watched)}] {name}", flush=True)
        try:
            unit_url, unit_html = session.open(url, referer=list_url)
            unit_url, unit_html = session.accept_if_needed(code, unit_url, unit_html, url)
            if _is_accept_page(unit_html, unit_url):
                unit_url, unit_html = session.open(url, referer=list_url)
            rows = parse_unit_table(unit_html, name, hall.name)
            print(f"    取得 {len(rows)} 台", flush=True)
            for row in rows:
                key = (row["machine_name"], row["dai_no"])
                if key in seen:
                    continue
                seen.add(key)
                machines.append(row)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            print(f"    一覧を開けませんでした: {summarize_failure(str(exc))}", flush=True)
            continue
    return machines


# 既に台データがある店は毎回取り、未取得の店は1巡回あたりこの件数だけ足す。
NEW_HALLS_PER_CYCLE = 12


def select_cycle_halls(halls: list[Hall], fetched_names: set[str], *, new_limit: int = NEW_HALLS_PER_CYCLE) -> list[Hall]:
    """取得済みの店を優先し、未取得店は new_limit 件までにする。"""
    known = [hall for hall in halls if hall.name in fetched_names]
    fresh = [hall for hall in halls if hall.name not in fetched_names]
    return known + fresh[: max(0, new_limit)]


PAGE_TIMEOUT_SEC = 15.0
DETAIL_CONCURRENCY = 2


class AsyncGoraggio:
    """店舗ごとに出口IPを固定した httpx クライアント。"""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.user_agent = rotate_user_agent("mobile")
        self.http_429 = 0
        self._client: Any = None

    async def __aenter__(self) -> AsyncGoraggio:
        import httpx

        from src.scraper.proxy_config import sticky_proxy_url

        self._httpx = httpx
        proxy = sticky_proxy_url(self.session_id)
        options: dict[str, Any] = {
            "timeout": httpx.Timeout(PAGE_TIMEOUT_SEC),
            "follow_redirects": True,
            "headers": {
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "ja,en;q=0.8",
            },
        }
        if proxy:
            options["proxy"] = proxy
        try:
            self._client = httpx.AsyncClient(http2=True, **options)
        except Exception:
            self._client = httpx.AsyncClient(**options)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def open(
        self,
        url: str,
        *,
        data: bytes | None = None,
        referer: str | None = None,
        attempts: int = 2,
    ) -> tuple[str, str]:
        headers = {"Referer": referer} if referer else {}
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            headers["Origin"] = "https://daidata.goraggio.com"
        last: Exception | None = None
        for _attempt in range(max(1, attempts)):
            try:
                if data is None:
                    response = await self._client.get(url, headers=headers)
                else:
                    response = await self._client.post(url, content=data, headers=headers)
            except Exception as exc:
                name = type(exc).__name__
                if "Timeout" in name:
                    last = GoraggioError("接続タイムアウト")
                elif "Connect" in name or "Proxy" in name or "Network" in name:
                    last = GoraggioError("接続できません")
                else:
                    last = GoraggioError("接続できません" if "://" in str(exc) else str(exc)[:160])
                continue
            if response.status_code == 429:
                self.http_429 += 1
                last = GoraggioError("HTTP 429")
                continue
            if response.status_code >= 400:
                last = GoraggioError(f"HTTP {response.status_code}")
                if response.status_code in {502, 503, 504}:
                    continue
                raise last
            return str(response.url), response.content.decode("utf-8", errors="replace")
        raise last or GoraggioError("接続できません")

    async def accept_if_needed(self, hall_code: str, final_url: str, html: str, referer: str) -> tuple[str, str]:
        if not _is_accept_page(html, final_url):
            return final_url, html
        token = TOKEN_RE.search(html)
        if not token:
            raise GoraggioError(f"{hall_code}: 利用規約同意トークンがありません")
        payload = urllib.parse.urlencode({"_token": token.group(1)}).encode()
        action = f"https://daidata.goraggio.com/{hall_code}/accept"
        return await self.open(action, data=payload, referer=referer or final_url)


def _should_fill(reason: str) -> bool:
    """タイムアウト・429・接続失敗だけを、別IPの1回補填へ回す。"""
    text = reason or ""
    folded = text.casefold()
    return "429" in folded or "タイムアウト" in text or "接続" in text


def _merge_rows(base: list[dict[str, Any]], extra: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = {(row["machine_name"], row["dai_no"]) for row in base}
    merged = list(base)
    for row in extra:
        key = (row["machine_name"], row["dai_no"])
        if key in seen:
            continue
        seen.add(key)
        merged.append(row)
    return merged


async def _read_model(
    client: AsyncGoraggio,
    hall: Hall,
    code: str,
    list_url: str,
    name: str,
    unit_url: str,
    *,
    attempts: int,
) -> tuple[list[dict[str, Any]], str]:
    try:
        page_url, unit_html = await client.open(unit_url, referer=list_url, attempts=attempts)
        page_url, unit_html = await client.accept_if_needed(code, page_url, unit_html, unit_url)
        if _is_accept_page(unit_html, page_url):
            page_url, unit_html = await client.open(unit_url, referer=list_url, attempts=attempts)
    except GoraggioError as exc:
        return [], str(exc)
    return parse_unit_table(unit_html, name, hall.name), ""


async def _fetch_watched(
    hall: Hall,
    session_id: str,
) -> tuple[list[dict[str, Any]], int, list[tuple[str, str, str]]]:
    code = hall_code_of(hall)
    list_url = list_url_of(hall)
    async with AsyncGoraggio(session_id) as client:
        final_url, html = await client.open(list_url, referer=f"https://daidata.goraggio.com/{code}")
        final_url, html = await client.accept_if_needed(code, final_url, html, list_url)
        if _is_accept_page(html, final_url):
            final_url, html = await client.open(list_url, referer=f"https://daidata.goraggio.com/{code}")
        links = parse_model_links(html, final_url or list_url)
        watched = [(name, url) for name, url in links if is_watched(name)]
        print(f"  {hall.name}: 一覧 {len(links)} 機種 / 監視対象 {len(watched)} 機種", flush=True)
        sem = asyncio.Semaphore(DETAIL_CONCURRENCY)

        async def one(name: str, unit_url: str) -> tuple[str, str, list[dict[str, Any]], str]:
            async with sem:
                rows, reason = await _read_model(client, hall, code, list_url, name, unit_url, attempts=2)
                if reason:
                    from src.scraper.pace import summarize_failure

                    print(f"  {hall.name} / {name}: {summarize_failure(reason)}", flush=True)
                return name, unit_url, rows, reason

        groups = await asyncio.gather(*(one(name, url) for name, url in watched))
        machines: list[dict[str, Any]] = []
        failed: list[tuple[str, str, str]] = []
        for name, unit_url, rows, reason in groups:
            if reason and _should_fill(reason):
                failed.append((name, unit_url, reason))
            machines = _merge_rows(machines, rows)
        return machines, client.http_429, failed


async def _fill_models(
    hall: Hall,
    failed: list[tuple[str, str, str]],
) -> tuple[list[dict[str, Any]], int, list[str]]:
    wait = random.uniform(3.0, 5.0)
    print(f"  {hall.name}: 欠損 {len(failed)} 機種を {wait:.1f}秒後に別IPで1回補填", flush=True)
    await asyncio.sleep(wait)
    code = hall_code_of(hall)
    list_url = list_url_of(hall)
    recovered: list[dict[str, Any]] = []
    still: list[str] = []
    async with AsyncGoraggio(f"fill{hall.id}") as client:
        for name, unit_url, _reason in failed:
            rows, reason = await _read_model(client, hall, code, list_url, name, unit_url, attempts=1)
            if reason:
                from src.scraper.pace import summarize_failure

                label = summarize_failure(reason)
                print(f"  {hall.name} / {name}: 補填失敗 ({label})", flush=True)
                still.append(f"{hall.name} / {name}: {label}")
                continue
            print(f"  {hall.name} / {name}: 補填 {len(rows)} 台", flush=True)
            recovered = _merge_rows(recovered, rows)
        return recovered, client.http_429, still


async def fetch_goraggio_hall_async(hall: Hall) -> tuple[list[dict[str, Any]], int, list[str]]:
    try:
        machines, http_429, failed = await _fetch_watched(hall, hall.id)
    except GoraggioError as exc:
        from src.scraper.pace import summarize_failure

        wait = random.uniform(3.0, 5.0)
        print(
            f"  {hall.name}: 店舗取得失敗（{summarize_failure(str(exc))}）。{wait:.1f}秒後に別IPで1回再取得",
            flush=True,
        )
        await asyncio.sleep(wait)
        machines, http_429, failed = await _fetch_watched(hall, f"b{hall.id}")
    if not failed:
        return machines, http_429, []
    recovered, fill_429, still = await _fill_models(hall, failed)
    return _merge_rows(machines, recovered), http_429 + fill_429, still


async def fetch_goraggio_data_async(halls: list[Hall] | None = None) -> FetchResult:
    targets = halls if halls is not None else halls_for_source("goraggio")
    if not targets:
        raise GoraggioError("source=goraggio の有効ホールが config/halls.json にありません")
    fetched_names = {
        hall.name
        for hall in targets
        if (hall_data_dir(hall.name, PROJECT_ROOT / "data" / "goraggio") / "incoming.json").is_file()
    }
    chosen = select_cycle_halls(targets, fetched_names, new_limit=len(targets))
    deferred = len(targets) - len(chosen)
    if deferred:
        print(
            f"台データオンライン: 未取得のうち {len(chosen) - len(fetched_names)} 店を今回取り、残り {deferred} 店は次回以降",
            flush=True,
        )
    targets = chosen
    started = time.monotonic()
    combined: list[dict[str, Any]] = []
    fetched: list[str] = []
    failed: list[str] = []
    reasons: list[str] = []
    details: list[dict[str, str]] = []
    http_429 = 0
    missing_models: list[str] = []
    workers = min(max_workers(), len(targets))
    print(f"台データオンライン: {len(targets)}店を同時{workers}・HTTP/2・店舗別IPで取得", flush=True)

    from src.scraper.smart_retry import partition_halls
    from src.scraper.source_fallback import finish_hall_attempt

    ready, waiting = partition_halls(targets, "goraggio")
    if waiting:
        print(f"台データオンライン: 再試行待ち {len(waiting)}店をスキップ", flush=True)
    gate = asyncio.Semaphore(workers)
    lock = asyncio.Lock()

    def _store(hall: Hall, payload: list[dict[str, Any]] | None, error: str) -> None:
        payload, error, _via = finish_hall_attempt(hall, payload, error, "goraggio")
        if error or payload is None:
            safe = error or "取得失敗"
            print(f"  {hall.name} の取得失敗: {safe}", flush=True)
            failed.append(hall.name)
            reasons.append(f"{hall.name}: {safe}")
            details.append(failure_record(hall.name, safe))
            return
        hall_result = FetchResult(source="goraggio", hall_name=hall.name, machines=payload)
        hall_path = hall_result.write_incoming(hall_data_dir(hall.name, PROJECT_ROOT / "data" / "goraggio"))
        print(f"  {hall.name}: {len(payload)} 件を {hall_path} に保存", flush=True)
        combined.extend(payload)
        fetched.append(hall.name)

    async def _run(hall: Hall) -> None:
        nonlocal http_429
        async with gate:
            try:
                payload, seen_429, missing = await fetch_goraggio_hall_async(hall)
            except Exception as exc:
                from src.scraper.pace import summarize_failure

                payload, seen_429, missing, error = None, 0, [], summarize_failure(str(exc))
            else:
                error = "" if payload else "0 件"
            async with lock:
                http_429 += seen_429
                missing_models.extend(missing)
                _store(hall, payload, error)

    await asyncio.gather(*(_run(hall) for hall in ready))
    for hall in waiting:
        _store(hall, None, "再試行待ち")
    elapsed = time.monotonic() - started
    print(
        f"台データオンライン: {format_duration(elapsed)} / 429応答 {http_429} 回 / 補填後の欠損 {len(missing_models)} 機種",
        flush=True,
    )
    for item in missing_models:
        print(f"  欠損: {item}", flush=True)
    if not combined:
        detail = " / ".join(reasons)
        raise GoraggioError("台データオンラインの取得に失敗したため incoming.json は更新していません。" + (f" {detail}" if detail else ""))
    return FetchResult(
        source="goraggio",
        hall_name=fetched[0] if len(fetched) == 1 else "複数店舗",
        machines=combined,
        extra={
            "halls": fetched,
            "failed_halls": failed,
            "failure_details": details,
            "elapsed_sec": round(elapsed, 1),
            "elapsed_label": format_duration(elapsed),
            "http_429": http_429,
            "missing_models": missing_models,
        },
    )


def fetch_goraggio_data(halls: list[Hall] | None = None) -> FetchResult:
    return asyncio.run(fetch_goraggio_data_async(halls))
