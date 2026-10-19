"""Square の定期購入から会員台帳を戻す。

Render の sqlite は再デプロイで消える。Square 側に残っている契約を台帳へ書き戻す。
"""

from __future__ import annotations

import json
import os
from urllib.request import Request
from urllib.request import urlopen

from src.dashboard.square_members import restore_members_from_square

_SQUARE_VERSION = "2026-09-16"


def pull_square_members() -> int:
    token = (os.environ.get("SQUARE_ACCESS_TOKEN") or "").strip()
    if not token:
        print("Squareの会員復元をスキップしました。アクセストークンがありません。", flush=True)
        return 0
    try:
        subscriptions = _search_subscriptions(token)
        customers = _retrieve_customers(token, [str(item.get("customer_id") or "") for item in subscriptions])
    except Exception as exc:
        print(f"Squareの会員復元に失敗しました: {type(exc).__name__}", flush=True)
        return 0
    records = []
    for subscription in subscriptions:
        customer_id = str(subscription.get("customer_id") or "")
        customer = customers.get(customer_id) or {}
        records.append(
            {
                "email": customer.get("email_address") or "",
                "line_user_id": customer.get("reference_id") or "",
                "line_name": customer.get("given_name") or "",
                "customer_id": customer_id,
                "subscription_id": subscription.get("id") or "",
                "status": subscription.get("status") or "",
                "start_date": subscription.get("start_date") or "",
            }
        )
    restored = restore_members_from_square(records)
    print(f"Squareから会員を復元しました: {restored}人", flush=True)
    return restored


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
