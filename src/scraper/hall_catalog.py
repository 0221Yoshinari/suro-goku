"""テラモバと台データオンラインの店舗マスターを集め、監視対象へ登録する。"""

from __future__ import annotations

import argparse
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from src.scraper.log_safe import emit, failure_reason
from src.scraper.halls import (
    GROUP_INCLUDES_ART_IN_TOTAL_GAMES,
    HALLS_CONFIG_PATH,
    infer_area,
    PREFECTURE_BY_CODE,
    PREFECTURE_BY_LABEL,
    PREFECTURE_CATALOG,
    PREFECTURE_LABELS,
    PROJECT_ROOT,
    load_halls_config,
)

MASTER_PATH = PROJECT_ROOT / "data" / "hall_master.json"
TARGETS_PATH = PROJECT_ROOT / "config" / "hall_targets.json"
DAIDATA_ORIGIN = "https://daidata.goraggio.com"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_STORE_RE = re.compile(
    r'<a href="(https://daidata\.goraggio\.com/(\d+))">\s*'
    r"<h3>(.*?)</h3>\s*"
    r"<address>(.*?)</address>",
    re.S,
)
_DIGIT_TRANS = str.maketrans("０１２３４５６７８９", "0123456789")


class HallCatalogError(RuntimeError):
    """店舗マスターの取得または登録に失敗した。"""


def load_targets(path: Path | None = None) -> dict[str, Any]:
    target = path or TARGETS_PATH
    payload = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise HallCatalogError(f"{target} はオブジェクトである必要があります")
    return payload


def load_master(path: Path | None = None) -> dict[str, Any]:
    target = path or MASTER_PATH
    if not target.is_file():
        return {"fetched_at": None, "count": 0, "halls": []}
    payload = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return {"fetched_at": None, "count": 0, "halls": []}
    payload.setdefault("halls", [])
    return payload


def merge_master_rows(master: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, int]:
    """取得し直した店舗をマスターへ足す。同じ店舗は公開フラグと住所だけ更新する。"""
    halls = master.setdefault("halls", [])
    if not isinstance(halls, list):
        raise HallCatalogError("店舗マスターの halls が配列ではありません")
    index: dict[tuple[str, ...], dict[str, Any]] = {}
    for item in halls:
        if not isinstance(item, dict):
            continue
        if item.get("source") == "daidata":
            index[("daidata", str(item.get("store_id") or ""))] = item
        elif item.get("source") == "teramoba":
            index[("teramoba", str(item.get("host") or ""), str(item.get("store_id") or ""))] = item
    added = 0
    updated = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("source") == "daidata":
            key = ("daidata", str(row.get("store_id") or ""))
        elif row.get("source") == "teramoba":
            key = ("teramoba", str(row.get("host") or ""), str(row.get("store_id") or ""))
        else:
            continue
        current = index.get(key)
        if current is None:
            halls.append(row)
            index[key] = row
            added += 1
            continue
        changed = False
        for field in ("name", "prefecture", "prefecture_label", "address", "url", "data_public", "host"):
            if field not in row or row.get(field) in (None, ""):
                continue
            if current.get(field) != row.get(field):
                current[field] = row[field]
                changed = True
        if changed:
            updated += 1
    master["halls"] = halls
    master["count"] = len([item for item in halls if isinstance(item, dict)])
    master["sources"] = _source_counts([item for item in halls if isinstance(item, dict)])
    master["fetched_at"] = datetime.now(timezone.utc).isoformat()
    return {"added": added, "updated": updated}


def save_master(document: dict[str, Any], path: Path | None = None) -> Path:
    target = path or MASTER_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    halls = [item for item in document.get("halls") or [] if isinstance(item, dict)]
    document = {
        "fetched_at": document.get("fetched_at"),
        "count": len(halls),
        "sources": _source_counts(halls),
        "halls": halls,
    }
    _write_json(target, document)
    return target


