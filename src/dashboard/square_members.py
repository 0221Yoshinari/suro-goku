"""LINE / Square の会員台帳。

DATABASE_URL が Postgres のときは、請求サーバーとツール本体が同じ台帳を読む。
それ以外で SQUARE_MEMBER_DB があればその sqlite を使う。
どちらも無ければ data/square_members.sqlite。パスワードやトークンは保存しない。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[2]
JST = timezone(timedelta(hours=9))
# 登録日を0日目とし、11日目に有料課金が始まるまでの利用期限。
TRIAL_DAYS = 11
_LOCK = threading.Lock()
_READY: set[str] = set()
_ENGINES: dict[str, object] = {}
_ID_RE = re.compile(r"^sm_[0-9a-f]{16}$")
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,}$")
_STATUSES = {"pending", "trialing", "active", "canceled", "past_due", "suspended"}
_FILTERS = {"all", "pending", "trialing", "active", "canceled", "hold"}
_SORTS = {
    "registered_at",
    "trial_end",
    "email",
    "status",
    "line_user_id",
    "line_name",
    "square_customer_id",
}
_STATUS_RANK = {
    "pending": 0,
    "trialing": 1,
    "active": 2,
    "past_due": 3,
    "suspended": 4,
    "canceled": 5,
}
_STATUS_LABEL = {
    "pending": "申込中",
    "trialing": "無料体験中",
    "active": "有料会員",
    "canceled": "解約済み",
    "past_due": "決済エラー/一時停止",
    "suspended": "アカウント停止",
}
_BADGE = {
    "pending": "muted",
    "trialing": "trialing",
    "active": "active",
    "canceled": "canceled",
    "past_due": "past_due",
    "suspended": "past_due",
}
_ACTIONS = {"extend_trial", "force_active", "cancel", "suspend"}
_COLUMNS = (
    "id",
    "line_user_id",
    "line_name",
    "email",
    "status",
    "registered_at",
    "trial_end",
    "square_customer_id",
    "square_subscription_id",
    "paid_at",
    "access_until",
    "status_source",
    "status_event_at",
    "updated_at",
)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS square_members (
  id TEXT PRIMARY KEY,
  line_user_id TEXT NOT NULL DEFAULT '',
  line_name TEXT NOT NULL DEFAULT '',
  email TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',
  registered_at BIGINT NOT NULL DEFAULT 0,
  trial_end BIGINT,
  square_customer_id TEXT NOT NULL DEFAULT '',
  square_subscription_id TEXT NOT NULL DEFAULT '',
  paid_at BIGINT,
  access_until BIGINT,
  status_source TEXT NOT NULL DEFAULT '',
  status_event_at BIGINT NOT NULL DEFAULT 0,
  updated_at BIGINT NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS square_events (
  event_id TEXT PRIMARY KEY,
  received_at BIGINT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS square_members_email_idx ON square_members (email);
CREATE INDEX IF NOT EXISTS square_members_line_user_idx ON square_members (line_user_id);
"""
# ツールを開くたびに会員全員を引き直すと、判定APIの前に待ちが出る。
_MEMBER_CACHE_TTL_SECONDS = 15.0
_MEMBER_CACHE: dict[str, tuple[int, float, list[dict]]] = {}
_MEMBER_CACHE_GEN = 0
_MEMBER_CACHE_LOCK = threading.Lock()
_INSERT = (
    "INSERT INTO square_members ("
    + ", ".join(_COLUMNS)
    + ") VALUES ("
    + ", ".join(f":{name}" for name in _COLUMNS)
    + ") ON CONFLICT(id) DO UPDATE SET "
    + ", ".join(f"{name} = excluded.{name}" for name in _COLUMNS if name != "id")
)


class SquareMemberError(Exception):
    """台帳の操作を受け付けられない。"""


class SquareWebhookError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def init_square_store() -> str:
    try:
        kind = describe_backend()
        _backend()
    except Exception as exc:
        print(f"会員管理台帳を初期化できません: {type(exc).__name__}", flush=True)
        return "unavailable"
    print(f"会員管理台帳: {kind}", flush=True)
    return kind


def describe_backend() -> str:
    kind, _target = _resolve()
    return kind


