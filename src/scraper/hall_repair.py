"""取得元の再検証、閉店店のアーカイブ、全国店舗の追加。"""

from __future__ import annotations

import argparse
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable
from urllib.parse import urlparse

from src.dashboard.hall_admin import list_hall_rows
from src.scraper.log_safe import emit, failure_reason
from src.scraper.hall_catalog import (
    apply_catalog,
    archive_hall,
    archive_reason,
    daidata_publication_state,
    extract_teramoba_urls,
    fetch_catalog,
    load_master,
    load_targets,
    merge_master_rows,
    names_match,
    parse_hackertarget_hosts,
    parse_teramoba_hall_page,
    reassign_hall_source,
    save_halls_payload,
    save_master,
)
from src.scraper.halls import HALLS_CONFIG_PATH, PREFECTURE_CATALOG, load_halls_config

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
DESKTOP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
Get = Callable[[str], str]


def all_prefecture_slugs() -> list[str]:
    return [slug for slug, _label in PREFECTURE_CATALOG]


def _http_get(url: str, *, user_agent: str, timeout: float = 30) -> str:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, "replace")


def desktop_get(url: str) -> str:
    return _http_get(url, user_agent=DESKTOP_UA, timeout=45)


def mobile_get(url: str) -> str:
    return _http_get(url, user_agent=MOBILE_UA, timeout=25)


def search_teramoba(query: str, get: Get | None = None, *, pause_sec: float = 0.0) -> list[str]:
    """公開検索からテラモバの店舗URLを集める。"""
    getter = get or desktop_get
    found: list[str] = []
    seen: set[str] = set()
    for offset in (0, 10):
        url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
        if offset:
            url += f"&s={offset}"
        try:
            html = getter(url)
        except (OSError, urllib.error.URLError, TimeoutError):
            break
        if _search_blocked(html):
            break
        page = extract_teramoba_urls(html)
        if not page:
            break
        for item in page:
            if item not in seen:
                seen.add(item)
                found.append(item)
        if pause_sec:
            time.sleep(pause_sec)
    return found


def _search_blocked(html: str) -> bool:
    folded = html.casefold()
    return "anomaly-modal" in folded or "unfortunately, bots" in folded


def configured_teramoba_hosts(halls_payload: dict[str, Any]) -> list[str]:
    hosts: list[str] = []
    seen: set[str] = set()
    for raw in halls_payload.get("halls") or []:
        if not isinstance(raw, dict):
            continue
        cfg = raw.get("teramoba") if isinstance(raw.get("teramoba"), dict) else {}
        host = urlparse(str(cfg.get("base_url") or raw.get("source_url") or "")).netloc.lower()
        if host.endswith(".pt.teramoba2.com") and host not in seen:
            seen.add(host)
            hosts.append(host)
    return hosts


def discover_teramoba_urls(
    *,
    hosts: list[str],
    prefecture_labels: list[str],
    get: Get | None = None,
    pause_sec: float = 0.8,
) -> list[str]:
    """ホスト単位と、店舗の少ない県名でテラモバURLを集める。"""
    getter = get or desktop_get
    urls: list[str] = []
    seen: set[str] = set()

    def absorb(found: list[str]) -> None:
        for url in found:
            key = _department_key(url)
            if key and key not in seen:
                seen.add(key)
                urls.append(url)

    for host in hosts:
        emit(f"テラモバ検索: {host}", flush=True)
        absorb(search_teramoba(f"site:{host}", getter, pause_sec=0))
        time.sleep(pause_sec)
    for label in prefecture_labels:
        emit(f"テラモバ検索: {label}", flush=True)
        absorb(search_teramoba(f'site:pt.teramoba2.com "{label}"', getter, pause_sec=0))
        time.sleep(pause_sec)
    return urls


def _department_key(url: str) -> str:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return ""
    return f"{parsed.netloc.lower()}/{parts[0]}"