def parse_daidata_store_list(html: str, *, prefecture_name: str = "") -> list[dict[str, Any]]:
    """台データオンラインの店舗一覧を店舗レコードにする。

    データ公開中は data_public=true。導入リクエストのみの店は false。
    """
    sections = _daidata_sections(html)
    fallback = PREFECTURE_BY_LABEL.get(prefecture_name.strip(), "")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for chunk, data_public in sections:
        for match in _STORE_RE.finditer(chunk):
            url, store_id, name_raw, address_raw = match.groups()
            name = _clean_text(name_raw)
            if not name or store_id in seen:
                continue
            seen.add(store_id)
            address_parts = [part for part in re.split(r"\s+", _clean_text(address_raw)) if part]
            label = ""
            address = ""
            if address_parts and address_parts[0] in PREFECTURE_BY_LABEL:
                label = address_parts[0]
                address = " ".join(address_parts[1:])
            elif address_parts:
                address = " ".join(address_parts)
            if not label:
                label = prefecture_name.strip()
            slug = PREFECTURE_BY_LABEL.get(label, fallback)
            rows.append(
                {
                    "name": name,
                    "prefecture": slug,
                    "prefecture_label": PREFECTURE_LABELS.get(slug, label),
                    "address": address,
                    "store_id": store_id,
                    "hall_id": None,
                    "url": url,
                    "source": "daidata",
                    "host": "daidata.goraggio.com",
                    "data_public": data_public,
                }
            )
    return rows


def _daidata_sections(html: str) -> list[tuple[str, bool]]:
    public_at = html.find('id="contractStores"')
    request_at = html.find('id="noContractStores"')
    sections: list[tuple[str, bool]] = []
    if public_at >= 0:
        end = request_at if request_at > public_at else len(html)
        sections.append((html[public_at:end], True))
    if request_at >= 0:
        sections.append((html[request_at:], False))
    if not sections:
        sections.append((html, True))
    return sections


def parse_teramoba_hall_page(html: str, page_url: str) -> dict[str, Any] | None:
    """テラモバ店舗ページの data-page から1店舗を読む。一覧が無いページは None。"""
    match = re.search(r'data-page="([^"]+)"', html)
    if not match:
        return None
    try:
        payload = json.loads(unescape(match.group(1)))
    except json.JSONDecodeError:
        return None
    props = payload.get("props") if isinstance(payload, dict) else None
    info = props.get("hall_informations") if isinstance(props, dict) else None
    if not isinstance(info, dict):
        return None
    name = str(info.get("hall_name") or "").strip()
    department = str(info.get("hall_code") or "").strip()
    if not name or not department:
        return None
    code_raw = info.get("prefecture_cd")
    try:
        slug = PREFECTURE_BY_CODE.get(int(code_raw), "")
    except (TypeError, ValueError):
        slug = ""
    host = str(info.get("domain") or "").strip() or urlparse(page_url).netloc
    hall_id_raw = info.get("hall_id")
    try:
        hall_id = int(hall_id_raw) if hall_id_raw not in (None, "") else None
    except (TypeError, ValueError):
        hall_id = None
    kind = ""
    if page_url:
        kind = (parse_qs(urlparse(page_url).query).get("kind_code") or [""])[0].strip()
    return {
        "name": name,
        "prefecture": slug,
        "prefecture_label": PREFECTURE_LABELS.get(slug, ""),
        "address": str(info.get("address") or "").strip(),
        "store_id": department,
        "hall_id": hall_id,
        "url": page_url,
        "source": "teramoba",
        "host": host,
        "kind_code": kind,
    }


def matches_target(record: dict[str, Any], targets: dict[str, Any]) -> bool:
    """対象県の店舗か。系列一致、テラモバ、または台データ公開中なら登録する。"""
    prefectures = {str(item).strip() for item in (targets.get("prefectures") or []) if str(item).strip()}
    if prefectures and str(record.get("prefecture") or "") not in prefectures:
        return False
    if brand_for_name(str(record.get("name") or ""), targets) is not None:
        return True
    if record.get("source") == "teramoba":
        return True
    return record.get("source") == "daidata" and record.get("data_public") is True


