"""Square の定期購入から会員台帳を戻す。

Render の sqlite は再デプロイで消える。Square 側に残っている契約と、
顧客メモに書いた台帳の写しから会員を戻す。
"""

from __future__ import annotations

import json
import os
import threading
import time
from urllib.request import Request
from urllib.request import urlopen

from src.dashboard.square_members import describe_backend
from src.dashboard.square_members import restore_members_from_square

_SQUARE_VERSION = "2026-09-16"
_NOTE_PREFIX = "SURO1:"
_SYNCED = False
_SYNC_LOCK = threading.Lock()
_KEEPER_STARTED = False
_NOTE_KEYS = (
    "status",
    "registered_at",
    "trial_end",
    "paid_at",
    "status_source",
    "email",
    "line_user_id",
    "line_name",
    "square_subscription_id",
)


def needs_square_restore() -> bool:
    """本番の一時ディスクだけ、Square を正本にして台帳を埋める。"""
    if describe_backend() == "postgres":
        return False
    if (os.environ.get("RENDER") or "").strip().lower() not in {"1", "true", "yes"}:
        return False
    return bool((os.environ.get("SQUARE_ACCESS_TOKEN") or "").strip())


def ensure_square_members() -> bool:
    """Square を一度読めていれば True。読めないあいだは空の台帳を確定させない。"""
    global _SYNCED
    if not needs_square_restore():
        return True
    if _SYNCED:
        return True
    with _SYNC_LOCK:
        if _SYNCED:
            return True
        try:
            pull_square_members()
        except Exception as exc:
            print(f"Squareの会員復元に失敗しました: {type(exc).__name__}", flush=True)
            return False
        _SYNCED = True
        return True


def keep_square_members() -> None:
    """起動後も定期的に Square から取り直す。通知を取りこぼしても台帳が空のまま残らない。"""
    global _KEEPER_STARTED
    if _KEEPER_STARTED or not needs_square_restore():
        return
    _KEEPER_STARTED = True

    def _loop() -> None:
        global _SYNCED
        while True:
            time.sleep(300)
            if not needs_square_restore():
                continue
            with _SYNC_LOCK:
                try:
                    pull_square_members()
                    _SYNCED = True
                except Exception as exc:
                    print(f"Squareの会員復元に失敗しました: {type(exc).__name__}", flush=True)

    threading.Thread(target=_loop, name="square-member-sync", daemon=True).start()


def remember_member(member: dict) -> None:
    """会員の状態を Square の顧客メモへ書く。サーバーのファイルが消えても残る。"""
    if not needs_square_restore():
        return
    customer_id = str(member.get("square_customer_id") or "").strip()
    token = (os.environ.get("SQUARE_ACCESS_TOKEN") or "").strip()
    if not customer_id or not token:
        return
    note = _NOTE_PREFIX + json.dumps(_note_payload(member), ensure_ascii=False, separators=(",", ":"))
    threading.Thread(
        target=_write_note,
        args=(token, customer_id, note),
        name="square-member-note",
        daemon=True,
    ).start()


