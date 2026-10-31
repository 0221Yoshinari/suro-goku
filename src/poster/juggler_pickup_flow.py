"""ジャグラーモード TOP4 を4画像1投稿で出す。auto_poster と preview スクリプト共通。"""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from src.poster.capture import CaptureError, capture_juggler_islands
from src.poster.discord_preview import DiscordPreviewError, send_x_preview
from src.poster.juggler_rank import compose_juggler_pickup_post, rank_juggler_islands
from src.poster.ledger import ledger_path, load_ledger, posted_entry, remember
from src.poster.x_api import XApiError, publish_tweet, should_publish

JUGGLER_HALL_KEY = "ジャグラーTOP4"
JUGGLER_LIMIT = 4


@dataclass
class JugglerPickupArgs:
    halls: Path
    date: str
    slot: str
    out_dir: Path
    meta: Path
    text: Path
    dashboard: str = ""
    dry_run: bool = False
    discord: bool = False
    heads_up: bool = False
    skip_capture: bool = False
    force: bool = False
    limit: int = JUGGLER_LIMIT


def juggler_pickup_dir(base: Path, date: str, slot: str) -> Path:
    return base / date / slot


def run_juggler_pickup(args: JugglerPickupArgs, *, now: datetime) -> tuple[int, dict[str, Any]]:
    """キャプチャ〜投稿まで。戻り値は (exit_code, meta_dict)。"""
    limit = max(1, min(int(args.limit), JUGGLER_LIMIT))
    picks = rank_juggler_islands(args.halls, limit=limit)
    if not picks:
        meta = {
            "ok": True,
            "posted": False,
            "mode": "juggler_top4",
            "slot": args.slot,
            "date": args.date,
            "skipped": "no_juggler",
        }
        return 0, meta

    text = compose_juggler_pickup_post(picks, now=now, slot=args.slot)
    args.text.parent.mkdir(parents=True, exist_ok=True)
    args.text.write_text(text + "\n", encoding="utf-8")

    targets = [
        (str(row.get("hall_name") or ""), str(row.get("family_label") or ""))
        for row in picks
    ]
    saved = _capture_or_load(args, targets)
    if saved is None:
        meta = {
            "ok": False,
            "posted": False,
            "mode": "juggler_top4",
            "slot": args.slot,
            "date": args.date,
            "text": text,
        }
        return 1, meta

    pictures = [path.read_bytes() for path in saved]
    ledger_file = ledger_path(args.meta)
    ledger = load_ledger(ledger_file)
    prior = posted_entry(ledger, args.date, args.slot, JUGGLER_HALL_KEY)

    preview_text = _heads_up_prefix(args, text)
    if args.discord:
        try:
            status = send_x_preview(preview_text, images=pictures)
        except DiscordPreviewError as exc:
            meta = {"ok": False, "posted": False, "mode": "juggler_top4", "error": str(exc), "text": text}
            return 1, meta
        meta = {
            "ok": True,
            "posted": False,
            "mode": "juggler_top4",
            "slot": args.slot,
            "date": args.date,
            "text": text,
            "images": [str(path) for path in saved],
            "discord_preview": True,
            "preview_http": status,
        }
        return 0, meta

    if prior and not args.force and not args.dry_run and not args.discord:
        meta = {
            "ok": True,
            "posted": True,
            "mode": "juggler_top4",
            "slot": args.slot,
            "date": args.date,
            "tweet_id": prior["tweet_id"],
            "skipped": "already_posted",
        }
        return 0, meta

    if not should_publish(ok=True, dry_run=args.dry_run or args.discord, image_ready=bool(pictures)):
        meta = {
            "ok": True,
            "posted": False,
            "mode": "juggler_top4",
            "slot": args.slot,
            "date": args.date,
            "text": text,
            "images": [str(path) for path in saved],
        }
        return 0, meta

    try:
        result = publish_tweet(text, images=pictures)
    except XApiError as exc:
        meta = {"ok": False, "posted": False, "mode": "juggler_top4", "error": str(exc), "text": text}
        return 1, meta

    remember(ledger, args.date, args.slot, JUGGLER_HALL_KEY, result["id"], "")
    ledger_file.parent.mkdir(parents=True, exist_ok=True)
    ledger_file.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    url = f"https://x.com/i/web/status/{result['id']}"
    try:
        send_x_preview(f"{text}\n{url}", images=pictures, label="【X投稿済み】")
    except DiscordPreviewError:
        pass

    meta = {
        "ok": True,
        "posted": True,
        "mode": "juggler_top4",
        "slot": args.slot,
        "date": args.date,
        "text": text,
        "tweet_id": result["id"],
        "url": url,
        "images": [str(path) for path in saved],
        "islands": [row.get("hall_name") for row in picks],
    }
    return 0, meta


def _heads_up_prefix(args: JugglerPickupArgs, text: str) -> str:
    if not args.heads_up:
        return text
    return f"5分後に投稿します。\n\n{text.strip()}"


def _capture_or_load(args: JugglerPickupArgs, targets: list[tuple[str, str]]) -> list[Path] | None:
    args.out_dir.mkdir(parents=True, exist_ok=True)
    if args.skip_capture:
        saved: list[Path] = []
        for index in range(1, len(targets) + 1):
            path = args.out_dir / f"{index:02d}.png"
            if not path.is_file():
                return None
            saved.append(path)
        return saved

    base_url = args.dashboard or None
    with tempfile.TemporaryDirectory(prefix="juggler-capture-") as folder:
        capture_dir = Path(folder)
        try:
            shots = capture_juggler_islands(targets, capture_dir, base_url=base_url)
        except CaptureError:
            return None
        if len(shots) != len(targets):
            return None
        saved = []
        for index, shot in enumerate(shots, start=1):
            dest = args.out_dir / f"{index:02d}.png"
            shutil.copyfile(shot, dest)
            saved.append(dest)
        return saved