def fetch_teramoba_records(urls: list[str], get: Get | None = None, *, pause_sec: float = 0.25) -> list[dict[str, Any]]:
    getter = get or desktop_get
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, url in enumerate(urls, start=1):
        key = _department_key(url)
        if not key or key in seen:
            continue
        seen.add(key)
        try:
            html = getter(url)
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            emit(f"テラモバ取得失敗 {url}: {failure_reason(exc)}", flush=True)
            time.sleep(pause_sec)
            continue
        row = parse_teramoba_hall_page(html, url)
        if row is not None:
            rows.append(row)
            if index % 25 == 0:
                emit(f"テラモバ店舗 {index}/{len(urls)} 読込 {len(rows)} 件", flush=True)
        time.sleep(pause_sec)
    return rows


def grandgroup_urls(get: Get | None = None) -> list[str]:
    """グランドグループ公式からテラモバURLを拾う。"""
    getter = get or desktop_get
    try:
        index_html = getter("https://grandgroup.jp/")
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        emit(f"グランドグループ公式を開けませんでした: {failure_reason(exc)}", flush=True)
        return []
    pages = ["https://grandgroup.jp/"]
    for match in urllib.parse.unquote(index_html).split("https://grandgroup.jp/"):
        path = match.split('"', 1)[0].split("'", 1)[0]
        if path.startswith("grand"):
            pages.append("https://grandgroup.jp/" + path.split("?", 1)[0])
    found: list[str] = []
    seen: set[str] = set()
    for page in dict.fromkeys(pages):
        try:
            html = index_html if page == "https://grandgroup.jp/" else getter(page)
        except (OSError, urllib.error.URLError, TimeoutError):
            continue
        for url in extract_teramoba_urls(html):
            if url not in seen:
                seen.add(url)
                found.append(url)
        time.sleep(0.2)
    return found


def probe_daidata_states(store_ids: list[str], *, workers: int = 4) -> dict[str, str]:
    """台データオンラインの機種一覧が更新されているかを見る。"""

    def one(store_id: str) -> tuple[str, str]:
        url = f"https://daidata.goraggio.com/{store_id}/list?mode=psModelNameSearch&ps=S"
        try:
            html = mobile_get(url)
        except urllib.error.HTTPError as exc:
            return store_id, "missing" if exc.code == 404 else "error"
        except (OSError, urllib.error.URLError, TimeoutError):
            return store_id, "error"
        return store_id, daidata_publication_state(html)

    states: dict[str, str] = {}
    if not store_ids:
        return states
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(one, store_id) for store_id in store_ids]
        done = 0
        for future in as_completed(futures):
            store_id, state = future.result()
            states[store_id] = state
            done += 1
            if done % 40 == 0 or done == len(store_ids):
                emit(f"台データ再検証 {done}/{len(store_ids)}", flush=True)
    return states


def alternate_record(name: str, current_source: str, records: list[dict[str, Any]]) -> dict[str, Any] | None:
    """同名店舗を、今の取得元とは別の公開ソースから探す。"""
    for record in records:
        source = str(record.get("source") or "")
        if source == current_source or not names_match(name, str(record.get("name") or "")):
            continue
        if source == "daidata" and record.get("data_public") is not True:
            continue
        if source not in {"daidata", "teramoba"}:
            continue
        return record
    return None


def hall_store_id(hall: dict[str, Any]) -> str:
    goraggio = hall.get("goraggio") if isinstance(hall.get("goraggio"), dict) else {}
    return str(goraggio.get("hall_code") or "").strip()


def backfill_alternate_sources(
    halls_payload: dict[str, Any],
    records: list[dict[str, Any]],
    states: dict[str, str],
    *,
    get: Get | None = None,
    pause_sec: float = 0.8,
) -> list[dict[str, Any]]:
    """未更新の台データ店舗について、同名のテラモバ店舗を検索してマスターへ足す。"""
    getter = get or desktop_get
    found: list[dict[str, Any]] = []
    for hall in halls_payload.get("halls") or []:
        if not isinstance(hall, dict) or hall.get("archived") is True or not hall.get("enabled"):
            continue
        if str(hall.get("source") or "") != "goraggio":
            continue
        state = states.get(hall_store_id(hall))
        if state not in {"stale", "missing", "unpublished", "empty"}:
            continue
        name = str(hall.get("name") or "").strip()
        if not name or alternate_record(name, "daidata", records):
            continue
        emit(f"別ソース検索: {name}", flush=True)
        urls = search_teramoba(f'"{name}" site:pt.teramoba2.com', getter, pause_sec=0)
        rows = fetch_teramoba_records(urls[:4], getter, pause_sec=0.1)
        matched = [row for row in rows if names_match(name, str(row.get("name") or ""))]
        if matched:
            found.extend(matched)
            records.extend(matched)
        time.sleep(pause_sec)
    return found


