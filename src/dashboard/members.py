"""ログイン用の会員台帳。

DATABASE_URL があるときは PostgreSQL（失敗時は sqlite3）に置く。
未設定のときは data/members.json を使う。
新規の申し込みと解約は Square の台帳で行う。プロキシ認証と .env はこの台帳に出さない。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
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
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,}$")
_PBKDF2_ROUNDS = 200_000


class MembersStoreError(Exception):
    """台帳が壊れている。中身は呼び出し元で捨て、権限は出さない。"""


def init_member_store() -> str:
    from src.dashboard.member_db import init_member_store as _init_db

    try:
        kind = _init_db()
    except Exception as exc:
        print(f"会員台帳の初期化に失敗したためファイルを使います: {type(exc).__name__}", flush=True)
        kind = "json"
    _seed_viewer_from_env()
    return kind


def _seed_viewer_from_env() -> None:
    """SEED_MEMBER_EMAIL と SEED_MEMBER_PASSWORD があるときだけ、閲覧アカウントを揃える。"""
    email = (os.environ.get("SEED_MEMBER_EMAIL") or "").strip()
    password = os.environ.get("SEED_MEMBER_PASSWORD") or ""
    if not email or not password:
        return
    try:
        result = ensure_viewer_account(email, password)
    except MembersStoreError as exc:
        print(f"閲覧アカウントを揃えられませんでした: {exc}", flush=True)
        return
    print(f"閲覧アカウントを{'作成' if result == 'created' else '更新'}しました", flush=True)


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
            if not member.get("password_hash"):
                _clear_session(member)
            member["updated_at"] = current
            _save(data)
            return "", "", message
        session_id = _issue_session(member, ip, fp, current)
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
        bound = _session_bound(member, session_id, fp, ip, current)
        if member.get("password_hash"):
            if bound == "shared":
                return "shared", "別の端末で利用中です"
            if bound != "ok":
                return "replaced", "ログインが必要です"
            if not allowed:
                _touch_session(data, member, current, changed)
                return "inactive", message
            _touch_session(data, member, current, changed)
            return "ok", ""
        if not allowed:
            if changed or member.get("active_session_id"):
                _clear_session(member)
                member["updated_at"] = current
                _save(data)
            return "revoked", message
        if bound == "shared":
            return "shared", "別の端末で利用中です"
        if bound != "ok":
            return "replaced", "ログインが必要です"
        _touch_session(data, member, current, changed)
        return "ok", ""


def member_session_live(
    member_id: str,
    session_id: str,
    fp: str = "",
    ip: str = "",
    now: int | None = None,
) -> bool:
    state, _message = authorize_member_request(member_id, session_id, fp, ip=ip, now=now)
    return state in {"ok", "inactive"}


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


def register_account(email: str, password: str) -> tuple[str, str]:
    """メールとパスワードで会員を作る。戻り値は (会員ID, エラー文)。"""
    cleaned = _clean_email(email)
    if not cleaned:
        return "", "メールアドレスの形式を確認してください"
    if not _acceptable_password(password):
        return "", "パスワードは8文字以上128文字以内にしてください"
    current = int(time.time())
    with _LOCK:
        try:
            data = _load()
        except MembersStoreError:
            return "", "処理に失敗しました。しばらくしてから再度お試しください。"
        if _find_email(data, cleaned) is not None:
            return "", "このメールアドレスは登録済みです"
        member = _new_member(current)
        member["email"] = cleaned
        member["password_hash"] = hash_password(password)
        member["status"] = "incomplete"
        member["is_active"] = False
        data["members"].append(member)
        try:
            _save(data)
        except MembersStoreError:
            return "", "処理に失敗しました。しばらくしてから再度お試しください。"
        return str(member["id"]), ""


def ensure_viewer_account(email: str, password: str) -> str:
    """閲覧できる会員を用意する。同じメールがあればパスワードと権限を上書きする。

    戻り値は created または updated。パスワードは PBKDF2 のハッシュだけを保存する。
    """
    cleaned = _clean_email(email)
    if not cleaned:
        raise MembersStoreError("メールアドレスの形式を確認してください")
    if not _acceptable_password(password):
        raise MembersStoreError("パスワードは8文字以上128文字以内にしてください")
    current = int(time.time())
    with _LOCK:
        try:
            data = _load()
        except MembersStoreError:
            raise
        member = _find_email(data, cleaned)
        created = member is None
        if member is None:
            member = _new_member(current)
            member["email"] = cleaned
            data["members"].append(member)
        member["password_hash"] = hash_password(password)
        member["is_active"] = True
        member["status"] = "active"
        member["status_source"] = "operator_seed"
        member["status_event_at"] = current
        member["current_period_end"] = None
        member["trial_end"] = None
        member["updated_at"] = current
        try:
            _save(data)
        except MembersStoreError:
            raise
        return "created" if created else "updated"


def open_password_session(
    email: str,
    password: str,
    ip: str,
    fp: str,
    now: int | None = None,
) -> tuple[str, str, str]:
    """メールとパスワードで入室する。未課金でもセッションは発行する。

    戻り値は (会員ID, セッションID, エラー文)。認証失敗はすべて空。
    新しいログインは、それまでのセッションを無効にする。
    """
    cleaned = _clean_email(email)
    if not cleaned or not _acceptable_password(password):
        hash_password(password or "invalid-password")
        return "", "", ""
    current = int(time.time() if now is None else now)
    with _LOCK:
        try:
            data = _load()
        except MembersStoreError:
            return "", "", "処理に失敗しました。しばらくしてから再度お試しください。"
        member = _find_email(data, cleaned)
        stored = str(member.get("password_hash") or "") if member else ""
        if member is None or not stored:
            password_matches(password, _dummy_password_hash())
            return "", "", ""
        if not password_matches(password, stored):
            return "", "", ""
        _expire_if_needed(member, current)
        session_id = _issue_session(member, ip, fp, current)
        _save(data)
        return str(member["id"]), session_id, ""


def issue_member_session(member_id: str, ip: str, fp: str, now: int | None = None) -> str:
    """登録直後など、本人確認済みの会員にセッションを1つ出す。"""
    current = int(time.time() if now is None else now)
    with _LOCK:
        try:
            data = _load()
        except MembersStoreError:
            return ""
        member = _by_id(data, member_id)
        if member is None:
            return ""
        session_id = _issue_session(member, ip, fp, current)
        _save(data)
        return session_id


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
    member_id: str = "",
    name: str = "",
) -> dict | None:
    """支払い済みの Checkout を台帳へ反映する。同じ決済の再送では状態を巻き戻さない。"""
    session_id = (session_id or "").strip()
    if not session_id:
        return None
    current = int(time.time())
    with _LOCK:
        data = _load()
        member = _by_id(data, member_id) if member_id else None
        if member is None:
            member = _locate(data, customer_id, subscription_id, session_id, email)
        created = member is None
        if member is None:
            member = _new_member(current)
            data["members"].append(member)
        _fill_identity(member, customer_id=customer_id, subscription_id=subscription_id, email=email, name=name)
        member["checkout_session_id"] = session_id
        if _can_replace_status(member, when, "checkout"):
            member["status"] = "active"
            member["is_active"] = True
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
    member_id: str = "",
    email: str = "",
    name: str = "",
    trial_start: int | None = None,
    plan_name: str = "",
    started_at: int | None = None,
) -> None:
    customer_id = (customer_id or "").strip()
    subscription_id = (subscription_id or "").strip()
    member_id = (member_id or "").strip()
    if not customer_id and not subscription_id and not member_id:
        return
    current = int(time.time())
    with _LOCK:
        data = _load()
        member = _by_id(data, member_id) if member_id else None
        if member is None:
            member = _locate(data, customer_id, subscription_id, "", email)
        created = member is None
        if member is None:
            if deleted:
                return
            member = _new_member(current)
            data["members"].append(member)
        stored_sub = str(member.get("stripe_subscription_id") or "")
        if deleted and stored_sub and subscription_id and stored_sub != subscription_id:
            return
        _fill_identity(member, customer_id=customer_id, subscription_id=subscription_id, email=email, name=name)
        replaced = _can_replace_status(member, when, "subscription")
        if replaced:
            member["status"] = "canceled" if deleted else (status or "incomplete").strip().lower()
            member["is_active"] = (not deleted) and member["status"] in _ENTITLED
            member["status_source"] = "subscription"
            member["status_event_at"] = int(when)
            if not deleted:
                member["trial_end"] = trial_end
                member["current_period_end"] = period_end
                if trial_start is not None:
                    member["trial_start"] = trial_start
            if not member["is_active"] and not member.get("password_hash"):
                _clear_session(member)
        if trial_start is not None and not member.get("trial_start"):
            member["trial_start"] = trial_start
        cleaned_plan = _clean_label(plan_name)
        if cleaned_plan and (replaced or not member.get("plan_name")):
            member["plan_name"] = cleaned_plan
        if started_at and not member.get("subscription_started_at"):
            member["subscription_started_at"] = int(started_at)
        if created and started_at:
            member["created_at"] = int(started_at)
        member["updated_at"] = current
        _save(data)


def contract_records() -> list[dict]:
    """管理画面用。ライセンスキーとセッション情報は含めない。"""
    with _LOCK:
        data = _load()
    rows: list[dict] = []
    for member in data.get("members") or []:
        if not isinstance(member, dict):
            continue
        rows.append(
            {
                "id": str(member.get("id") or ""),
                "email": str(member.get("email") or ""),
                "name": str(member.get("name") or ""),
                "stripe_customer_id": str(member.get("stripe_customer_id") or ""),
                "stripe_subscription_id": str(member.get("stripe_subscription_id") or ""),
                "status": str(member.get("status") or ""),
                "trial_start": _optional_stamp(member.get("trial_start")),
                "trial_end": _optional_stamp(member.get("trial_end")),
                "current_period_end": _optional_stamp(member.get("current_period_end")),
                "plan_name": str(member.get("plan_name") or ""),
                "subscription_started_at": _optional_stamp(member.get("subscription_started_at")),
                "created_at": _optional_stamp(member.get("created_at")) or 0,
            }
        )
    return rows


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
        "name": "",
        "password_hash": "",
        "license_key": "SG-" + secrets.token_urlsafe(12),
        "stripe_customer_id": "",
        "stripe_subscription_id": "",
        "checkout_session_id": "",
        "status": "incomplete",
        "is_active": False,
        "status_source": "",
        "status_event_at": 0,
        "trial_start": None,
        "trial_end": None,
        "current_period_end": None,
        "plan_name": "",
        "subscription_started_at": None,
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
        "is_active": bool(member.get("is_active")),
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


def _fill_identity(member: dict, *, customer_id: str, subscription_id: str, email: str, name: str = "") -> None:
    if customer_id:
        member["stripe_customer_id"] = customer_id
    if subscription_id:
        member["stripe_subscription_id"] = subscription_id
    cleaned = email.strip().lower()
    if cleaned and "@" in cleaned and len(cleaned) <= 200:
        member["email"] = cleaned
    cleaned_name = _clean_label(name)
    if cleaned_name:
        member["name"] = cleaned_name


def _expire_if_needed(member: dict, now: int) -> bool:
    status = str(member.get("status") or "")
    if status == "trialing":
        trial_end = member.get("trial_end")
        if trial_end and now >= int(trial_end):
            _mark_inactive(member, "trial_expired", now)
            return True
    if status == "active":
        period_end = member.get("current_period_end")
        if period_end and now >= int(period_end) + _grace_seconds():
            _mark_inactive(member, "expired", now)
            return True
    return False


def _entitled(member: dict) -> tuple[bool, str]:
    status = str(member.get("status") or "")
    if bool(member.get("is_active")) and status in _ENTITLED:
        return True, ""
    if status == "trial_expired":
        return False, "無料体験は終了しています"
    if status == "expired":
        return False, "利用期限が切れています"
    return False, "利用権限がありません"


def _mark_inactive(member: dict, status: str, now: int) -> None:
    member["status"] = status
    member["is_active"] = False
    if not member.get("password_hash"):
        _clear_session(member)
    member["updated_at"] = now


def _issue_session(member: dict, ip: str, fp: str, current: int) -> str:
    """今のセッションを置き換える。以前の端末は次の通信でログアウトになる。"""
    session_id = secrets.token_hex(16)
    member["active_session_id"] = session_id
    member["session_seen"] = current
    member["session_ip"] = (ip or "")[:80]
    member["session_fp"] = fp or ""
    member["updated_at"] = current
    return session_id


def _session_bound(member: dict, session_id: str, fp: str, ip: str, current: int) -> str:
    """今のセッションそのものか。ok / shared / replaced。"""
    if str(member.get("active_session_id") or "") != session_id:
        return "replaced"
    stored_fp = str(member.get("session_fp") or "")
    stored_ip = str(member.get("session_ip") or "")
    seen = int(member.get("session_seen") or 0)
    if stored_fp and fp and stored_fp != fp:
        if current - seen < _idle_seconds():
            return "shared"
        return "replaced"
    if stored_ip and ip and stored_ip != ip and current - seen < _concurrent_seconds():
        return "shared"
    return "ok"


def _touch_session(data: dict, member: dict, current: int, changed: bool) -> None:
    seen = int(member.get("session_seen") or 0)
    if current - seen >= _touch_seconds():
        member["session_seen"] = current
        member["updated_at"] = current
        _save(data)
    elif changed:
        _save(data)


_DUMMY_PASSWORD_HASH = ""


def _dummy_password_hash() -> str:
    global _DUMMY_PASSWORD_HASH
    if not _DUMMY_PASSWORD_HASH:
        _DUMMY_PASSWORD_HASH = hash_password("dummy-password-value")
    return _DUMMY_PASSWORD_HASH


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _normalize_loaded(data: dict) -> None:
    for member in data.get("members") or []:
        if not isinstance(member, dict):
            continue
        member.setdefault("password_hash", "")
        member.setdefault("name", "")
        member.setdefault("plan_name", "")
        member.setdefault("trial_start", None)
        member.setdefault("subscription_started_at", None)
        if member.get("is_active") is None and "is_active" not in member:
            member["is_active"] = str(member.get("status") or "") in _ENTITLED
        else:
            member["is_active"] = _as_bool(member.get("is_active"))


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8")[:128], bytes.fromhex(salt), _PBKDF2_ROUNDS)
    return f"pbkdf2${_PBKDF2_ROUNDS}${salt}${digest.hex()}"


def password_matches(password: str, stored: str) -> bool:
    try:
        scheme, rounds_raw, salt, digest = (stored or "").split("$", 3)
        if scheme != "pbkdf2":
            return False
        rounds = int(rounds_raw)
        if rounds < 50_000 or rounds > 1_000_000:
            return False
        check = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8")[:128], bytes.fromhex(salt), rounds)
        return hmac.compare_digest(check, bytes.fromhex(digest))
    except (TypeError, ValueError):
        return False


def _acceptable_password(password: str) -> bool:
    if not isinstance(password, str):
        return False
    if len(password) < 8 or len(password) > 128:
        return False
    return not any(ch in password for ch in "\r\n\x00")


def _clean_label(value: str) -> str:
    text = " ".join(str(value or "").split())
    if not text or any(ord(ch) < 32 for ch in text):
        return ""
    return text[:80]


def _optional_stamp(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return number


def _clean_email(value: str) -> str:
    email = (value or "").strip().lower()
    if len(email) > 200 or not _EMAIL_RE.fullmatch(email):
        return ""
    return email


def _find_email(data: dict, email: str) -> dict | None:
    for member in data["members"]:
        if isinstance(member, dict) and str(member.get("email") or "") == email:
            return member
    return None


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
    from src.dashboard.member_db import MemberDbError
    from src.dashboard.member_db import load_members
    from src.dashboard.member_db import uses_database

    if uses_database():
        try:
            data = load_members()
        except MemberDbError as exc:
            raise MembersStoreError("会員台帳を読み取れません") from exc
        _normalize_loaded(data)
        return data
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
    _normalize_loaded(data)
    return data


def _save(data: dict) -> None:
    from src.dashboard.member_db import MemberDbError
    from src.dashboard.member_db import save_members
    from src.dashboard.member_db import uses_database

    if uses_database():
        try:
            save_members(data)
        except MemberDbError as exc:
            raise MembersStoreError("会員台帳を保存できません") from exc
        return
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