def square_signature_valid(body: bytes, signature: str, notification_url: str, key: str) -> bool:
    """Square の通知署名。通知URLと本文を連結して HMAC-SHA256 し、Base64 と比較する。"""
    if not body or not signature or not notification_url or not key:
        return False
    digest = hmac.new(key.encode("utf-8"), notification_url.encode("utf-8") + body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("ascii")
    return hmac.compare_digest(expected, signature.strip())


def handle_square_webhook(body: bytes, signature: str) -> dict:
    key = (os.environ.get("SQUARE_WEBHOOK_SIGNATURE_KEY") or "").strip()
    url = (os.environ.get("SQUARE_WEBHOOK_NOTIFICATION_URL") or "").strip()
    if not square_signature_valid(body, signature, url, key):
        raise SquareWebhookError(401, "署名を確認できません")
    if len(body) > 1_000_000:
        raise SquareWebhookError(413, "通知が大きすぎます")
    try:
        import json

        payload = json.loads(body.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise SquareWebhookError(400, "通知の形式を確認できません") from exc
    if not isinstance(payload, dict):
        raise SquareWebhookError(400, "通知の形式を確認できません")
    try:
        result = apply_square_event(payload)
    except SquareMemberError as exc:
        raise SquareWebhookError(400, str(exc)) from exc
    return {"ok": True, "result": result}


def record_line_signup(
    *,
    line_user_id: str,
    email: str,
    square_customer_id: str = "",
    line_name: str = "",
    when: int | None = None,
) -> dict | None:
    """メールを受け取った時点の申込。既存の課金状態は巻き戻さない。"""
    user_id = _clean_token(line_user_id, 100)
    cleaned_email = _clean_email(email)
    if not user_id and not cleaned_email and not square_customer_id:
        return None
    current = int(time.time() if when is None else when)
    with _LOCK:
        rows = _load_members()
        member = _locate(
            rows,
            line_user_id=user_id,
            customer_id=_clean_token(square_customer_id, 128),
            email=cleaned_email,
        )
        created = member is None
        if member is None:
            member = _new_member(current)
            member["status"] = "pending"
            member["status_source"] = "line"
            member["status_event_at"] = current
        _fill(
            member,
            line_user_id=user_id,
            line_name=line_name,
            email=cleaned_email,
            customer_id=square_customer_id,
        )
        member["updated_at"] = current
        _save_member(member)
        if created:
            rows.append(member)
        return _present(member, current)


def record_card_trial(
    *,
    line_user_id: str,
    email: str,
    square_customer_id: str,
    subscription_id: str,
    when: int | None = None,
) -> dict | None:
    """カード登録が終わった申込を無料体験にする。登録日が0日目で、11日目から有料。"""
    current = int(time.time() if when is None else when)
    with _LOCK:
        rows = _load_members()
        member = _locate(
            rows,
            line_user_id=_clean_token(line_user_id, 100),
            customer_id=_clean_token(square_customer_id, 128),
            email=_clean_email(email),
        )
        if member is None:
            return None
        _fill(
            member,
            line_user_id=line_user_id,
            email=email,
            customer_id=square_customer_id,
            subscription_id=subscription_id,
        )
        _enter_trial(member, current, None)
        member["updated_at"] = current
        _save_member(member)
        return _present(member, current)


# 画面上の「無料体験中」は trialing、「有料会員」は active。これ以外はツールを開けない。
_TOOL_ALLOWED = {"trialing", "active"}


def find_member_by_email(email: str, now: int | None = None) -> dict | None:
    """メールアドレスの会員。カード登録URLを出すときに使う。"""
    current = int(time.time() if now is None else now)
    cleaned = _clean_email(email)
    if not cleaned:
        return None
    with _LOCK:
        member = _locate(_load_members(), email=cleaned)
        if member is None:
            return None
        return _present(member, current)


def find_member_by_line(line_user_id: str, now: int | None = None) -> dict | None:
    """LINE ID の会員。サブスクリプションIDを含む。無ければ None。"""
    current = int(time.time() if now is None else now)
    user_id = _clean_token(line_user_id, 100)
    if not user_id:
        return None
    with _LOCK:
        member = _locate(_load_members(), line_user_id=user_id)
        if member is None:
            return None
        return _present(member, current)


def cancel_member_record(
    line_user_id: str,
    when: int | None = None,
    *,
    access_until: int | None = None,
) -> bool:
    """台帳を解約済みにする。access_until までは閲覧を残す。会員がいないときは False。"""
    current = int(time.time() if when is None else when)
    user_id = _clean_token(line_user_id, 100)
    if not user_id:
        return False
    with _LOCK:
        rows = _load_members()
        member = _locate(rows, line_user_id=user_id)
        if member is None:
            return False
        _set_status(member, "canceled", current, "line", override_operator=True)
        member["access_until"] = _optional_int(access_until)
        _retain_unpaid_trial(member, current)
        member["updated_at"] = current
        _save_member(member)
        return True


def tool_access(line_user_id: str, now: int | None = None) -> dict:
    """LINE ID で台帳を引く。呼び出しのたびに台帳を読む。"""
    return _access_for(line_user_id=line_user_id, email="", now=now)


def email_access(email: str, now: int | None = None) -> dict:
    """ログインに使ったメールアドレスで台帳を引く。手元に無ければ請求サーバーへ聞く。"""
    from src.dashboard.square_sync import ensure_square_members
    from src.dashboard.square_sync import needs_square_restore

    if needs_square_restore() and not _should_ask_billing() and not ensure_square_members():
        raise SquareMemberError("会員確認ができません。しばらくしてから再度お試しください。")
    local = _access_for(line_user_id="", email=email, now=now)
    if local["allowed"] or not _should_ask_billing():
        return local
    remote = _ask_billing(email)
    if remote is None:
        raise SquareMemberError("会員確認ができません。しばらくしてから再度お試しください。")
    if remote.get("allowed"):
        return remote
    return local


def restore_members_from_square(records: list[dict], *, now: int | None = None) -> int:
    """Square の定期購入から台帳を作り直す。再起動で消えた sqlite を戻すため。"""
    current = int(time.time() if now is None else now)
    restored = 0
    with _LOCK:
        rows = _load_members()
        for record in records:
            if not isinstance(record, dict):
                continue
            email = _clean_email(str(record.get("email") or ""))
            customer_id = _clean_token(str(record.get("customer_id") or ""), 128)
            line_user_id = _clean_token(str(record.get("line_user_id") or ""), 100)
            if not email and not customer_id:
                continue
            member = _locate(rows, line_user_id=line_user_id, customer_id=customer_id, email=email)
            if member is None:
                member = _new_member(current)
                rows.append(member)
            square_status = str(record.get("status") or "").upper()
            _fill(
                member,
                line_user_id=line_user_id,
                line_name=str(record.get("line_name") or ""),
                email=email,
                customer_id=customer_id,
                subscription_id=str(record.get("subscription_id") or ""),
            )
            _apply_note(member, record.get("note"))
            if square_status in {"CANCELED", "DEACTIVATED"}:
                _set_status(member, "canceled", current, "square", override_operator=True)
                _retain_unpaid_trial(member, current)
            elif square_status in {"ACTIVE", "PENDING"}:
                _enter_trial(member, current, record.get("start_date"))
            elif not member.get("status"):
                member["status"] = "pending"
                member["status_source"] = "square"
            member["updated_at"] = current
            _save_member(member)
            restored += 1
    return restored


_BILLING_CACHE: dict[str, tuple[float, dict]] = {}


def _billing_member_url() -> str:
    explicit = (os.environ.get("BILLING_MEMBER_URL") or "").strip().rstrip("/")
    if explicit:
        return explicit
    if (os.environ.get("RENDER") or "").strip().lower() in {"1", "true", "yes"}:
        return "https://suro-goku-billing.onrender.com"
    return ""


def _should_ask_billing() -> bool:
    if (os.environ.get("MEMBER_LEDGER_SOURCE") or "").strip().lower() == "local":
        return False
    url = _billing_member_url()
    if not url:
        return False
    own = (os.environ.get("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
    return own != url


def _ask_billing(email: str) -> dict | None:
    cleaned = _clean_email(email)
    if not cleaned:
        return None
    cached = _BILLING_CACHE.get(cleaned)
    if cached and time.time() - cached[0] < 60:
        return cached[1]
    import json
    from urllib.error import HTTPError
    from urllib.parse import urlencode
    from urllib.request import Request
    from urllib.request import urlopen

    url = _billing_member_url() + "/member-access?" + urlencode({"email": cleaned})
    headers = {"Accept": "application/json"}
    token = (os.environ.get("MASTER_PANEL_TOKEN") or "").strip()
    if token:
        headers["x-master-token"] = token
    payload = None
    for attempt in range(2):
        try:
            with urlopen(Request(url, headers=headers), timeout=25) as response:
                payload = json.loads(response.read().decode("utf-8"))
            break
        except HTTPError as exc:
            if exc.code >= 500 and attempt == 0:
                time.sleep(1)
                continue
            print("請求サーバーの会員確認に失敗しました", flush=True)
            return None
        except Exception:
            if attempt == 0:
                time.sleep(1)
                continue
            print("請求サーバーの会員確認に失敗しました", flush=True)
            return None
    if not isinstance(payload, dict):
        return None
    view = _access_view(
        bool(payload.get("allowed")),
        str(payload.get("reason") or ""),
        str(payload.get("status") or ""),
        str(payload.get("line_user_id") or ""),
        str(payload.get("email") or cleaned),
    )
    _BILLING_CACHE[cleaned] = (time.time(), view)
    return view


def _access_for(*, line_user_id: str, email: str, now: int | None) -> dict:
    """無料体験中と有料会員を許可する。未課金の解約は体験終了まで残す。期限切れと決済失敗は拒否する。"""
    current = int(time.time() if now is None else now)
    user_id = _clean_token(line_user_id, 100)
    cleaned = _clean_email(email)
    if not user_id and not cleaned:
        return _access_view(False, "unregistered", "", "", "")
    with _LOCK:
        rows = _load_members()
        member = _locate(rows, line_user_id=user_id, email=cleaned)
    identity = cleaned or user_id
    if member is None:
        return _access_view(False, "unregistered", "", user_id, cleaned)
    status = str(member.get("status") or "")
    if status == "canceled":
        until = _optional_int(member.get("access_until"))
        if not member.get("paid_at"):
            trial_end = _optional_int(member.get("trial_end"))
            if trial_end is not None and trial_end > current:
                until = trial_end if until is None else max(until, trial_end)
        if until is not None and until > current:
            return _access_view(
                True,
                "cancel_scheduled",
                status,
                user_id or str(member.get("line_user_id") or ""),
                cleaned or str(member.get("email") or ""),
            )
    if status not in _TOOL_ALLOWED:
        reason = {
            "canceled": "canceled",
            "past_due": "past_due",
            "suspended": "suspended",
            "pending": "pending",
        }.get(status, "unregistered")
        return _access_view(False, reason, status, user_id, cleaned or str(member.get("email") or ""))
    if status == "trialing":
        trial_end = member.get("trial_end")
        if trial_end is not None and int(trial_end) <= current:
            return _access_view(False, "trial_ended", status, user_id, cleaned or str(member.get("email") or ""))
    return _access_view(True, "ok", status, user_id or str(member.get("line_user_id") or ""), cleaned or str(member.get("email") or ""))


def _access_view(allowed: bool, reason: str, status: str, line_user_id: str, email: str = "") -> dict:
    return {
        "allowed": allowed,
        "reason": reason,
        "status": status,
        "line_user_id": line_user_id,
        "email": email,
    }


def list_members(
    *,
    query: str = "",
    status: str = "all",
    sort: str = "registered_at",
    order: str = "desc",
    now: int | None = None,
) -> dict:
    chosen = (status or "all").strip().lower()
    sort_key = (sort or "registered_at").strip().lower()
    direction = (order or "desc").strip().lower()
    if chosen not in _FILTERS or sort_key not in _SORTS or direction not in {"asc", "desc"}:
        raise SquareMemberError("絞り込みを確認できません")
    current = int(time.time() if now is None else now)
    needle = " ".join((query or "").split()).lower()[:200]
    with _LOCK:
        rows = _load_members()
    summary = _summary(rows)
    visible = [_present(item, current) for item in rows if _matches(item, needle, chosen)]
    reverse = direction == "desc"
    visible.sort(key=lambda item: _sort_value(item, sort_key), reverse=reverse)
    return {
        "summary": summary,
        "generated_at": _format_jst(current),
        "sort": sort_key,
        "order": direction,
        "members": visible,
    }


def apply_member_action(member_id: str, action: str, *, days: int = 7, now: int | None = None) -> dict:
    member_id = (member_id or "").strip()
    action = (action or "").strip().lower()
    if not _ID_RE.fullmatch(member_id):
        raise SquareMemberError("会員が見つかりません")
    if action not in _ACTIONS:
        raise SquareMemberError("操作を確認できません")
    try:
        extra_days = int(days)
    except (TypeError, ValueError) as exc:
        raise SquareMemberError("延長日数を確認できません") from exc
    if action == "extend_trial" and not 1 <= extra_days <= 90:
        raise SquareMemberError("延長日数は1日から90日です")
    current = int(time.time() if now is None else now)
    with _LOCK:
        rows = _load_members()
        member = next((item for item in rows if item["id"] == member_id), None)
        if member is None:
            raise SquareMemberError("会員が見つかりません")
        if action == "extend_trial":
            base = member.get("trial_end") or current
            member["trial_end"] = max(int(base), current) + extra_days * 86400
            member["status"] = "trialing"
        elif action == "force_active":
            member["status"] = "active"
            if not member.get("paid_at"):
                member["paid_at"] = current
        elif action == "cancel":
            member["status"] = "canceled"
        else:
            member["status"] = "suspended"
        member["status_source"] = "operator"
        member["status_event_at"] = current
        member["updated_at"] = current
        _save_member(member)
        summary = _summary(rows)
        presented = _present(member, current)
    return {"member": presented, "summary": summary}


def apply_square_event(payload: dict) -> str:
    event_id = _clean_token(str(payload.get("event_id") or ""), 200)
    if not event_id:
        raise SquareMemberError("通知の形式を確認できません")
    event_type = str(payload.get("type") or "").strip().lower()
    when = _parse_time(payload.get("created_at"))
    with _LOCK:
        if _event_seen(event_id):
            return "duplicate"
        changed = _dispatch(event_type, payload, when)
        _remember_event(event_id, when)
    return "updated" if changed else "ignored"


def _dispatch(event_type: str, payload: dict, when: int) -> bool:
    obj = _event_object(payload)
    if event_type in {"invoice.payment_made", "invoice.updated", "invoice.payment_failed", "invoice.scheduled_charge_failed"}:
        return _apply_invoice(obj, event_type, when)
    if event_type in {"payment.updated", "payment.created"}:
        return _apply_payment(obj, when)
    if event_type in {"subscription.created", "subscription.updated"}:
        return _apply_subscription(obj, when)
    return False


def _apply_invoice(invoice: dict, event_type: str, when: int) -> bool:
    if not invoice:
        return False
    amount = _invoice_amount(invoice)
    customer_id, email = _invoice_customer(invoice)
    subscription_id = _clean_token(str(invoice.get("subscription_id") or ""), 128)
    member = _ensure_member(
        when,
        customer_id=customer_id,
        subscription_id=subscription_id,
        email=email,
    )
    _fill(member, email=email, customer_id=customer_id, subscription_id=subscription_id)
    invoice_status = str(invoice.get("status") or "").upper()
    failed = event_type in {"invoice.payment_failed", "invoice.scheduled_charge_failed"} or (
        event_type == "invoice.updated" and invoice_status in {"UNPAID", "FAILED"}
    )
    paid = event_type == "invoice.payment_made" or (event_type == "invoice.updated" and invoice_status == "PAID")
    changed = False
    if failed and event_type != "invoice.payment_made":
        changed = _set_status(member, "past_due", when, "square", override_operator=True)
    elif paid and amount > 0:
        if not member.get("paid_at"):
            member["paid_at"] = when
            changed = True
        changed = _set_status(member, "active", when, "square", override_operator=True) or changed
    elif paid and amount == 0:
        changed = _enter_trial(member, when, invoice.get("scheduled_at") or invoice.get("due_date"))
    member["updated_at"] = int(time.time())
    _save_member(member)
    return changed


def _apply_payment(payment: dict, when: int) -> bool:
    if not payment:
        return False
    status = str(payment.get("status") or "").upper()
    money = payment.get("amount_money") if isinstance(payment.get("amount_money"), dict) else {}
    try:
        amount = int(money.get("amount") or 0)
    except (TypeError, ValueError):
        amount = 0
    note = str(payment.get("note") or "")
    line_user_id = ""
    if note.startswith("LINE:"):
        line_user_id = _clean_token(note.split(":", 1)[1], 100)
    customer_id = _clean_token(str(payment.get("customer_id") or ""), 128)
    member = _ensure_member(when, customer_id=customer_id, line_user_id=line_user_id)
    _fill(member, line_user_id=line_user_id, customer_id=customer_id)
    changed = False
    if status == "FAILED":
        changed = _set_status(member, "past_due", when, "square", override_operator=True)
    elif status == "COMPLETED" and amount > 0:
        if not member.get("paid_at"):
            member["paid_at"] = when
            changed = True
        changed = _set_status(member, "active", when, "square", override_operator=True) or changed
    elif status == "COMPLETED" and amount == 0:
        changed = _enter_trial(member, when, None)
    member["updated_at"] = int(time.time())
    _save_member(member)
    return changed


def _apply_subscription(subscription: dict, when: int) -> bool:
    if not subscription:
        return False
    customer_id = _clean_token(str(subscription.get("customer_id") or ""), 128)
    subscription_id = _clean_token(str(subscription.get("id") or ""), 128)
    member = _ensure_member(when, customer_id=customer_id, subscription_id=subscription_id)
    _fill(member, customer_id=customer_id, subscription_id=subscription_id)
    status = str(subscription.get("status") or "").upper()
    changed = False
    canceling = status in {"CANCELED", "DEACTIVATED"}
    prior_trial_end = _optional_int(member.get("trial_end"))
    charge_at = _charged_through(subscription.get("charged_through_date"))
    if charge_at and not member.get("paid_at") and member.get("status_source") != "operator":
        # 解約通知の請求日で、残っている無料期間を短くしない。
        if canceling and prior_trial_end and charge_at < prior_trial_end:
            pass
        elif member.get("trial_end") != charge_at:
            member["trial_end"] = charge_at
            changed = True
    if canceling:
        changed = _set_status(member, "canceled", when, "square", override_operator=True) or changed
        if _retain_unpaid_trial(member, when):
            changed = True
    elif status == "PAUSED":
        changed = _set_status(member, "suspended", when, "square", override_operator=True) or changed
    elif status in {"ACTIVE", "PENDING", ""}:
        changed = _enter_trial(member, when, subscription.get("start_date")) or changed
    member["updated_at"] = int(time.time())
    _save_member(member)
    return changed


def _retain_unpaid_trial(member: dict, when: int) -> bool:
    """カード登録後、まだ月額を払っていない解約は、無料期間の終わりまでログインを残す。"""
    if member.get("paid_at"):
        return False
    trial_end = _optional_int(member.get("trial_end"))
    if trial_end is None or trial_end <= int(when):
        return False
    current_until = _optional_int(member.get("access_until"))
    if current_until is not None and current_until >= trial_end:
        return False
    member["access_until"] = trial_end
    return True


def _enter_trial(member: dict, when: int, start_hint: object) -> bool:
    changed = False
    if not member.get("trial_end"):
        start = _charged_through(start_hint) or when
        # charged_through は「その日まで請求済み」の翌日。日付ヒントが開始日なら11日後（11日目）にする。
        if _looks_like_date(start_hint):
            start_day = _date_stamp(start_hint)
            member["trial_end"] = (start_day or when) + TRIAL_DAYS * 86400
        else:
            member["trial_end"] = start + TRIAL_DAYS * 86400
        changed = True
    if member.get("paid_at"):
        return _set_status(member, "active", when, "square") or changed
    if member.get("status") in {"active", "canceled", "suspended", "past_due"}:
        return changed
    return _set_status(member, "trialing", when, "square") or changed


def _ensure_member(
    when: int,
    *,
    customer_id: str = "",
    subscription_id: str = "",
    email: str = "",
    line_user_id: str = "",
) -> dict:
    rows = _load_members()
    member = _locate(
        rows,
        line_user_id=line_user_id,
        customer_id=customer_id,
        subscription_id=subscription_id,
        email=email,
    )
    if member is None:
        member = _new_member(when)
        member["status"] = "pending"
        member["status_source"] = "square"
        member["status_event_at"] = 0
    return member


def _set_status(member: dict, status: str, when: int, source: str, *, override_operator: bool = False) -> bool:
    if status not in _STATUSES:
        return False
    operator_hold = member.get("status") == "suspended" and member.get("status_source") == "operator"
    if source != "operator" and operator_hold:
        return False
    if source != "operator" and member.get("status_source") == "operator" and not override_operator:
        return False
    if not _can_replace(member, when):
        return False
    if member.get("status") == status and member.get("status_source") == source:
        member["status_event_at"] = max(int(member.get("status_event_at") or 0), int(when))
        return False
    member["status"] = status
    member["status_source"] = source
    member["status_event_at"] = int(when)
    return True


def _can_replace(member: dict, when: int) -> bool:
    previous = int(member.get("status_event_at") or 0)
    return int(when) >= previous


def _locate(
    rows: list[dict],
    *,
    line_user_id: str = "",
    customer_id: str = "",
    subscription_id: str = "",
    email: str = "",
) -> dict | None:
    checks = (
        ("square_subscription_id", subscription_id),
        ("square_customer_id", customer_id),
        ("line_user_id", line_user_id),
        ("email", email),
    )
    for field, expected in checks:
        if not expected:
            continue
        for row in rows:
            if str(row.get(field) or "") == expected:
                return row
    return None


def _fill(
    member: dict,
    *,
    line_user_id: str = "",
    line_name: str = "",
    email: str = "",
    customer_id: str = "",
    subscription_id: str = "",
) -> None:
    user_id = _clean_token(line_user_id, 100)
    if user_id:
        member["line_user_id"] = user_id
    name = _clean_label(line_name)
    if name:
        member["line_name"] = name
    cleaned = _clean_email(email)
    if cleaned:
        member["email"] = cleaned
    customer = _clean_token(customer_id, 128)
    if customer:
        member["square_customer_id"] = customer
    subscription = _clean_token(subscription_id, 128)
    if subscription:
        member["square_subscription_id"] = subscription


def _summary(rows: list[dict]) -> dict:
    started = [item for item in rows if item.get("status") != "pending"]
    converted = [item for item in started if item.get("paid_at")]
    cvr = None
    if started:
        cvr = round(len(converted) * 1000 / len(started) / 10, 1)
    return {
        "total": len(rows),
        "trialing": sum(1 for item in rows if item.get("status") == "trialing"),
        "active": sum(1 for item in rows if item.get("status") == "active"),
        "pending": sum(1 for item in rows if item.get("status") == "pending"),
        "started": len(started),
        "converted": len(converted),
        "cvr": cvr,
        "cvr_label": "—" if cvr is None else f"{cvr:.1f}%",
        "formula": "有料化した人数 / 無料体験を開始した人数",
    }


def _matches(member: dict, needle: str, status: str) -> bool:
    current = str(member.get("status") or "")
    if status == "hold":
        if current not in {"past_due", "suspended"}:
            return False
    elif status != "all" and current != status:
        return False
    if not needle:
        return True
    haystack = " ".join(
        [
            str(member.get("email") or ""),
            str(member.get("line_name") or ""),
            str(member.get("line_user_id") or ""),
            str(member.get("square_customer_id") or ""),
            str(member.get("square_subscription_id") or ""),
            str(member.get("id") or ""),
        ]
    ).lower()
    return needle in haystack


def _present(member: dict, now: int) -> dict:
    status = str(member.get("status") or "")
    trial_end = member.get("trial_end")
    return {
        "id": member.get("id") or "",
        "line_user_id": member.get("line_user_id") or "",
        "line_name": member.get("line_name") or "",
        "email": member.get("email") or "",
        "status": status,
        "status_label": _STATUS_LABEL.get(status, "不明"),
        "badge": _BADGE.get(status, "muted"),
        "registered_at": member.get("registered_at") or None,
        "registered_at_jst": _format_jst(member.get("registered_at")),
        "trial_end": trial_end,
        "trial_end_jst": _format_jst(trial_end),
        "trial_remaining_label": _remaining_label(trial_end, now, status),
        "square_customer_id": member.get("square_customer_id") or "",
        "square_subscription_id": member.get("square_subscription_id") or "",
        "paid_at": member.get("paid_at"),
        "paid_at_jst": _format_jst(member.get("paid_at")),
    }


def _sort_value(item: dict, key: str):
    if key == "status":
        return (_STATUS_RANK.get(str(item.get("status") or ""), 9), str(item.get("email") or ""))
    if key in {"registered_at", "trial_end"}:
        value = item.get(key)
        return int(value or 0)
    return str(item.get(key) or "").lower()


def _remaining_label(trial_end: object, now: int, status: str) -> str:
    if not trial_end or status not in {"trialing", "pending"}:
        return ""
    try:
        delta = int(trial_end) - now
    except (TypeError, ValueError):
        return ""
    if delta <= 0:
        return "終了"
    if delta < 86400:
        hours = max(1, (delta + 3599) // 3600)
        return f"あと{hours}時間"
    days = (delta + 86399) // 86400
    return f"あと{days}日"


def _new_member(now: int) -> dict:
    return {
        "id": "sm_" + secrets.token_hex(8),
        "line_user_id": "",
        "line_name": "",
        "email": "",
        "status": "pending",
        "registered_at": int(now),
        "trial_end": None,
        "square_customer_id": "",
        "square_subscription_id": "",
        "paid_at": None,
        "status_source": "",
        "status_event_at": 0,
        "updated_at": int(now),
    }


def _event_object(payload: dict) -> dict:
    data = payload.get("data")
    if not isinstance(data, dict):
        return {}
    obj = data.get("object")
    if not isinstance(obj, dict):
        return {}
    for key in ("invoice", "subscription", "payment"):
        inner = obj.get(key)
        if isinstance(inner, dict):
            return inner
    return obj


def _invoice_customer(invoice: dict) -> tuple[str, str]:
    recipient = invoice.get("primary_recipient")
    if not isinstance(recipient, dict):
        recipient = {}
    customer_id = _clean_token(str(recipient.get("customer_id") or invoice.get("customer_id") or ""), 128)
    email = _clean_email(str(recipient.get("email_address") or invoice.get("email_address") or ""))
    return customer_id, email


def _invoice_amount(invoice: dict) -> int:
    amounts: list[int] = []
    requests = invoice.get("payment_requests")
    if isinstance(requests, list):
        for item in requests:
            if not isinstance(item, dict):
                continue
            for key in ("total_completed_amount_money", "computed_amount_money", "fixed_amount_requested_money"):
                money = item.get(key)
                if isinstance(money, dict):
                    amounts.append(_money(money))
    for key in ("next_payment_amount_money",):
        money = invoice.get(key)
        if isinstance(money, dict):
            amounts.append(_money(money))
    positive = [item for item in amounts if item > 0]
    if positive:
        return max(positive)
    return 0


def _money(money: dict) -> int:
    try:
        return int(money.get("amount") or 0)
    except (TypeError, ValueError):
        return 0


def _parse_time(value: object) -> int:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if text:
        try:
            return int(datetime.fromisoformat(text).timestamp())
        except ValueError:
            pass
    return int(time.time())


def _looks_like_date(value: object) -> bool:
    text = str(value or "").strip()
    return len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-"


def _date_stamp(value: object) -> int | None:
    text = str(value or "").strip()
    if not _looks_like_date(text):
        return None
    try:
        day = datetime.strptime(text[:10], "%Y-%m-%d").replace(tzinfo=JST)
    except ValueError:
        return None
    return int(day.timestamp())


def _charged_through(value: object) -> int | None:
    start = _date_stamp(value)
    if start is None:
        return None
    return start + 86400


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


def _clean_email(value: str) -> str:
    cleaned = str(value or "").strip().lower()
    if not cleaned or len(cleaned) > 254 or not _EMAIL_RE.fullmatch(cleaned):
        return ""
    return cleaned


def _clean_label(value: str) -> str:
    text = " ".join(str(value or "").split())
    if not text or any(ch in text for ch in "<>\r\n\x00"):
        return ""
    return text[:80]


def _clean_token(value: str, limit: int) -> str:
    text = str(value or "").strip()
    if not text or len(text) > limit or any(ch in text for ch in "\r\n\x00 <>"):
        return ""
    return text


def _database_url() -> str:
    raw = (os.environ.get("DATABASE_URL") or "").strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {'"', "'"}:
        raw = raw[1:-1].strip()
    return raw


def _resolve() -> tuple[str, str]:
    """本番の Postgres を、ローカル sqlite の上書きより先に使う。"""
    url = _database_url()
    if url.startswith("postgres"):
        return "postgres", url
    override = (os.environ.get("SQUARE_MEMBER_DB") or "").strip()
    if override:
        return "sqlite", override
    if url.startswith("sqlite"):
        return "sqlite", str(_sqlite_path(url))
    return "sqlite", str(PROJECT_ROOT / "data" / "square_members.sqlite")


def _sqlite_path(url: str) -> Path:
    parsed = urlparse(url)
    path = parsed.path or ""
    if url.startswith("sqlite:////"):
        return Path(path)
    relative = path.lstrip("/")
    if not relative:
        return PROJECT_ROOT / "data" / "square_members.sqlite"
    candidate = Path(relative)
    if candidate.is_absolute():
        return candidate
    return PROJECT_ROOT / candidate


def _backend():
    kind, target = _resolve()
    if kind == "postgres":
        _ensure_postgres(target)
        return "postgres", target
    path = Path(target)
    _ensure_sqlite(path)
    return "sqlite", path


def _ensure_sqlite(path: Path) -> None:
    key = str(path)
    if key in _READY and path.is_file():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5)
    try:
        connection.executescript(_SCHEMA)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(square_members)")}
        if "access_until" not in columns:
            connection.execute("ALTER TABLE square_members ADD COLUMN access_until BIGINT")
        connection.commit()
    finally:
        connection.close()
    _READY.add(key)


def _connect_sqlite(path: Path) -> sqlite3.Connection:
    _ensure_sqlite(path)
    connection = sqlite3.connect(path, timeout=5)
    connection.row_factory = sqlite3.Row
    return connection


def _drop_member_cache() -> None:
    global _MEMBER_CACHE_GEN
    with _MEMBER_CACHE_LOCK:
        _MEMBER_CACHE_GEN += 1
        _MEMBER_CACHE.clear()


def _cached_members(target: str) -> list[dict] | None:
    with _MEMBER_CACHE_LOCK:
        cached = _MEMBER_CACHE.get(target)
        if cached is None or time.monotonic() - cached[1] >= _MEMBER_CACHE_TTL_SECONDS:
            return None
        return [dict(row) for row in cached[2]]


def _remember_members(target: str, generation: int, rows: list[dict]) -> None:
    with _MEMBER_CACHE_LOCK:
        if generation != _MEMBER_CACHE_GEN:
            return
        _MEMBER_CACHE[target] = (generation, time.monotonic(), [dict(row) for row in rows])


def _load_members() -> list[dict]:
    kind, target = _backend()
    if kind == "postgres":
        cached = _cached_members(str(target))
        if cached is not None:
            return cached
        with _MEMBER_CACHE_LOCK:
            generation = _MEMBER_CACHE_GEN
        rows = _postgres_load(str(target))
        _remember_members(str(target), generation, rows)
        return [dict(row) for row in rows]
    connection = _connect_sqlite(Path(target))
    try:
        found = connection.execute("SELECT * FROM square_members").fetchall()
    except sqlite3.Error as exc:
        raise SquareMemberError("会員台帳を読み取れません") from exc
    finally:
        connection.close()
    return [_member_from_row(row) for row in found]


def _save_member(member: dict) -> None:
    params = _params(member)
    kind, target = _backend()
    if kind == "postgres":
        _postgres_save(str(target), params)
        _drop_member_cache()
        return
    connection = _connect_sqlite(Path(target))
    try:
        connection.execute(_INSERT, params)
        connection.commit()
    except sqlite3.Error as exc:
        connection.rollback()
        raise SquareMemberError("会員台帳を保存できません") from exc
    finally:
        connection.close()
    try:
        from src.dashboard.square_sync import remember_member

        remember_member(dict(member))
    except Exception as exc:
        print(f"会員メモの保存を開始できませんでした: {type(exc).__name__}", flush=True)


def _apply_note(member: dict, note: object) -> None:
    """Square の顧客メモに残した台帳の写しを会員へ戻す。"""
    text = str(note or "")
    if not text.startswith("SURO1:"):
        return
    try:
        import json

        snapshot = json.loads(text[6:])
    except (UnicodeError, ValueError):
        return
    if not isinstance(snapshot, dict):
        return
    _fill(
        member,
        line_user_id=str(snapshot.get("line_user_id") or ""),
        line_name=str(snapshot.get("line_name") or ""),
        email=str(snapshot.get("email") or ""),
        subscription_id=str(snapshot.get("square_subscription_id") or ""),
    )
    status = str(snapshot.get("status") or "")
    if status in _STATUSES:
        member["status"] = status
    source = str(snapshot.get("status_source") or "")
    if source:
        member["status_source"] = source
    registered = _optional_stamp(snapshot.get("registered_at"))
    if registered:
        member["registered_at"] = registered
    for key in ("trial_end", "paid_at"):
        stamp = _optional_stamp(snapshot.get(key))
        if stamp:
            member[key] = stamp


def _optional_stamp(value: object) -> int:
    if value in (None, ""):
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _event_seen(event_id: str) -> bool:
    kind, target = _backend()
    if kind == "postgres":
        from sqlalchemy import text

        engine = _engine(str(target))
        with engine.connect() as connection:
            found = connection.execute(
                text("SELECT event_id FROM square_events WHERE event_id = :event_id"),
                {"event_id": event_id},
            ).first()
        return found is not None
    connection = _connect_sqlite(Path(target))
    try:
        found = connection.execute("SELECT event_id FROM square_events WHERE event_id = ?", (event_id,)).fetchone()
    finally:
        connection.close()
    return found is not None


def _remember_event(event_id: str, when: int) -> None:
    kind, target = _backend()
    if kind == "postgres":
        from sqlalchemy import text

        engine = _engine(str(target))
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO square_events (event_id, received_at) VALUES (:event_id, :received_at) "
                    "ON CONFLICT (event_id) DO NOTHING"
                ),
                {"event_id": event_id, "received_at": int(when)},
            )
        return
    connection = _connect_sqlite(Path(target))
    try:
        connection.execute(
            "INSERT OR IGNORE INTO square_events (event_id, received_at) VALUES (?, ?)",
            (event_id, int(when)),
        )
        connection.commit()
    except sqlite3.Error as exc:
        connection.rollback()
        raise SquareMemberError("会員台帳を保存できません") from exc
    finally:
        connection.close()


def _params(member: dict) -> dict:
    row = {}
    for name in _COLUMNS:
        value = member.get(name)
        if name in {"trial_end", "paid_at", "access_until"}:
            row[name] = _optional_int(value)
        elif name in {"registered_at", "status_event_at", "updated_at"}:
            row[name] = int(value or 0)
        else:
            row[name] = "" if value is None else str(value)
    return row


def _member_from_row(row) -> dict:
    member = {name: row[name] for name in _COLUMNS}
    member["trial_end"] = _optional_int(member.get("trial_end"))
    member["paid_at"] = _optional_int(member.get("paid_at"))
    member["access_until"] = _optional_int(member.get("access_until"))
    for name in ("registered_at", "status_event_at", "updated_at"):
        member[name] = int(member.get(name) or 0)
    for name in _COLUMNS:
        if name not in {"trial_end", "paid_at", "access_until", "registered_at", "status_event_at", "updated_at"}:
            member[name] = "" if member[name] is None else str(member[name])
    return member


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_postgres_url(url: str) -> str:
    if url.startswith("postgresql+psycopg://") or url.startswith("postgresql+psycopg2://"):
        return url
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def _engine(url: str):
    cached = _ENGINES.get(url)
    if cached is not None:
        return cached
    from sqlalchemy import create_engine

    engine = create_engine(_normalize_postgres_url(url), pool_pre_ping=True, connect_args={"connect_timeout": 3})
    _ENGINES[url] = engine
    return engine


def _ensure_postgres(url: str) -> None:
    if url in _READY:
        return
    from sqlalchemy import text

    engine = _engine(url)
    with engine.begin() as connection:
        for statement in (item.strip() for item in _SCHEMA.split(";") if item.strip()):
            connection.execute(text(statement))
        connection.execute(text("ALTER TABLE square_members ADD COLUMN IF NOT EXISTS access_until BIGINT"))
    _READY.add(url)


def _postgres_load(url: str) -> list[dict]:
    from sqlalchemy import text

    engine = _engine(url)
    try:
        with engine.connect() as connection:
            found = connection.execute(text("SELECT * FROM square_members")).mappings().all()
    except Exception as exc:
        raise SquareMemberError("会員台帳を読み取れません") from exc
    return [_member_from_row(row) for row in found]


def _postgres_save(url: str, params: dict) -> None:
    from sqlalchemy import text

    engine = _engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(text(_INSERT), params)
    except Exception as exc:
        raise SquareMemberError("会員台帳を保存できません") from exc