def fetchable_record(record: dict[str, Any]) -> bool:
    """台データを取りに行ける店か。未公開の台データオンラインは対象外。"""
    if record.get("source") == "daidata":
        return record.get("data_public") is True
    return record.get("source") == "teramoba"


def brand_for_name(name: str, targets: dict[str, Any]) -> str | None:
    folded = _fold(name)
    if not folded:
        return None
    for brand in targets.get("brands") or []:
        if not isinstance(brand, dict):
            continue
        brand_id = str(brand.get("id") or "").strip()
        if not brand_id:
            continue
        for keyword in brand.get("keywords") or []:
            token = _fold(str(keyword))
            if token and token in folded:
                return brand_id
    return None


def apply_catalog(
    master: dict[str, Any],
    halls_payload: dict[str, Any],
    targets: dict[str, Any],
    *,
    register_enabled: bool | None = None,
) -> dict[str, Any]:
    """条件に合う店舗だけを halls.json の halls へ追加する。既存店の設定は維持する。"""
    enabled_default = (
        bool(targets.get("register_enabled", False))
        if register_enabled is None
        else bool(register_enabled)
    )
    halls = halls_payload.setdefault("halls", [])
    if not isinstance(halls, list):
        raise HallCatalogError("halls.json の halls が配列ではありません")
    added: list[str] = []
    updated = 0
    unchanged = 0
    skipped = 0
    unpublished = 0
    for record in master.get("halls") or []:
        if not isinstance(record, dict):
            continue
        if not matches_target(record, targets):
            skipped += 1
            continue
        if record.get("source") == "daidata" and record.get("data_public") is False:
            unpublished += 1
        existing = _find_existing(halls, record)
        if isinstance(existing, dict) and existing.get("archived") is True:
            unchanged += 1
            continue
        if existing is None:
            entry = build_hall_entry(
                record,
                targets,
                enabled=True if fetchable_record(record) else enabled_default,
                taken_ids=_ids(halls),
            )
            halls.append(entry)
            added.append(entry["id"])
            continue
        if _same_source(existing, record):
            if _fill_blanks(existing, record) or _sync_fetchable(existing, record):
                updated += 1
            else:
                unchanged += 1
        else:
            unchanged += 1
    return {
        "added": len(added),
        "added_ids": added,
        "updated": updated,
        "unchanged": unchanged,
        "skipped": skipped,
        "unpublished": unpublished,
        "total": len(halls),
    }


def build_hall_entry(
    record: dict[str, Any],
    targets: dict[str, Any],
    *,
    enabled: bool,
    taken_ids: set[str],
) -> dict[str, Any]:
    name = str(record.get("name") or "").strip()
    group = brand_for_name(name, targets) or "other"
    prefecture = str(record.get("prefecture") or "").strip()
    area = infer_area(prefecture, str(record.get("address") or ""), name) or prefecture
    source = str(record.get("source") or "")
    if source == "daidata":
        store_id = str(record.get("store_id") or "").strip()
        hall_id = _unique_id(f"daidata-{store_id}", taken_ids)
        return {
            "id": hall_id,
            "name": name,
            "aliases": [name],
            "enabled": enabled,
            "group": group,
            "prefecture": prefecture,
            "area": area,
            "source": "goraggio",
            "source_url": str(record.get("url") or f"{DAIDATA_ORIGIN}/{store_id}"),
            "goraggio": {
                "base_url": DAIDATA_ORIGIN,
                "hall_code": store_id,
                "ps": "S",
            },
            "includes_art_in_total_games": GROUP_INCLUDES_ART_IN_TOTAL_GAMES.get(group, False),
            "data_public": record.get("data_public", True) is not False,
        }
    if source == "teramoba":
        department = str(record.get("store_id") or "").strip()
        host = str(record.get("host") or "").strip()
        base_url = f"https://{host}" if host else ""
        kind = str(record.get("kind_code") or "").strip() or "21"
        list_url = str(record.get("url") or "").strip()
        if not list_url and base_url and department:
            list_url = f"{base_url}/{department}/rack_info_kt?kind_code={kind}"
        numeric = record.get("hall_id")
        slug = f"teramoba-{numeric}" if numeric else f"teramoba-{department}"
        entry: dict[str, Any] = {
            "id": _unique_id(slug, taken_ids),
            "name": name,
            "aliases": [name],
            "enabled": enabled,
            "group": group,
            "prefecture": prefecture,
            "area": area,
            "source": "teramoba",
            "source_url": list_url,
            "teramoba": {
                "base_url": base_url,
                "department": department,
                "kind_code": kind,
                "kind_codes": [kind],
                "list_url": list_url,
            },
            "includes_art_in_total_games": GROUP_INCLUDES_ART_IN_TOTAL_GAMES.get(group, False),
            "data_public": True,
        }
        if numeric not in (None, ""):
            entry["teramoba"]["hall_id"] = int(numeric)
        return entry
    raise HallCatalogError(f"未対応のソースです: {source}")


