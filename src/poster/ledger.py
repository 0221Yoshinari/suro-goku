"""同じ日・時間帯・店の投稿を、一度だけにするための記録。"""

from __future__ import annotations

import json
from pathlib import Path


def ledger_path(meta: Path) -> Path:
    return meta.with_name("post_ledger.json")


def load_ledger(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def posted_entry(ledger: dict, date: str, slot: str, hall: str) -> dict | None:
    day = ledger.get(date)
    block = day.get(slot) if isinstance(day, dict) else None
    entry = block.get(hall) if isinstance(block, dict) else None
    if isinstance(entry, dict) and str(entry.get("tweet_id") or "").strip():
        return entry
    return None


def remember(ledger: dict, date: str, slot: str, hall: str, tweet_id: str, reply_id: str) -> None:
    day = ledger.setdefault(date, {})
    block = day.setdefault(slot, {})
    block[hall] = {"tweet_id": tweet_id, "reply_id": reply_id}