def apply_source_fallbacks(
    halls_payload: dict[str, Any],
    records: list[dict[str, Any]],
    states: dict[str, str],
    targets: dict[str, Any],
) -> dict[str, int]:
    """未更新・欠番の台データ店舗をテラモバへ付け替え、見つからなければ巡回から外す。"""
    reassigned = 0
    archived = 0
    kept_live = 0
    for hall in halls_payload.get("halls") or []:
        if not isinstance(hall, dict) or hall.get("archived") is True:
            continue
        if str(hall.get("source") or "") != "goraggio":
            continue
        store_id = hall_store_id(hall)
        state = states.get(store_id)
        if state is None:
            continue
        if state == "live":
            kept_live += 1
            continue
        if state == "error":
            continue
        alternate = alternate_record(str(hall.get("name") or ""), "daidata", records)
        if alternate is not None and reassign_hall_source(hall, alternate, targets):
            reassigned += 1
            continue
        if state in {"stale", "missing", "unpublished"}:
            archive_hall(hall, "台データが未更新または欠番で、テラモバにも同名店が無い")
            archived += 1
    return {"reassigned": reassigned, "archived_unavailable": archived, "live": kept_live}


def archive_closed(halls_payload: dict[str, Any]) -> int:
    count = 0
    for hall in halls_payload.get("halls") or []:
        if not isinstance(hall, dict):
            continue
        reason = archive_reason(hall)
        if not reason:
            continue
        archive_hall(hall, reason)
        count += 1
    return count


def thin_prefecture_labels(records: list[dict[str, Any]], *, minimum: int = 10) -> list[str]:
    public = Counter(
        str(item.get("prefecture") or "")
        for item in records
        if item.get("source") == "daidata" and item.get("data_public") is True
    )
    return [label for slug, label in PREFECTURE_CATALOG if public[slug] < minimum]