def refresh_areas(halls_payload: dict[str, Any], master: dict[str, Any]) -> int:
    """県スラッグのままのエリアを、住所から詳細エリアへ付け替える。"""
    index: dict[tuple[str, ...], dict[str, Any]] = {}
    for item in master.get("halls") or []:
        if not isinstance(item, dict):
            continue
        if item.get("source") == "daidata":
            index[("daidata", str(item.get("store_id") or ""))] = item
        elif item.get("source") == "teramoba":
            index[("teramoba", str(item.get("host") or ""), str(item.get("store_id") or ""))] = item
    changed = 0
    for hall in halls_payload.get("halls") or []:
        if not isinstance(hall, dict):
            continue
        pref = str(hall.get("prefecture") or "")
        area = str(hall.get("area") or "")
        if area and area != pref:
            continue
        address = ""
        goraggio = hall.get("goraggio") if isinstance(hall.get("goraggio"), dict) else {}
        rec = index.get(("daidata", str(goraggio.get("hall_code") or "")))
        if rec is None:
            cfg = hall.get("teramoba") if isinstance(hall.get("teramoba"), dict) else {}
            host = urlparse(str(cfg.get("base_url") or "")).netloc
            rec = index.get(("teramoba", host, str(cfg.get("department") or "")))
        if isinstance(rec, dict):
            address = str(rec.get("address") or "")
        inferred = infer_area(pref, address, str(hall.get("name") or ""))
        if inferred and inferred != area:
            hall["area"] = inferred
            changed += 1
    return changed


def collect_teramoba_seeds(halls_payload: dict[str, Any], targets: dict[str, Any]) -> list[str]:
    seeds: list[str] = []
    for raw in targets.get("teramoba_seeds") or []:
        url = str(raw or "").strip()
        if url and url not in seeds:
            seeds.append(url)
    for raw in halls_payload.get("halls") or []:
        if not isinstance(raw, dict):
            continue
        if str(raw.get("source") or raw.get("source_type") or "") != "teramoba":
            continue
        url = str(raw.get("source_url") or "").strip()
        if not url:
            cfg = raw.get("teramoba")
            if isinstance(cfg, dict):
                url = str(cfg.get("list_url") or "").strip()
        if url and url not in seeds:
            seeds.append(url)
    return seeds