def pull_square_members() -> int:
    token = (os.environ.get("SQUARE_ACCESS_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("missing square token")
    subscriptions = _search_subscriptions(token)
    try:
        customers = _search_customers(token)
    except Exception as exc:
        print(f"Squareの顧客一覧は読めませんでした: {type(exc).__name__}", flush=True)
        customers = {}
    missing = [
        str(item.get("customer_id") or "")
        for item in subscriptions
        if str(item.get("customer_id") or "") not in customers
    ]
    if missing:
        customers.update(_retrieve_customers(token, missing))
    records = []
    seen: set[str] = set()
    for subscription in subscriptions:
        customer_id = str(subscription.get("customer_id") or "")
        seen.add(customer_id)
        records.append(_record(customers.get(customer_id) or {}, subscription))
    for customer_id, customer in customers.items():
        if customer_id in seen:
            continue
        note = str(customer.get("note") or "")
        if not str(customer.get("reference_id") or "") and not note.startswith(_NOTE_PREFIX):
            continue
        records.append(_record(customer, None))
    restored = restore_members_from_square(records)
    print(f"Squareから会員を復元しました: {restored}人", flush=True)
    return restored


def _note_payload(member: dict) -> dict:
    payload = {}
    for key in _NOTE_KEYS:
        value = member.get(key)
        if value in (None, ""):
            continue
        payload[key] = value
    return payload


def _record(customer: dict, subscription: dict | None) -> dict:
    subscription = subscription or {}
    return {
        "email": customer.get("email_address") or "",
        "line_user_id": customer.get("reference_id") or "",
        "line_name": customer.get("given_name") or "",
        "customer_id": customer.get("id") or subscription.get("customer_id") or "",
        "subscription_id": subscription.get("id") or "",
        "status": subscription.get("status") or "",
        "start_date": subscription.get("start_date") or "",
        "note": customer.get("note") or "",
    }


def _write_note(token: str, customer_id: str, note: str) -> None:
    try:
        current = _square_json(token, "GET", f"/v2/customers/{customer_id}")
        customer = current.get("customer") if isinstance(current.get("customer"), dict) else {}
        if str(customer.get("note") or "") == note:
            return
        _square_json(
            token,
            "PUT",
            f"/v2/customers/{customer_id}",
            {"note": note, "version": customer.get("version")},
        )
    except Exception as exc:
        print(f"会員メモをSquareへ保存できませんでした: {type(exc).__name__}", flush=True)


def _square_base() -> str:
    application_id = (os.environ.get("SQUARE_APPLICATION_ID") or "").strip()
    requested = (os.environ.get("SQUARE_ENVIRONMENT") or "sandbox").strip().lower()
    if application_id.startswith("sandbox-"):
        return "https://connect.squareupsandbox.com"
    if application_id.startswith("sq0idp-") or requested == "production":
        return "https://connect.squareup.com"
    return "https://connect.squareupsandbox.com"


def _square_json(token: str, method: str, path: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        _square_base() + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Square-Version": _SQUARE_VERSION,
            "Content-Type": "application/json",
        },
    )
    with urlopen(request, timeout=20) as response:
        body = json.loads(response.read().decode("utf-8"))
    if not isinstance(body, dict):
        raise RuntimeError("square response")
    return body


def _search_subscriptions(token: str) -> list[dict]:
    found: list[dict] = []
    cursor = ""
    for _ in range(10):
        payload: dict = {"limit": 100}
        if cursor:
            payload["cursor"] = cursor
        page = _square_json(token, "POST", "/v2/subscriptions/search", payload)
        for item in page.get("subscriptions") or []:
            if isinstance(item, dict) and item.get("id"):
                found.append(item)
        cursor = str(page.get("cursor") or "")
        if not cursor:
            break
    return found


def _search_customers(token: str) -> dict[str, dict]:
    found: dict[str, dict] = {}
    cursor = ""
    for _ in range(10):
        payload: dict = {
            "limit": 100,
            "query": {"filter": {"created_at": {"start_at": "2020-01-01T00:00:00Z"}}},
        }
        if cursor:
            payload["cursor"] = cursor
        page = _square_json(token, "POST", "/v2/customers/search", payload)
        for item in page.get("customers") or []:
            if isinstance(item, dict) and item.get("id"):
                found[str(item["id"])] = item
        cursor = str(page.get("cursor") or "")
        if not cursor:
            break
    return found


def _retrieve_customers(token: str, customer_ids: list[str]) -> dict[str, dict]:
    unique = []
    seen: set[str] = set()
    for customer_id in customer_ids:
        if customer_id and customer_id not in seen:
            seen.add(customer_id)
            unique.append(customer_id)
    found: dict[str, dict] = {}
    for start in range(0, len(unique), 100):
        chunk = unique[start : start + 100]
        page = _square_json(token, "POST", "/v2/customers/bulk-retrieve", {"customer_ids": chunk})
        responses = page.get("responses") or {}
        if isinstance(responses, dict):
            for customer_id, item in responses.items():
                customer = item.get("customer") if isinstance(item, dict) else None
                if isinstance(customer, dict):
                    found[str(customer_id)] = customer
    return found