def coverage_table(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """都道府県ごとの有効店舗と状態内訳。店舗が無い県も 0 で出す。"""
    grouped: dict[str, list[dict[str, Any]]] = {slug: [] for slug, _label in PREFECTURE_CATALOG}
    for row in rows:
        slug = str(row.get("prefecture") or "")
        grouped.setdefault(slug, []).append(row)
    table: list[dict[str, Any]] = []
    for slug, label in PREFECTURE_CATALOG:
        items = grouped.get(slug) or []
        enabled = [item for item in items if item.get("enabled") and item.get("status") != "archived"]
        table.append(
            {
                "prefecture": slug,
                "label": label,
                "enabled": len(enabled),
                "ok": sum(1 for item in enabled if item.get("status") == "ok"),
                "error": sum(1 for item in enabled if item.get("status") == "error"),
                "pending": sum(1 for item in enabled if item.get("status") == "pending"),
                "unpublished": sum(
                    1 for item in items if item.get("data_public") is False and item.get("status") != "archived"
                ),
                "archived": sum(1 for item in items if item.get("status") == "archived"),
                "meets_minimum": len(enabled) >= 10,
            }
        )
    return table


def print_coverage(table: list[dict[str, Any]]) -> None:
    emit("都道府県\t有効\t成功\tエラー\t未取得\t未公開\tアーカイブ\t10店以上", flush=True)
    for row in table:
        emit(
            f"{row['label']}\t{row['enabled']}\t{row['ok']}\t{row['error']}\t{row['pending']}\t"
            f"{row['unpublished']}\t{row['archived']}\t{'yes' if row['meets_minimum'] else 'no'}",
            flush=True,
        )
    enabled = sum(row["enabled"] for row in table)
    short = [row["label"] for row in table if not row["meets_minimum"]]
    emit(f"有効合計 {enabled} / 10店未満 {len(short)} 県: {', '.join(short) if short else 'なし'}", flush=True)


def run_repair(*, pause_sec: float = 0.35, search_pause_sec: float = 0.8) -> dict[str, Any]:
    before_rows = list_hall_rows()["halls"]
    before_enabled = sum(1 for row in before_rows if row.get("enabled") and row.get("status") != "archived")
    targets = load_targets()
    targets["prefectures"] = all_prefecture_slugs()
    halls_payload = load_halls_config()
    emit("台データオンラインの全国一覧を取り直します", flush=True)
    fetched = fetch_catalog(targets=targets, halls_payload=halls_payload, pause_sec=pause_sec, opener=desktop_get)
    master = load_master()
    merged = merge_master_rows(master, list(fetched.get("halls") or []))
    emit(f"台データ反映 追加={merged['added']} 更新={merged['updated']}", flush=True)

    hosts = parse_hackertarget_hosts(desktop_get("https://api.hackertarget.com/hostsearch/?q=teramoba2.com"))
    for host in configured_teramoba_hosts(halls_payload):
        if host not in hosts:
            hosts.append(host)
    labels = thin_prefecture_labels(list(master.get("halls") or []))
    emit(f"テラモバ探索 ホスト {len(hosts)} / 10店未満の県 {len(labels)}", flush=True)
    urls = grandgroup_urls(desktop_get)
    urls.extend(
        discover_teramoba_urls(hosts=hosts, prefecture_labels=labels, get=desktop_get, pause_sec=search_pause_sec)
    )
    deduped: list[str] = []
    seen_dept: set[str] = set()
    for url in urls:
        key = _department_key(url)
        if key and key not in seen_dept:
            seen_dept.add(key)
            deduped.append(url)
    emit(f"テラモバ店舗ページ {len(deduped)} 件を読みます", flush=True)
    teramoba_rows = fetch_teramoba_records(deduped, desktop_get, pause_sec=0.2)
    merged_tera = merge_master_rows(master, teramoba_rows)
    save_master(master)
    emit(f"テラモバ反映 追加={merged_tera['added']} 更新={merged_tera['updated']} 読込={len(teramoba_rows)}", flush=True)

    summary = apply_catalog(master, halls_payload, targets, register_enabled=False)
    closed = archive_closed(halls_payload)
    store_ids = []
    for hall in halls_payload.get("halls") or []:
        if not isinstance(hall, dict) or hall.get("archived") is True or not hall.get("enabled"):
            continue
        if str(hall.get("source") or "") != "goraggio":
            continue
        store_id = hall_store_id(hall)
        if store_id:
            store_ids.append(store_id)
    emit(f"有効な台データ店舗 {len(store_ids)} 件の取得元を再検証します", flush=True)
    states = probe_daidata_states(store_ids)
    backfill = backfill_alternate_sources(
        halls_payload,
        list(master.get("halls") or []),
        states,
        get=desktop_get,
        pause_sec=search_pause_sec,
    )
    if backfill:
        merge_master_rows(master, backfill)
        save_master(master)
    fallback = apply_source_fallbacks(halls_payload, list(master.get("halls") or []), states, targets)
    save_halls_payload(halls_payload, HALLS_CONFIG_PATH)

    after = list_hall_rows()
    table = coverage_table(after["halls"])
    print_coverage(table)
    enabled = sum(row["enabled"] for row in table)
    result = {
        "before_enabled": before_enabled,
        "after_enabled": enabled,
        "added": summary["added"],
        "daidata_errors": len(fetched.get("errors") or []),
        "teramoba_fetched": len(teramoba_rows),
        "closed_archived": closed,
        **fallback,
        "state_counts": dict(Counter(states.values())),
        "below_minimum": [row["label"] for row in table if not row["meets_minimum"]],
        "table": table,
    }
    emit(
        "結果 "
        f"有効 {before_enabled} → {enabled}（差 {enabled - before_enabled}） "
        f"新規登録={summary['added']} 取得元変更={fallback['reassigned']} "
        f"閉店アーカイブ={closed} 取得不能アーカイブ={fallback['archived_unavailable']} "
        f"再検証={result['state_counts']}",
        flush=True,
    )
    return result


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="店舗マスターの再検証と全国追加")
    parser.add_argument("--pause", type=float, default=0.35)
    parser.add_argument("--search-pause", type=float, default=0.8)
    args = parser.parse_args(argv)
    from src.run_gate import exit_if_halted

    exit_if_halted("店舗マスター再検証")
    run_repair(pause_sec=args.pause, search_pause_sec=args.search_pause)


if __name__ == "__main__":
    main()
