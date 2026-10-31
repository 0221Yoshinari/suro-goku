#!/usr/bin/env python3
"""スロ極の判定から、X投稿用の画面キャプチャと文面を作り、条件を満たすときだけ投稿する。

- output/summary.png  選ばれたホールのスロ極画面（1200×675）
- output/post.txt     投稿文面
- output/post.json    採点結果。ok が false のときは投稿しない

画像の撮影は Playwright。投稿は X API v2（画像も v2 の media/upload）。
どちらも requirements.txt の Playwright と httpx を使う。追加パッケージは不要。
キーは .env の X_API_KEY / X_API_SECRET / X_ACCESS_TOKEN / X_ACCESS_TOKEN_SECRET。

全6・全56・全456の機種か、末尾の平均推測設定が厚い店だけを載せる。
差枚は文面にも画像にも使わない。該当が無い日は画像を残さず、投稿もしない。
--dry-run のときは画像と文面だけ作り、Xへは送らない。
--discord のときは X へ出さず、文面と画像を Discord へ【X投稿プレビュー】として送る。
どちらも付けない本番は X へ投稿し、同じ内容を Discord へ【X投稿済み】として残す。
投稿に成功した直後、運営LINEグループへ投稿URLを送る。
同じ日・時間帯・店は output/post_ledger.json に記録し、二度投稿しない。
対象日とスロットは日本時間。11:50、14:00、17:00、19:00、21:00 だけ実行する。
朝のエリア宣言・ターゲットホール投稿は行わない。
各スロットは、大阪・東京・福岡の登録店から全台系・末尾・並びの一番見栄えがいい1件を出す。
強い数値が無いときは、末尾0〜9の相対差だけでも1件出す（断定は弱めの文面）。
それも無ければ投稿しない。
Webhook は DISCORD_WEBHOOK_URL、未設定なら config/discord.json を使う。
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.poster.capture import CaptureError, capture_hall
from src.poster.card import render_summary
from src.poster.chase import (
    AREAS,
    ChaseTarget,
    attach_data_image,
    chase_slot,
    now_jst,
    offhours_reason,
    scheduled_slot,
)
from src.poster.day_flow import area_targets, best_material
from src.poster.copy import compose_chase, compose_morning_areas, compose_post
from src.poster.discord_preview import DiscordPreviewError, send_x_preview, should_preview
from src.poster.events import resolve_events
from src.poster.labels import display_name
from src.poster.ledger import ledger_path, load_ledger, posted_entry, remember
from src.poster.x_api import XApiError, assert_write_access, load_x_env, publish_reply, publish_tweet, should_publish
from src.poster.select import (
    DEFAULT_HALLS_DIR,
    EXCLUSION_LABELS,
    JudgmentSourceError,
    HallPick,
    Selection,
    load_judgment_halls,
    select_top,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EVENTS = PROJECT_ROOT / "data" / "events" / "todays_events.json"
DEFAULT_IMAGE = PROJECT_ROOT / "output" / "summary.png"
DEFAULT_TEXT = PROJECT_ROOT / "output" / "post.txt"
DEFAULT_META = PROJECT_ROOT / "output" / "post.json"
JST = ZoneInfo("Asia/Tokyo")


def main(argv: list[str] | None = None) -> int:
    load_x_env()
    parser = argparse.ArgumentParser(description="スロ極の判定から、X投稿用の画面と文面を作る。ok のときだけ投稿する。")
    parser.add_argument("--halls", type=Path, default=DEFAULT_HALLS_DIR, help="店舗別判定ディレクトリか、1件の判定JSON")
    parser.add_argument("--events", type=Path, default=DEFAULT_EVENTS, help="todays_events.json")
    parser.add_argument("--date", default=_today(), help="対象日 YYYY-MM-DD。判定時刻は日本時間で見る")
    parser.add_argument("--image", type=Path, default=DEFAULT_IMAGE, help="まとめ画像の出力先")
    parser.add_argument("--text", type=Path, default=DEFAULT_TEXT, help="投稿文面の出力先")
    parser.add_argument("--meta", type=Path, default=DEFAULT_META, help="採点結果JSONの出力先")
    parser.add_argument("--dashboard", default="", help="スロ極のURL。未設定なら SURO_DASHBOARD_URL、無ければ手元で開く")
    parser.add_argument("--image-source", choices=("screen", "card"), default="screen", help="screen はスロ極の画面。card はテスト用")
    parser.add_argument("--pattern", choices=("auto", "hall", "digits"), default="auto", help="auto は全台系と末尾の強い方。hall は全台系、digits は末尾分析")
    parser.add_argument(
        "--slot",
        choices=("auto", "morning", "pickup", "midday", "afternoon", "evening", "night"),
        default="auto",
        help="追いかける時間帯。auto は今の時刻",
    )
    parser.add_argument("--dry-run", action="store_true", help="画像と文面だけ作り、Xへは送らない")
    parser.add_argument("--discord", action="store_true", help="Xへ出さず、Discordへ【X投稿プレビュー】を送る")
    parser.add_argument(
        "--allow-off-hours",
        action="store_true",
        help="テスト専用。定時の外でも文面を作る。本番の定時実行では使わない",
    )
    parser.add_argument(
        "--heads-up",
        action="store_true",
        help="Discordへ、5分後に投稿する予告と本文を送る。Xへは出さない前提で --discord と使う",
    )
    args = parser.parse_args(argv)
    now = now_jst()
    if not args.allow_off_hours:
        reason = offhours_reason(args.slot, args.date, now)
        if reason:
            print(reason, file=sys.stderr)
            return 0
    slot = _resolve_slot(args, now)
    if slot == "morning":
        return _skip_morning(args)
    if not args.dry_run and not args.discord:
        try:
            assert_write_access()
        except XApiError as exc:
            print(str(exc), file=sys.stderr)
            return 1

    try:
        halls, _unreadable = load_judgment_halls(args.halls, on_progress=_progress)
    except JudgmentSourceError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    events = resolve_events(_event_rows(args.events), args.date)
    print(f"対象日 {args.date} / {slot} / 日本時間 {now.strftime('%Y-%m-%d %H:%M')}", file=sys.stderr)
    targets = area_targets(events, args.date)
    if targets:
        meta_path = ledger_path(args.meta)
        if posted_entry(load_ledger(meta_path), args.date, slot, "ジャグラーTOP4"):
            print(f"{slot} はジャグラー投稿済みのため、数値投稿をスキップします。", file=sys.stderr)
            _write_json(args.meta, {"ok": True, "posted": True, "skipped": "juggler_midday", "date": args.date})
            return 0
        hit = best_material(halls, targets, args.date)
        if hit is not None:
            chosen = ChaseTarget(hit.hall_name, "", hit.score, hit.event_titles, 0.0, 0.0, hit)
            return _run_chase([chosen], slot, args)
        _clear(args.image)
        _clear(args.text)
        print("全台系・末尾・並びが出ていないので投稿しません。", file=sys.stderr)
        _heads_up_skip(args, slot)
        _write_json(args.meta, {"ok": False, "posted": False, "text": "", "pick": None, "date": args.date})
        return 0

    _clear(args.image)
    _clear(args.text)
    _write_json(args.meta, {"ok": False, "posted": False, "text": "", "pick": None, "date": args.date})
    print(f"{display_name(args.date)} は投稿できるホールがありません。", file=sys.stderr)
    _heads_up_skip(args, slot)
    return 2


def _resolve_slot(args, now: datetime) -> str:
    if args.allow_off_hours:
        return chase_slot(now.hour) if args.slot == "auto" else args.slot
    if args.slot == "auto":
        return scheduled_slot(now) or ""
    return args.slot


def _skip_morning(args) -> int:
    """朝のエリア宣言は出さない。launchd の morning が残っていても安全に終了する。"""
    print("朝のターゲット投稿は停止しています。スキップします。", file=sys.stderr)
    _clear(args.image)
    _clear(args.text)
    _write_json(
        args.meta,
        {"ok": True, "posted": False, "skipped": "morning_disabled", "date": args.date},
    )
    if getattr(args, "heads_up", False) and args.discord:
        try:
            status = send_x_preview(
                "5分後の10:00は、朝のエリア宣言は行いません。",
                label="【投稿予告】",
            )
        except DiscordPreviewError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"Discordへ見送り予告を送りました (HTTP {status})")
    return 0


def _post_morning(targets, args) -> int:
    """朝は3エリア全店を追う宣言にし、まとめ画像を付ける。公式LINEの返信は付けない。"""
    text = compose_morning_areas()
    ledger = load_ledger(ledger_path(args.meta))
    _print_post(text, "")
    try:
        from src.poster.morning_card import render_morning_area_card

        render_morning_area_card(args.image, date=args.date)
    except CaptureError as exc:
        _write_morning_meta(targets, args, text, _Delivery(error=str(exc)), [])
        print(str(exc), file=sys.stderr)
        return 1
    outcome = _deliver(
        text,
        "",
        args.image,
        date=args.date,
        slot="morning",
        hall="まとめ",
        args=args,
        ledger=ledger,
        ok=True,
        image_ready=args.image.is_file(),
        text_only=False,
    )
    if outcome.tweet_id:
        remember(ledger, args.date, "morning", "エリア宣言", outcome.tweet_id, "")
        _write_json(ledger_path(args.meta), ledger)
    tweets = []
    if outcome.tweet_id:
        tweets.append({"hall_name": "エリア宣言", "tweet_id": outcome.tweet_id, "reply_id": ""})
    _write_text(args.text, text + "\n")
    _write_morning_meta(targets, args, text, outcome, tweets)
    if args.dry_run or args.discord:
        print("Xへは投稿していません")
    if outcome.error:
        print(outcome.error, file=sys.stderr)
        return 1
    if args.discord and not outcome.previewed:
        return 1
    return 0


def _write_morning_meta(targets, args, text: str, outcome, tweets: list[dict[str, str]]) -> None:
    _write_json(
        args.meta,
        {
            "ok": True,
            "mode": "chase",
            "slot": "morning",
            "date": args.date,
            "areas": list(AREAS),
            "posted": outcome.posted,
            "tweets": tweets,
            "discord_preview": outcome.previewed,
            "text": text,
            "image": str(args.image) if args.image.is_file() else "",
            "targets": [
                {
                    "hall_name": target.hall_name,
                    "prefecture": target.prefecture,
                    "points": target.points,
                    "event_titles": list(target.event_titles),
                    "avg_games": round(target.avg_games, 1),
                    "stability": round(target.stability, 3),
                    "same_day": target.same_day.hall_name if target.same_day else "",
                }
                for target in targets
            ],
            "post_error": outcome.error,
        },
    )


def _run_chase(targets, slot: str, args) -> int:
    """朝は全店を1投稿にまとめる。昼と夜は当日データがある店だけ、個別に出す。"""
    if slot == "morning":
        return _post_morning(targets, args)
    texts: list[str] = []
    errors: list[str] = []
    tweets: list[dict[str, str]] = []
    previewed = 0
    ledger = load_ledger(ledger_path(args.meta))
    saved_image = False
    needed_image = False
    for target in targets:
        text = compose_chase(target, slot)
        if not text.strip():
            print(f"数値がないため見送ります: {target.hall_name}")
            continue
        reply = ""
        texts.append(f"{target.prefecture} {target.points}pt\n{_with_reply(text, reply).rstrip()}")
        attach = attach_data_image(slot, same_day=target.same_day is not None)
        image_path: Path | None = None
        if attach:
            needed_image = True
            image_path, error = _chase_image(target, args, primary=not saved_image)
            if image_path is not None and image_path.is_file() and image_path == args.image:
                saved_image = True
        else:
            error = ""
            print(f"数値画像は付けません: {target.hall_name}")
        _print_post(text, reply)
        print("---")
        if error or (attach and (image_path is None or not image_path.is_file())):
            errors.append(f"{target.hall_name}: {error or '画像なし'}")
            print(error or "画像なし", file=sys.stderr)
            continue
        outcome = _deliver(
            text,
            reply,
            image_path,
            date=args.date,
            slot=slot,
            hall=target.hall_name,
            args=args,
            ledger=ledger,
            ok=True,
            image_ready=bool(image_path and image_path.is_file()),
            text_only=not attach,
        )
        if outcome.previewed:
            previewed += 1
        if outcome.tweet_id:
            tweets.append({"hall_name": target.hall_name, "tweet_id": outcome.tweet_id, "reply_id": outcome.reply_id})
        if outcome.error:
            errors.append(f"{target.hall_name}: {outcome.error}")
            print(outcome.error, file=sys.stderr)
    combined = "\n\n---\n\n".join(texts)
    _write_text(args.text, combined + "\n")
    _write_json(
        args.meta,
        {
            "ok": True,
            "mode": "chase",
            "slot": slot,
            "date": args.date,
            "areas": list(AREAS),
            "posted": bool(tweets),
            "tweets": tweets,
            "discord_preview": previewed > 0,
            "text": combined,
            "image": str(args.image) if args.image.is_file() else "",
            "targets": [
                {
                    "hall_name": target.hall_name,
                    "prefecture": target.prefecture,
                    "points": target.points,
                    "event_titles": list(target.event_titles),
                    "avg_games": round(target.avg_games, 1),
                    "stability": round(target.stability, 3),
                    "same_day": target.same_day.hall_name if target.same_day else "",
                }
                for target in targets
            ],
            "post_error": " / ".join(errors),
        },
    )
    if args.dry_run or args.discord:
        print("Xへは投稿していません")
    if args.image_source == "screen" and needed_image and not args.image.is_file():
        return 1
    if args.discord and texts and previewed == 0:
        return 1
    if not texts:
        _heads_up_skip(args, slot)
    if errors:
        return 1
    return 0


def _chase_image(target, args, *, primary: bool) -> tuple[Path | None, str]:
    path = args.image
    if not primary:
        handle = tempfile.NamedTemporaryFile(prefix="suro-chase-", suffix=".png", delete=False)
        handle.close()
        path = Path(handle.name)
    if args.image_source == "card" and target.same_day is not None:
        image = render_summary(target.same_day, date=args.date)
        buffer = BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        _write_bytes(path, buffer.getvalue())
        return path, ""
    if args.image_source == "card":
        return None, "朝の宣言には画面キャプチャを使います"
    pick = target.same_day
    digits = bool(pick and pick.pattern == "digits" and pick.digits)
    try:
        capture_hall(
            target.hall_name,
            path,
            base_url=args.dashboard or None,
            view="digits" if digits else "hall",
            focus=_focus_section(pick) if pick is not None else "section-all-high",
            digit=pick.digits[0].digit if digits else None,
            machine_name=pick.machines[0].dom_name if pick is not None and not digits and pick.machines else None,
        )
    except CaptureError as exc:
        return None, str(exc)
    return path, ""


def _write_image(pick, args) -> str:
    if args.image_source == "card":
        image = render_summary(pick, date=args.date)
        buffer = BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        _write_bytes(args.image, buffer.getvalue())
        return ""
    try:
        capture_hall(
            pick.hall_name,
            args.image,
            base_url=args.dashboard or None,
            view="digits" if pick.pattern == "digits" else "hall",
            focus=_focus_section(pick),
            digit=pick.digits[0].digit if pick.pattern == "digits" and pick.digits else None,
            machine_name=pick.machines[0].dom_name if pick.pattern != "digits" and pick.machines else None,
        )
    except CaptureError as exc:
        return str(exc)
    return ""


def _focus_section(pick: HallPick) -> str:
    if pick.pattern == "digits":
        return "section-hall-digits"
    if pick.machines and pick.machines[0].is_all_high:
        return "section-all-high"
    if pick.machines and pick.machines[0].is_high_56:
        return "section-high-56"
    return "section-high-base"


def _focus_section(pick: HallPick) -> str:
    if pick.pattern == "digits":
        return "section-hall-digits"
    if pick.machines and pick.machines[0].is_all_high:
        return "section-all-high"
    if pick.machines and pick.machines[0].is_high_56:
        return "section-high-56"
    return "section-high-base"


@dataclass
class _Delivery:
    posted: bool = False
    previewed: bool = False
    tweet_id: str = ""
    reply_id: str = ""
    error: str = ""


def _deliver(
    text: str,
    reply: str,
    image_path: Path | None,
    *,
    date: str,
    slot: str,
    hall: str,
    args,
    ledger: dict,
    ok: bool,
    image_ready: bool,
    text_only: bool = False,
) -> _Delivery:
    """プレビュー、または X 投稿と Discord への投稿済み通知。"""
    prior = posted_entry(ledger, date, slot, hall)
    if prior and not args.dry_run and not args.discord:
        print(f"投稿済みのため飛ばします: {hall} https://x.com/i/web/status/{prior['tweet_id']}")
        return _Delivery(posted=True, tweet_id=str(prior["tweet_id"]), reply_id=str(prior.get("reply_id") or ""))
    picture = image_path.read_bytes() if image_ready and image_path is not None and not text_only else None
    if should_preview(discord=args.discord, ok=ok, image_ready=picture is not None, text_only=text_only):
        try:
            status = send_x_preview(_heads_up_text(args, text), picture)
            if reply.strip():
                send_x_preview(reply, label="【Xスレッド返信】")
        except DiscordPreviewError as exc:
            return _Delivery(error=str(exc))
        print(f"Discordへプレビューを送りました (HTTP {status})")
        return _Delivery(previewed=True)
    if not should_publish(ok=ok, dry_run=args.dry_run or args.discord, image_ready=picture is not None, text_only=text_only):
        return _Delivery()
    try:
        result = publish_tweet(text, picture)
        reply_id = ""
        if reply.strip():
            reply_id = publish_reply(reply, result["id"])["id"]
    except XApiError as exc:
        return _Delivery(error=str(exc))
    remember(ledger, date, slot, hall, result["id"], reply_id)
    _write_json(ledger_path(args.meta), ledger)
    url = f"https://x.com/i/web/status/{result['id']}"
    print(f"投稿しました: {url}")
    notice = _notify_posted(text, reply, picture, url)
    if notice:
        return _Delivery(posted=True, tweet_id=result["id"], reply_id=reply_id, error=notice)
    return _Delivery(posted=True, tweet_id=result["id"], reply_id=reply_id)


def _with_reply(text: str, reply: str) -> str:
    if not reply.strip():
        return text.rstrip() + "\n"
    return text.rstrip() + "\n\n---\n\n" + reply.strip() + "\n"


def _print_post(text: str, reply: str) -> None:
    print(text)
    if reply.strip():
        print("---")
        print(reply)
    else:
        print("公式LINEの返信は付けません")


def _notify_posted(text: str, reply: str, image: bytes | None, url: str) -> str:
    problems: list[str] = []
    try:
        send_x_preview(f"{text}\n{url}", image, label="【X投稿済み】")
        if reply.strip():
            send_x_preview(reply, label="【Xスレッド返信】")
    except DiscordPreviewError as exc:
        problems.append(str(exc))
    line_error = _notify_ops_line(url)
    if line_error:
        problems.append(line_error)
    return " ".join(problems)


def _notify_ops_line(url: str) -> str:
    """X投稿済みのURLを運営グループへ伝える。失敗しても投稿自体は取り消さない。"""
    try:
        from src.marketing.daily_report import ReportDeliveryError
        from src.marketing.daily_report import notify_ops_of_x_post

        notify_ops_of_x_post(url)
    except ReportDeliveryError as exc:
        print(f"運営LINEへの通知に失敗しました: {exc}", flush=True)
        return "運営LINEへの通知に失敗しました。"
    except Exception:
        print("運営LINEへの通知に失敗しました", flush=True)
        return "運営LINEへの通知に失敗しました。"
    return ""


def _today() -> str:
    return datetime.now(JST).date().isoformat()


def _progress(done: int, total: int) -> None:
    if total < 200:
        return
    if done == total or done % 200 == 0:
        print(f"判定ファイル {done}/{total}", file=sys.stderr)


def _event_rows(path: Path) -> list[dict]:
    if not path.is_file():
        print(f"イベントファイルが無いため、イベント加点なしで続けます: {path}", file=sys.stderr)
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        print(f"イベントを読めないため、イベント加点なしで続けます: {exc.__class__.__name__}", file=sys.stderr)
        return []
    rows = payload.get("events") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _meta(selection: Selection, *, text: str, image: Path | None) -> dict:
    pick = selection.pick
    return {
        "ok": pick is not None,
        "date": selection.date,
        "considered": selection.considered,
        "excluded_counts": selection.excluded_counts,
        "excluded_labels": EXCLUSION_LABELS,
        "image": str(image) if image is not None else "",
        "text": text,
        "pick": _pick_payload(pick) if pick is not None else None,
        "runners_up": [_pick_payload(item) for item in selection.runners_up],
    }


def _pick_payload(pick: HallPick) -> dict:
    return {
        "hall_name": pick.hall_name,
        "pattern": pick.pattern,
        "rank": pick.rank,
        "score": pick.score,
        "unit_count": pick.unit_count,
        "total_games": pick.total_games,
        "clock_label": pick.clock_label,
        "event_tier": pick.event_tier,
        "event_titles": list(pick.event_titles),
        "tags": list(pick.tags),
        "machines": [
            {
                "name": machine.name,
                "status_label": machine.status_label,
                "unit_count": machine.unit_count,
                "avg_games": machine.avg_games,
                "total_games": machine.total_games,
                "synthetic_probability": machine.synthetic_probability,
            }
            for machine in pick.machines
        ],
        "digits": [
            {
                "digit": item.digit,
                "avg_setting": item.avg_setting,
                "unit_count": item.unit_count,
                "status_label": item.status_label,
            }
            for item in pick.digits
        ],
    }


def _heads_up_text(args, text: str) -> str:
    if not getattr(args, "heads_up", False):
        return text
    return f"5分後に投稿します。\n\n{text.strip()}"


def _heads_up_skip(args, slot: str) -> None:
    if not getattr(args, "heads_up", False) or not args.discord:
        return
    when = {
        "morning": "10:00",
        "pickup": "11:50",
        "midday": "14:00",
        "afternoon": "17:00",
        "evening": "19:00",
        "night": "21:00",
    }.get(slot, slot)
    try:
        status = send_x_preview(
            f"5分後の{when}は、全台系・末尾・並びが出ていないので投稿しません。",
            label="【投稿予告】",
        )
    except DiscordPreviewError as exc:
        print(str(exc), file=sys.stderr)
        return
    print(f"Discordへ見送り予告を送りました (HTTP {status})")


def _empty_message(selection: Selection) -> str:
    parts = [
        f"{EXCLUSION_LABELS.get(key, key)} {count}"
        for key, count in selection.excluded_counts.items()
        if count
    ]
    detail = "、".join(parts) if parts else "対象ホールなし"
    return (
        f"{display_name(selection.date)} は投稿できるホールがありません。"
        f"全6・全56・全456も、平均推測設定の厚い末尾もありません。"
        f"（{detail}）"
    )


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _write_json(path: Path, payload: dict) -> None:
    _write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _clear(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


if __name__ == "__main__":
    raise SystemExit(main())