def fetch_catalog(
    *,
    targets: dict[str, Any] | None = None,
    halls_payload: dict[str, Any] | None = None,
    pause_sec: float = 0.4,
    opener=None,
) -> dict[str, Any]:
    """全都道府県の台データ一覧と、種URLのテラモバ店舗ページを取得する。"""
    targets = targets if targets is not None else load_targets()
    halls_payload = halls_payload if halls_payload is not None else load_halls_config()
    get = opener or _http_get
    halls: list[dict[str, Any]] = []
    errors: list[str] = []
    seen_daidata: set[str] = set()
    for _slug, label in PREFECTURE_CATALOG:
        url = f"{DAIDATA_ORIGIN}/store_list?pref={urllib.parse.quote(label)}"
        try:
            html = get(url)
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            errors.append(f"daidata {label}: {failure_reason(exc)}")
            emit(f"台データ {label} を取得できませんでした: {failure_reason(exc)}", flush=True)
            time.sleep(pause_sec)
            continue
        rows = parse_daidata_store_list(html, prefecture_name=label)
        fresh = 0
        for row in rows:
            store_id = str(row.get("store_id") or "")
            if not store_id or store_id in seen_daidata:
                continue
            seen_daidata.add(store_id)
            halls.append(row)
            fresh += 1
        emit(f"台データ {label}: {fresh} 件", flush=True)
        time.sleep(pause_sec)
    seen_teramoba: set[str] = set()
    for url in collect_teramoba_seeds(halls_payload, targets):
        try:
            html = get(url)
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            errors.append(f"teramoba: {failure_reason(exc)}")
            emit(f"テラモバを取得できませんでした: {url} {failure_reason(exc)}", flush=True)
            time.sleep(pause_sec)
            continue
        row = parse_teramoba_hall_page(html, url)
        if row is None:
            errors.append("teramoba: 店舗情報なし")
            emit(f"テラモバの店舗情報を読めませんでした: {url}", flush=True)
        else:
            key = f"{row.get('host')}/{row.get('store_id')}"
            if key not in seen_teramoba:
                seen_teramoba.add(key)
                halls.append(row)
                emit(f"テラモバ {row.get('name')}", flush=True)
        time.sleep(pause_sec)
    return {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "count": len(halls),
        "sources": _source_counts(halls),
        "errors": errors,
        "halls": halls,
    }


def save_halls_payload(payload: dict[str, Any], path: Path | None = None) -> Path:
    target = path or HALLS_CONFIG_PATH
    _write_json(target, payload)
    load_halls_config.cache_clear()
    return target


def _find_existing(halls: list[Any], record: dict[str, Any]) -> dict[str, Any] | None:
    source = str(record.get("source") or "")
    store_id = str(record.get("store_id") or "")
    host = str(record.get("host") or "")
    for raw in halls:
        if not isinstance(raw, dict):
            continue
        if source == "daidata":
            goraggio = raw.get("goraggio") if isinstance(raw.get("goraggio"), dict) else {}
            if str(goraggio.get("hall_code") or "") == store_id:
                return raw
            url = str(raw.get("source_url") or "").rstrip("/")
            if store_id and url.endswith("/" + store_id):
                return raw
        elif source == "teramoba":
            cfg = raw.get("teramoba") if isinstance(raw.get("teramoba"), dict) else {}
            base = str(cfg.get("base_url") or "")
            if str(cfg.get("department") or "") == store_id and host and host in base:
                return raw
    folded = _fold(str(record.get("name") or ""))
    prefecture = str(record.get("prefecture") or "")
    if not folded:
        return None
    for raw in halls:
        if not isinstance(raw, dict):
            continue
        if names_match(str(raw.get("name") or ""), str(record.get("name") or "")) and str(raw.get("prefecture") or "") == prefecture:
            return raw
    return None


def _same_source(existing: dict[str, Any], record: dict[str, Any]) -> bool:
    current = str(existing.get("source") or existing.get("source_type") or "")
    wanted = str(record.get("source") or "")
    if wanted == "daidata":
        return current == "goraggio"
    return current == wanted


def _fill_blanks(existing: dict[str, Any], record: dict[str, Any]) -> bool:
    changed = False
    prefecture = str(record.get("prefecture") or "").strip()
    if prefecture and not str(existing.get("prefecture") or "").strip():
        existing["prefecture"] = prefecture
        changed = True
    if prefecture and not str(existing.get("area") or "").strip():
        existing["area"] = prefecture
        changed = True
    return changed


