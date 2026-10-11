"""台データオンラインとテラモバを、取得失敗時に相互に切り替える。"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from src.scraper.hall_catalog import _fold, names_match
from src.scraper.halls import Hall, hall_from_raw, load_halls, load_halls_config
from src.scraper.pace import summarize_failure
from src.scraper.smart_retry import get_ledger, is_deferrable

FALLBACK_OF = {
    "goraggio": "teramoba",
    "teramoba": "goraggio",
}


def clear_fallback_cache() -> None:
    load_fallback_index.cache_clear()


def _hall_from_master(record: dict[str, Any]) -> Hall | None:
    name = str(record.get("name") or "").strip()
    source = str(record.get("source") or "")
    if not name or source not in {"daidata", "teramoba"}:
        return None
    if source == "daidata" and record.get("data_public") is not True:
        return None
    store_id = str(record.get("store_id") or record.get("hall_id") or "").strip()
    if source == "daidata":
        if not store_id:
            return None
        return hall_from_raw(
            {
                "id": f"master-{store_id}",
                "name": name,
                "enabled": True,
                "source": "goraggio",
                "source_url": str(record.get("url") or f"https://daidata.goraggio.com/{store_id}"),
                "prefecture": str(record.get("prefecture") or ""),
                "goraggio": {
                    "base_url": "https://daidata.goraggio.com",
                    "hall_code": store_id,
                    "ps": "S",
                },
            }
        )
    host = str(record.get("host") or "").strip()
    url = str(record.get("url") or "").strip()
    if not host and not url:
        return None
    base = f"https://{host}" if host else ""
    return hall_from_raw(
        {
            "id": f"master-teramoba-{store_id or name}",
            "name": name,
            "enabled": True,
            "source": "teramoba",
            "source_url": url or None,
            "prefecture": str(record.get("prefecture") or ""),
            "teramoba": {
                "base_url": base,
                "department": store_id,
                "hall_id": record.get("hall_id"),
                "kind_code": str(record.get("kind_code") or "").strip() or "21",
                "list_url": url,
            },
        }
    )


@lru_cache(maxsize=1)
def load_fallback_index() -> dict[tuple[str, str], Hall]:
    """予備ソースの店を、取得元と正規化した店名で引けるようにする。"""
    index: dict[tuple[str, str], Hall] = {}
    try:
        from src.scraper.hall_catalog import load_master

        master = load_master()
    except (OSError, ValueError):
        master = {}
    for record in master.get("halls") or []:
        if not isinstance(record, dict):
            continue
        hall = _hall_from_master(record)
        if hall is None:
            continue
        index[(hall.source, _fold(hall.name))] = hall
    for hall in load_halls(include_disabled=True):
        if hall.source not in {"goraggio", "teramoba"}:
            continue
        index[(hall.source, _fold(hall.name))] = hall
        for alias in hall.aliases:
            index[(hall.source, _fold(alias))] = hall
    return index


def _explicit_fallback(hall: Hall, target_source: str) -> Hall | None:
    for raw in load_halls_config().get("halls") or []:
        if not isinstance(raw, dict) or str(raw.get("id") or "") != hall.id:
            continue
        fallback = raw.get("fallback")
        if not isinstance(fallback, dict):
            return None
        source = str(fallback.get("source") or target_source).strip() or target_source
        if source != target_source:
            return None
        merged = {
            "id": hall.id,
            "name": hall.name,
            "aliases": list(hall.aliases),
            "enabled": True,
            "source": source,
            "prefecture": hall.prefecture,
            "area": hall.area,
            "group": hall.group,
            "source_url": fallback.get("source_url") or fallback.get("url"),
        }
        block = {key: value for key, value in fallback.items() if key not in {"source", "source_url", "url"}}
        merged[source] = block
        return hall_from_raw(merged)
    return None


def fallback_hall(hall: Hall, *, index: dict[tuple[str, str], Hall] | None = None) -> Hall | None:
    """失敗した店の予備ソース。無ければ None。"""
    target = FALLBACK_OF.get(hall.source)
    if not target:
        return None
    explicit = _explicit_fallback(hall, target)
    if explicit is not None:
        return explicit
    table = load_fallback_index() if index is None else index
    folded = _fold(hall.name)
    found = table.get((target, folded))
    if found is not None and found.id != hall.id:
        return found
    for (source, _name), candidate in table.items():
        if source != target or candidate.id == hall.id:
            continue
        if names_match(hall.name, candidate.name):
            return candidate
    return None


def _retag(rows: list[dict[str, Any]], hall: Hall, via: str) -> list[dict[str, Any]]:
    retagged: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        copied = dict(row)
        copied["hall_name"] = hall.name
        copied["fetched_via"] = via
        retagged.append(copied)
    return retagged


def recover_with_fallback(hall: Hall) -> list[dict[str, Any]] | None:
    alternate = fallback_hall(hall)
    if alternate is None:
        return None
    try:
        if alternate.source == "teramoba":
            from src.scraper.teramoba import fetch_teramoba_hall

            rows = fetch_teramoba_hall(alternate)
        elif alternate.source == "goraggio":
            from src.scraper.goraggio import fetch_goraggio_hall

            rows = fetch_goraggio_hall(alternate)
        else:
            return None
    except Exception as exc:
        print(f"  {hall.name}: 予備ソースも失敗（{summarize_failure(str(exc))}）", flush=True)
        return None
    if not rows:
        return None
    print(f"  {hall.name}: 予備ソース（{alternate.source}）で取得", flush=True)
    return _retag(rows, hall, alternate.source)


def finish_hall_attempt(
    hall: Hall,
    payload: list[dict[str, Any]] | None,
    error: str,
    source: str,
) -> tuple[list[dict[str, Any]] | None, str, str]:
    """成功なら台帳を戻し、429 等は退避してから予備ソースを一度だけ試す。"""
    ledger = get_ledger()
    if payload:
        ledger.mark_ok(hall.id, source)
        return payload, "", source
    safe = summarize_failure(error or "取得失敗")
    if error and error != "再試行待ち" and is_deferrable(error):
        delay = ledger.defer(hall.id, source, error)
        print(f"  {hall.name}: 一時スキップ（{safe}、{int(delay)}秒後に再試行）", flush=True)
    elif error == "再試行待ち":
        print(f"  {hall.name}: 再試行待ちのためスキップ", flush=True)
    recovered = recover_with_fallback(hall)
    if recovered:
        return recovered, "", FALLBACK_OF.get(source, source)
    return None, safe, source
