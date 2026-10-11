"""Stripe と連携する会員台帳。

ライセンスキーと利用状態だけをサーバのファイルに置く。
プロキシ認証、.env、Stripe の秘密鍵はこの台帳にもブラウザにも出さない。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_LOCK = threading.Lock()
_ENTITLED = {"active", "trialing"}
_IDLE_DEFAULT = 20 * 60
_TOUCH_DEFAULT = 30
_GRACE_DEFAULT = 2 * 60 * 60
_CONCURRENT_DEFAULT = 120


class MembersStoreError(Exception):
    """台帳が壊れている。中身は呼び出し元で捨て、権限は出さない。"""


def store_path() -> Path:
    raw = (os.environ.get("MEMBER_STORE_PATH") or "").strip()
    if raw:
        return Path(raw)
    return PROJECT_ROOT / "data" / "members.json"


def open_member_session(
    license_key: str,
    ip: str,
    fp: str,
    now: int | None = None,
) -> tuple[str, str, str]:
    """ライセンスで入室する。戻り値は (会員ID, セッションID, エラー文)。

    未知のキーは空文字を返す。知っているが止めたいキーはエラー文を返す。
    """
    submitted = (license_key or "").strip()
    if not submitted or len(submitted) > 128 or any(ch in submitted for ch in "\r\n\x00"):
        return "", "", ""
    current = int(time.time() if now is None else now)
    with _LOCK:
        try:
            data = _load()
        except MembersStoreError:
            return "", "", "処理に失敗しました。しばらくしてから再度お試しください。"
        member = _find_license(data, submitted)
        if member is None:
            return "", "", ""
        _expire_if_needed(member, current)
        allowed, message = _entitled(member)
        if not allowed:
            _clear_session(member)
            member["updated_at"] = current
            _save(data)
            return "", "", message
        seen = int(member.get("session_seen") or 0)
        active = str(member.get("active_session_id") or "")
        stored_fp = str(member.get("session_fp") or "")
        stored_ip = str(member.get("session_ip") or "")
        if active and stored_fp and fp and stored_fp != fp and current - seen < _idle_seconds():
            return "", "", "別の端末で利用中です"
        if active and stored_ip and ip and stored_ip != ip and current - seen < _concurrent_seconds():
            return "", "", "別の端末で利用中です"
        session_id = secrets.token_hex(16)
        member["active_session_id"] = session_id
        member["session_seen"] = current
        member["session_ip"] = (ip or "")[:80]
        member["session_fp"] = fp or ""
        member["updated_at"] = current
        _save(data)
        return str(member["id"]), session_id, ""


def authorize_member_request(
    member_id: str,
    session_id: str,
    fp: str,
    ip: str = "",
    now: int | None = None,
) -> tuple[str, str]:
    """データ画面への1回分を判定する。ok / revoked / shared / replaced / missing。"""
    current = int(time.time() if now is None else now)
    with _LOCK:
        try:
            data = _load()
        except MembersStoreError:
            return "revoked", "利用権限がありません"
        member = _by_id(data, member_id)
        if member is None:
            return "missing", "ログインが必要です"
        changed = _expire_if_needed(member, current)
        allowed, message = _entitled(member)
        if not allowed:
            if changed or member.get("active_session_id"):
                _clear_session(member)
                member["updated_at"] = current
                _save(data)
            return "revoked", message
        if str(member.get("active_session_id") or "") != session_id:
            return "replaced", "ログインが必要です"
        stored_fp = str(member.get("session_fp") or "")
        stored_ip = str(member.get("session_ip") or "")
        seen = int(member.get("session_seen") or 0)
        if stored_fp and fp and stored_fp != fp:
            # 先に入っている端末は残す。コピーされたCookieからの閲覧だけ止める。
            if current - seen < _idle_seconds():
                return "shared", "別の端末で利用中です"
            return "replaced", "ログインが必要です"
        if stored_ip and ip and stored_ip != ip and current - seen < _concurrent_seconds():
            return "shared", "別の端末で利用中です"
        if current - seen >= _touch_seconds():
            member["session_seen"] = current
            member["updated_at"] = current
            _save(data)
        elif changed:
            _save(data)
        return "ok", ""


def member_session_live(
    member_id: str,
    session_id: str,
    fp: str = "",
    ip: str = "",
    now: int | None = None,
) -> bool:
    state, _message = authorize_member_request(member_id, session_id, fp, ip=ip, now=now)
    return state == "ok"


def close_member_session(member_id: str, session_id: str) -> None:
    with _LOCK:
        try:
            data = _load()
        except MembersStoreError:
            return
        member = _by_id(data, member_id)
        if member is None or str(member.get("active_session_id") or "") != session_id:
            return
        _clear_session(member)
        member["updated_at"] = int(time.time())
        _save(data)


def present_member(member_id: str, now: int | None = None) -> dict | None:
    """画面に出してよい項目だけ返す。秘密鍵や接続情報は含めない。"""
    current = int(time.time() if now is None else now)
    with _LOCK:
        try:
            data = _load()
        except MembersStoreError:
            return None
        member = _by_id(data, member_id)
        if member is None:
            return None
        if _expire_if_needed(member, current):
            _save(data)
        allowed, message = _entitled(member)
        view = _public_copy(member)
        view["entitled"] = allowed
        view["message"] = message if not allowed else ""
        return view


def member_for_checkout(session_id: str) -> dict | None:
    with _LOCK:
        try:
            data = _load()
        except MembersStoreError:
            return None
        member = _find_checkout(data, session_id)
        return _public_copy(member) if member else None


def grant_checkout(
    *,
    when: int,
    session_id: str,
    customer_id: str = "",
    subscription_id: str = "",
    email: str = "",
) -> dict | None:
    """支払い済みの Checkout を台帳へ反映する。同じ決済の再送では状態を巻き戻さない。"""
    session_id = (session_id or "").strip()
    if not session_id:
        return None
    current = int(time.time())
    with _LOCK:
        data = _load()
        member = _locate(data, customer_id, subscription_id, session_id, email)
        created = member is None
        if member is None:
            member = _new_member(current)
            data["members"].append(member)
        _fill_identity(member, customer_id=customer_id, subscription_id=subscription_id, email=email)
        member["checkout_session_id"] = session_id
        if _can_replace_status(member, when, "checkout"):
            member["status"] = "active"
            member["status_source"] = "checkout"
            member["status_event_at"] = int(when)
        member["updated_at"] = current
        if created:
            member["created_at"] = current
        _save(data)
        return _public_copy(member)


def grant_subscription(
    *,
    when: int,
    customer_id: str,
    subscription_id: str,
    status: str,
    trial_end: int | None,
    period_end: int | None,
    deleted: bool = False,
) -> None:
    customer_id = (customer_id or "").strip()
    subscription_id = (subscription_id or "").strip()
    if not customer_id and not subscription_id:
        return
    current = int(time.time())
    with _LOCK:
        data = _load()
        member = _locate(data, customer_id, subscription_id, "", "")
        if member is None:
            if deleted:
                return
            member = _new_member(current)
            data["members"].append(member)
        _fill_identity(member, customer_id=customer_id, subscription_id=subscription_id, email="")
        if _can_replace_status(member, when, "subscription"):
            member["status"] = "canceled" if deleted else (status or "incomplete").strip().lower()
            member["status_source"] = "subscription"
            member["status_event_at"] = int(when)
            if not deleted:
                member["trial_end"] = trial_end
                member["current_period_end"] = period_end
            if member["status"] not in _ENTITLED:
                _clear_session(member)
        member["updated_at"] = current
        _save(data)


def event_seen(event_id: str) -> bool:
    event_id = (event_id or "").strip()
    if not event_id:
        return False
    with _LOCK:
        data = _load()
        return event_id in data["events"]


def remember_event(event_id: str) -> None:
    event_id = (event_id or "").strip()
    if not event_id:
        return
    with _LOCK:
        data = _load()
        if event_id in data["events"]:
            return
        data["events"].append(event_id)
        if len(data["events"]) > 500:
            del data["events"][:-500]
        _save(data)


def _new_member(now: int) -> dict:
    return {
        "id": "m_" + secrets.token_hex(8),
        "email": "",
        "license_key": "SG-" + secrets.token_urlsafe(12),
        "stripe_customer_id": "",
        "stripe_subscription_id": "",
        "checkout_session_id": "",
        "status": "incomplete",
        "status_source": "",
        "status_event_at": 0,
        "trial_end": None,
        "current_period_end": None,
        "active_session_id": "",
        "session_seen": 0,
        "session_ip": "",
        "session_fp": "",
        "created_at": now,
        "updated_at": now,
    }


def _public_copy(member: dict) -> dict:
    return {
        "id": member.get("id") or "",
        "email": member.get("email") or "",
        "license_key": member.get("license_key") or "",
        "status": member.get("status") or "",
        "trial_end": member.get("trial_end"),
        "current_period_end": member.get("current_period_end"),
        "checkout_session_id": member.get("checkout_session_id") or "",
    }


def _can_replace_status(member: dict, when: int, source: str) -> bool:
    previous = int(member.get("status_event_at") or 0)
    when = int(when)
    if when > previous:
        return True
    if when == previous and source == "subscription" and member.get("status_source") != "subscription":
        return True
    return previous == 0 and when == 0


def _fill_identity(member: dict, *, customer_id: str, subscription_id: str, email: str) -> None:
    if customer_id:
        member["stripe_customer_id"] = customer_id
    if subscription_id:
        member["stripe_subscription_id"] = subscription_id
    cleaned = email.strip().lower()
    if cleaned and "@" in cleaned and len(cleaned) <= 200:
        member["email"] = cleaned


def _expire_if_needed(member: dict, now: int) -> bool:
    status = str(member.get("status") or "")
    if status == "trialing":
        trial_end = member.get("trial_end")
        if trial_end and now >= int(trial_end):
            member["status"] = "trial_expired"
            _clear_session(member)
            member["updated_at"] = now
            return True
    if status == "active":
        period_end = member.get("current_period_end")
        if period_end and now >= int(period_end) + _grace_seconds():
            member["status"] = "expired"
            _clear_session(member)
            member["updated_at"] = now
            return True
    return False


def _entitled(member: dict) -> tuple[bool, str]:
    status = str(member.get("status") or "")
    if status in _ENTITLED:
        return True, ""
    if status == "trial_expired":
        return False, "無料体験は終了しています"
    if status == "expired":
        return False, "利用期限が切れています"
    return False, "利用権限がありません"


def _clear_session(member: dict) -> None:
    member["active_session_id"] = ""
    member["session_seen"] = 0
    member["session_fp"] = ""
    member["session_ip"] = ""


def _locate(data: dict, customer_id: str, subscription_id: str, checkout_id: str, email: str) -> dict | None:
    customer_id = (customer_id or "").strip()
    subscription_id = (subscription_id or "").strip()
    checkout_id = (checkout_id or "").strip()
    email = (email or "").strip().lower()
    members = [item for item in data["members"] if isinstance(item, dict)]
    for member in members:
        if customer_id and member.get("stripe_customer_id") == customer_id:
            return member
    for member in members:
        if subscription_id and member.get("stripe_subscription_id") == subscription_id:
            return member
    for member in members:
        if checkout_id and member.get("checkout_session_id") == checkout_id:
            return member
    if email:
        for member in members:
            if member.get("email") == email and not member.get("stripe_customer_id"):
                return member
    return None


def _find_checkout(data: dict, session_id: str) -> dict | None:
    return _locate(data, "", "", session_id, "")


def _find_license(data: dict, submitted: str) -> dict | None:
    found = None
    for member in data["members"]:
        if not isinstance(member, dict):
            continue
        stored = str(member.get("license_key") or "")
        if stored and _same(stored, submitted):
            found = member
    return found


def _by_id(data: dict, member_id: str) -> dict | None:
    for member in data["members"]:
        if isinstance(member, dict) and member.get("id") == member_id:
            return member
    return None


def _same(left: str, right: str) -> bool:
    return hmac.compare_digest(
        hashlib.sha256(left.encode("utf-8")).digest(),
        hashlib.sha256(right.encode("utf-8")).digest(),
    )


def _load() -> dict:
    path = store_path()
    if not path.is_file():
        return {"events": [], "members": []}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MembersStoreError("会員台帳を読み取れません") from exc
    if not raw.strip():
        return {"events": [], "members": []}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MembersStoreError("会員台帳を読み取れません") from exc
    if not isinstance(data, dict) or not isinstance(data.get("members"), list) or not isinstance(data.get("events"), list):
        raise MembersStoreError("会員台帳を読み取れません")
    return data


def _save(data: dict) -> None:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _idle_seconds() -> int:
    return _int_env("MEMBER_SESSION_IDLE_SEC", _IDLE_DEFAULT)


def _concurrent_seconds() -> int:
    return _int_env("MEMBER_CONCURRENT_WINDOW_SEC", _CONCURRENT_DEFAULT)


def _touch_seconds() -> int:
    return _int_env("MEMBER_TOUCH_INTERVAL_SEC", _TOUCH_DEFAULT)


def _grace_seconds() -> int:
    return _int_env("MEMBER_PERIOD_GRACE_SEC", _GRACE_DEFAULT)


def _int_env(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default