def _sync_fetchable(existing: dict[str, Any], record: dict[str, Any]) -> bool:
    """公開フラグをマスターに揃える。取得できる店だけ、無効なら有効へ上げる。"""
    if existing.get("archived") is True:
        return False
    if not fetchable_record(record):
        if record.get("source") == "daidata" and record.get("data_public") is False and existing.get("data_public") is not False:
            if existing.get("enabled"):
                return False
            existing["data_public"] = False
            return True
        return False
    changed = False
    if existing.get("data_public") is not True:
        existing["data_public"] = True
        changed = True
    if not existing.get("enabled"):
        existing["enabled"] = True
        changed = True
    return changed


def _ids(halls: list[Any]) -> set[str]:
    return {str(item.get("id")) for item in halls if isinstance(item, dict) and item.get("id")}


def _unique_id(base: str, taken: set[str]) -> str:
    candidate = base or "hall"
    if candidate not in taken:
        taken.add(candidate)
        return candidate
    index = 2
    while f"{candidate}-{index}" in taken:
        index += 1
    chosen = f"{candidate}-{index}"
    taken.add(chosen)
    return chosen


def _source_counts(halls: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in halls:
        source = str(item.get("source") or "")
        if source:
            counts[source] = counts.get(source, 0) + 1
    return counts


def daidata_publication_state(html: str) -> str:
    """台データオンラインの機種一覧が、今も取得できる状態か。

    「更新されていません」は営業日表示に混ざることがある。
    機種リンクがある店、利用規約の同意が必要な店は取得対象のままにする。
    """
    if "unit_list" in html or "accept_btn" in html or "利用規約に同意する" in html:
        return "live"
    if "台データは更新されていません" in html:
        return "stale"
    if "導入リクエスト" in html or "データ公開のリクエスト" in html:
        return "unpublished"
    return "empty"


def names_match(left: str, right: str) -> bool:
    """店舗名が同じか。空白と全角数字の差、末尾1文字のゆれは同一とみなす。"""
    folded_left = _fold(left)
    folded_right = _fold(right)
    if not folded_left or not folded_right:
        return False
    if folded_left == folded_right:
        return True
    for base, other in ((folded_left, folded_right), (folded_right, folded_left)):
        if other.startswith(base) and len(other) - len(base) <= 1:
            return True
    return False


def rack_url_for(page_url: str) -> str:
    """テラモバの店舗URLを、台データ一覧（rack_info_kt）へ揃える。"""
    parsed = urlparse(page_url.strip())
    host = (parsed.netloc or "").lower()
    if not host.endswith("pt.teramoba2.com"):
        return ""
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return ""
    department = parts[0]
    if department.lower() in {"images", "common", "extends", "api", "ajax", "wp-admin", "wp-includes"}:
        return ""
    kind = (parse_qs(parsed.query).get("kind_code") or ["21"])[0].strip() or "21"
    scheme = parsed.scheme or "https"
    return f"{scheme}://{host}/{department}/rack_info_kt?kind_code={kind}"


def extract_teramoba_urls(html: str) -> list[str]:
    """検索結果や公式サイトHTMLからテラモバ店舗URLを抜く。"""
    found: list[str] = []
    seen: set[str] = set()
    candidates = [urllib.parse.unquote(item) for item in re.findall(r"uddg=([^&\"']+)", html)]
    candidates.extend(re.findall(r"https?://[a-z0-9.-]+\.pt\.teramoba2\.com[^\"'\s<>]*", html, re.I))
    for raw in candidates:
        url = rack_url_for(raw.replace("&amp;", "&"))
        if url and url not in seen:
            seen.add(url)
            found.append(url)
    return found


def parse_hackertarget_hosts(text: str) -> list[str]:
    hosts: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        host = line.split(",")[0].strip().lower()
        if host.endswith(".pt.teramoba2.com") and host not in seen:
            seen.add(host)
            hosts.append(host)
    return hosts


def reassign_hall_source(existing: dict[str, Any], record: dict[str, Any], targets: dict[str, Any]) -> bool:
    """取得できない台データ店舗を、同名のテラモバ店舗へ付け替える。IDは維持する。"""
    if str(record.get("source") or "") != "teramoba":
        return False
    if not names_match(str(existing.get("name") or ""), str(record.get("name") or "")):
        return False
    entry = build_hall_entry(record, targets, enabled=True, taken_ids=set())
    kept_id = str(existing.get("id") or entry["id"])
    aliases = list(existing.get("aliases") or [])
    for name in (existing.get("name"), record.get("name")):
        text = str(name or "").strip()
        if text and text not in aliases:
            aliases.append(text)
    existing.clear()
    existing.update(entry)
    existing["id"] = kept_id
    existing["aliases"] = aliases
    existing["enabled"] = True
    existing["archived"] = False
    existing.pop("archive_reason", None)
    existing["source_note"] = "台データオンラインが未更新または未公開のためテラモバへ切り替え"
    return True


def archive_reason(hall: dict[str, Any]) -> str | None:
    """巡回から外す閉店・仮店舗。データが取れている店は対象外。"""
    if hall.get("archived") is True:
        return None
    name = str(hall.get("name") or "")
    if "作成時初期値確認要" in name:
        return "テスト用の仮店舗"
    if "メガガイア" in name:
        return None
    group = str(hall.get("group") or "")
    unpublished = hall.get("data_public") is False
    if group == "gaia" and unpublished and "ミクちゃん" not in name:
        return "閉店済みのガイア系列（台データ未公開）"
    return None


def archive_hall(hall: dict[str, Any], reason: str) -> None:
    hall["archived"] = True
    hall["enabled"] = False
    hall["archive_reason"] = reason


def _fold(text: str) -> str:
    return re.sub(r"\s+", "", text.translate(_DIGIT_TRANS)).casefold()


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(value)).strip()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _http_get(url: str) -> str:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        raw = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, "replace")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="店舗マスターの取得と監視対象への一括登録")
    parser.add_argument("command", choices=("fetch", "apply", "sync"))
    parser.add_argument("--targets", type=Path, default=TARGETS_PATH)
    parser.add_argument("--master", type=Path, default=MASTER_PATH)
    parser.add_argument("--halls", type=Path, default=HALLS_CONFIG_PATH)
    parser.add_argument("--enable-new", action="store_true", help="新規店舗を有効のまま登録する")
    parser.add_argument("--pause", type=float, default=0.4)
    args = parser.parse_args(argv)
    targets = load_targets(args.targets)
    if args.command in {"fetch", "sync"}:
        from src.run_gate import exit_if_halted

        exit_if_halted("店舗マスター取得")
        halls_payload = json.loads(args.halls.read_text(encoding="utf-8"))
        document = fetch_catalog(targets=targets, halls_payload=halls_payload, pause_sec=args.pause)
        path = save_master(document, args.master)
        emit(
            f"店舗マスター {path} / {document['count']} 件 / {document.get('sources')} / 失敗 {len(document.get('errors') or [])}",
            flush=True,
        )
    if args.command in {"apply", "sync"}:
        master = load_master(args.master)
        halls_payload = json.loads(args.halls.read_text(encoding="utf-8"))
        summary = apply_catalog(
            master,
            halls_payload,
            targets,
            register_enabled=True if args.enable_new else None,
        )
        save_halls_payload(halls_payload, args.halls)
        emit(
            "登録 "
            f"追加={summary['added']} 更新={summary['updated']} "
            f"既存={summary['unchanged']} 対象外={summary['skipped']} "
            f"非公開={summary['unpublished']} 合計={summary['total']}",
            flush=True,
        )


if __name__ == "__main__":
    main()
