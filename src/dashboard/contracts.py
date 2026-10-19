"""契約管理画面向けの会員一覧。時刻は日本時間で返す。"""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone

from src.dashboard.members import contract_records

JST = timezone(timedelta(hours=9))
_FILTERS = {"all", "trialing", "active", "past_due", "canceled", "ending_this_week"}
_STATUS_LABEL = {
    "trialing": "無料体験中",
    "active": "有料課金中",
    "canceled": "解約済",
    "past_due": "決済失敗",
    "unpaid": "未払い",
    "trial_expired": "体験終了",
    "expired": "期限切れ",
    "incomplete": "未完了",
    "incomplete_expired": "未完了",
    "paused": "停止",
}
_BADGE = {
    "trialing": "trialing",
    "active": "active",
    "canceled": "canceled",
    "past_due": "past_due",
    "unpaid": "past_due",
    "trial_expired": "canceled",
    "expired": "canceled",
    "paused": "canceled",
}
_PHASE = {
    "trialing": "無料",
    "active": "有料",
    "past_due": "決済失敗",
    "unpaid": "決済失敗",
}


class ContractQueryError(Exception):
    """絞り込み条件を解釈できない。"""


def list_contracts(*, query: str = "", status: str = "all", now: int | None = None) -> dict:
    chosen = (status or "all").strip().lower()
    if chosen not in _FILTERS:
        raise ContractQueryError("絞り込みを確認できません")
    current = int(datetime.now(JST).timestamp() if now is None else now)
    week_start, week_end = _week_bounds(current)
    needle = " ".join((query or "").split()).lower()[:200]
    rows = [_present(item, current, week_start, week_end) for item in contract_records()]
    rows.sort(key=lambda item: item["sort_at"], reverse=True)
    summary = _summary(rows, week_start, week_end)
    visible = [item for item in rows if _matches(item, needle, chosen)]
    for item in visible:
        item.pop("sort_at", None)
    return {
        "summary": summary,
        "generated_at": _format_jst(current),
        "members": visible,
    }


def _present(member: dict, now: int, week_start: int, week_end: int) -> dict:
    status = str(member.get("status") or "")
    trial_start = member.get("trial_start")
    trial_end = member.get("trial_end")
    started = member.get("subscription_started_at") or member.get("created_at") or 0
    billing = _next_billing(member, status)
    ending = status == "trialing" and bool(trial_end) and week_start <= int(trial_end) < week_end
    customer_id = str(member.get("stripe_customer_id") or "")
    return {
        "user_id": str(member.get("id") or ""),
        "customer_id": customer_id,
        "subscription_id": str(member.get("stripe_subscription_id") or ""),
        "email": str(member.get("email") or ""),
        "name": str(member.get("name") or ""),
        "created_at": started or None,
        "created_at_jst": _format_jst(started),
        "trial_start": trial_start,
        "trial_start_jst": _format_jst(trial_start),
        "trial_end": trial_end,
        "trial_end_jst": _format_jst(trial_end),
        "trial_range": _trial_range(trial_start, trial_end),
        "trial_remaining_label": _remaining_label(trial_end, now, status),
        "ending_this_week": ending,
        "next_billing_date": billing,
        "next_billing_jst": _format_jst(billing),
        "status": status,
        "status_label": _STATUS_LABEL.get(status, status or "不明"),
        "badge": _BADGE.get(status, "muted"),
        "phase": _PHASE.get(status, "停止"),
        "plan_name": str(member.get("plan_name") or ""),
        "sort_at": int(started or 0),
    }


def _summary(rows: list[dict], week_start: int, week_end: int) -> dict:
    return {
        "total": len(rows),
        "trialing": sum(1 for item in rows if item["status"] == "trialing"),
        "active": sum(1 for item in rows if item["status"] == "active"),
        "ending_this_week": sum(1 for item in rows if item["ending_this_week"]),
        "week_start_jst": _format_jst(week_start),
        "week_end_jst": _format_jst(week_end - 60),
    }


def _matches(item: dict, needle: str, status: str) -> bool:
    if status == "ending_this_week":
        if not item["ending_this_week"]:
            return False
    elif status != "all" and item["status"] != status:
        return False
    if not needle:
        return True
    haystack = " ".join(
        [
            item["email"],
            item["name"],
            item["customer_id"],
            item["user_id"],
            item["subscription_id"],
        ]
    ).lower()
    return needle in haystack


def _next_billing(member: dict, status: str) -> int | None:
    period_end = member.get("current_period_end")
    trial_end = member.get("trial_end")
    if status == "trialing":
        return trial_end or period_end
    if status in {"active", "past_due", "unpaid"}:
        return period_end or None
    return None


def _trial_range(start: int | None, end: int | None) -> str:
    left = _format_jst(start)
    right = _format_jst(end)
    if left and right:
        return f"{left} 〜 {right}"
    return right or left


def _remaining_label(trial_end: int | None, now: int, status: str) -> str:
    if not trial_end:
        return ""
    delta = int(trial_end) - now
    if status == "trialing" and delta > 0:
        if delta < 3600:
            return "まもなく終了"
        if delta < 86400:
            hours = (delta + 3599) // 3600
            return f"あと{hours}時間"
        days = (delta + 86399) // 86400
        return f"あと{days}日"
    if delta <= 0:
        return "終了"
    return ""


def _week_bounds(now: int) -> tuple[int, int]:
    moment = datetime.fromtimestamp(now, JST)
    start = datetime(moment.year, moment.month, moment.day, tzinfo=JST) - timedelta(days=moment.weekday())
    end = start + timedelta(days=7)
    return int(start.timestamp()), int(end.timestamp())


def _format_jst(stamp: object) -> str:
    if stamp is None or stamp == "":
        return ""
    try:
        number = int(stamp)
    except (TypeError, ValueError):
        return ""
    if number <= 0:
        return ""
    try:
        return datetime.fromtimestamp(number, JST).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return ""

