"""X API v2 への画像付き投稿。認証は OAuth 1.0a。

メディアの受け取りは v2 の media/upload、本文は v2 の tweets。
キーそのものはログに出さない。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qsl
from urllib.parse import quote
from urllib.parse import urlsplit

import httpx

from src.analyzer.judgment import PROJECT_ROOT

UPLOAD_URL = "https://api.x.com/2/media/upload"
TWEET_URL = "https://api.twitter.com/2/tweets"
ME_URL = "https://api.twitter.com/2/users/me"


class XApiError(RuntimeError):
    """投稿または疎通確認が失敗した。"""


def should_publish(*, ok: bool, dry_run: bool, image_ready: bool, text_only: bool = False) -> bool:
    """ok が真で、ドライランでないときだけ送る。朝の宣言は画像なしでも送る。"""
    return bool(ok) and not dry_run and (bool(text_only) or image_ready)


def load_x_env() -> None:
    path = PROJECT_ROOT / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and not (os.environ.get(key) or "").strip():
            os.environ[key] = value


def credentials() -> dict[str, str]:
    load_x_env()
    mapping = {
        "consumer_key": "X_API_KEY",
        "consumer_secret": "X_API_SECRET",
        "access_token": "X_ACCESS_TOKEN",
        "access_secret": "X_ACCESS_TOKEN_SECRET",
    }
    values = {name: (os.environ.get(env) or "").strip() for name, env in mapping.items()}
    missing = [mapping[name] for name, value in values.items() if not value]
    if missing:
        names = "、".join(missing)
        raise XApiError(f"X APIのキーが .env にありません: {names}")
    return values


def assert_write_access(client: httpx.Client | None = None) -> None:
    """空の本文で投稿口の権限だけを確かめる。投稿は作らない。"""
    creds = credentials()
    own_client = client is None
    http = client or httpx.Client(timeout=20)
    try:
        response = http.post(
            TWEET_URL,
            headers={
                "Authorization": _authorization("POST", TWEET_URL, {}, creds),
                "Content-Type": "application/json",
            },
            content=b"{}",
        )
    finally:
        if own_client:
            http.close()
    if response.status_code == 400:
        return
    if response.status_code == 403 and "oauth1" in (response.text or "").lower():
        raise XApiError(
            "Xアプリの権限が投稿に足りません。"
            "developer portal で App permissions を Read and Write にし、"
            "Access Token と Access Token Secret を再発行して .env を更新してください。"
        )
    _raise_for_status(response, "投稿権限の確認")
    if response.status_code < 300:
        raise XApiError("投稿権限の確認で空の投稿が受け付けられました。内容を確認してください。")


def verify_account(client: httpx.Client | None = None) -> dict[str, str]:
    """投稿せず、認証ユーザーだけを確認する。"""
    creds = credentials()
    own_client = client is None
    http = client or httpx.Client(timeout=20)
    try:
        response = http.get(ME_URL, headers={"Authorization": _authorization("GET", ME_URL, {}, creds)})
        _raise_for_status(response, "疎通確認")
        body = response.json()
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict) or not data.get("id"):
            raise XApiError("Xの疎通確認でユーザーを取得できませんでした")
        return {"id": str(data.get("id") or ""), "username": str(data.get("username") or "")}
    finally:
        if own_client:
            http.close()


def publish_tweet(
    text: str,
    image: bytes | None = None,
    *,
    images: list[bytes] | None = None,
    client: httpx.Client | None = None,
) -> dict[str, str]:
    """ポストを1件送る。画像は最大4枚まで。戻り値は投稿ID。"""
    if not text.strip():
        raise XApiError("投稿文面が空です")
    pictures = _tweet_images(image, images)
    creds = credentials()
    own_client = client is None
    http = client or httpx.Client(timeout=40)
    try:
        payload: dict = {"text": text}
        if pictures:
            media_ids = [_upload_media(http, creds, blob) for blob in pictures]
            payload["media"] = {"media_ids": media_ids}
        response = http.post(
            TWEET_URL,
            headers={
                "Authorization": _authorization("POST", TWEET_URL, {}, creds),
                "Content-Type": "application/json",
            },
            content=json.dumps(payload).encode("utf-8"),
        )
        _raise_for_status(response, "投稿")
        body = response.json()
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict) or not data.get("id"):
            raise XApiError("Xは投稿を受け付けましたが、投稿IDがありません")
        return {"id": str(data.get("id") or ""), "text": str(data.get("text") or text)}
    finally:
        if own_client:
            http.close()


def publish_reply(text: str, in_reply_to_tweet_id: str, client: httpx.Client | None = None) -> dict[str, str]:
    """画像なしの返信を、指定した投稿の直下に付ける。"""
    if not text.strip():
        raise XApiError("返信文面が空です")
    if not str(in_reply_to_tweet_id).strip():
        raise XApiError("返信先の投稿IDがありません")
    creds = credentials()
    own_client = client is None
    http = client or httpx.Client(timeout=40)
    try:
        response = http.post(
            TWEET_URL,
            headers={
                "Authorization": _authorization("POST", TWEET_URL, {}, creds),
                "Content-Type": "application/json",
            },
            content=json.dumps(
                {
                    "text": text,
                    "reply": {"in_reply_to_tweet_id": str(in_reply_to_tweet_id)},
                }
            ).encode("utf-8"),
        )
        _raise_for_status(response, "返信")
        body = response.json()
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict) or not data.get("id"):
            raise XApiError("Xは返信を受け付けましたが、投稿IDがありません")
        return {"id": str(data.get("id") or ""), "text": str(data.get("text") or text)}
    finally:
        if own_client:
            http.close()


def _tweet_images(image: bytes | None, images: list[bytes] | None) -> list[bytes]:
    found: list[bytes] = []
    if images:
        found.extend(item for item in images if item)
    elif image:
        found.append(image)
    if len(found) > 4:
        raise XApiError("Xの1投稿に付けられる画像は4枚までです")
    for blob in found:
        if not blob:
            raise XApiError("投稿画像が空です")
    return found


def _upload_media(http: httpx.Client, creds: dict[str, str], image: bytes) -> str:
    response = http.post(
        UPLOAD_URL,
        headers={"Authorization": _authorization("POST", UPLOAD_URL, {}, creds)},
        data={"media_category": "tweet_image"},
        files={"media": ("summary.png", image, "image/png")},
    )
    _raise_for_status(response, "画像アップロード")
    body = response.json()
    media_id = ""
    data = body.get("data") if isinstance(body, dict) else None
    if isinstance(data, dict):
        media_id = str(data.get("id") or "")
    if not media_id and isinstance(body, dict):
        media_id = str(body.get("media_id_string") or body.get("media_id") or "")
    if not media_id:
        raise XApiError("Xの画像アップロードで media_id を取得できませんでした")
    return media_id


def _authorization(method: str, url: str, extra: dict[str, str], creds: dict[str, str]) -> str:
    params = {
        "oauth_consumer_key": creds["consumer_key"],
        "oauth_nonce": secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_token": creds["access_token"],
        "oauth_version": "1.0",
    }
    params.update(extra)
    params["oauth_signature"] = _signature(method, url, params, creds)
    parts = [f'{_pct(key)}="{_pct(value)}"' for key, value in sorted(params.items())]
    return "OAuth " + ", ".join(parts)


def _signature(method: str, url: str, params: dict[str, str], creds: dict[str, str]) -> str:
    parsed = urlsplit(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    items = {str(key): str(value) for key, value in params.items()}
    items.update(parse_qsl(parsed.query, keep_blank_values=True))
    encoded = "&".join(f"{_pct(key)}={_pct(items[key])}" for key in sorted(items))
    base = "&".join([method.upper(), _pct(base_url), _pct(encoded)])
    key = f"{_pct(creds['consumer_secret'])}&{_pct(creds['access_secret'])}"
    digest = hmac.new(key.encode("utf-8"), base.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def _pct(value: str) -> str:
    return quote(str(value), safe="")


def _raise_for_status(response: httpx.Response, action: str) -> None:
    if response.status_code < 400:
        return
    detail = ""
    try:
        body = response.json()
    except (json.JSONDecodeError, ValueError):
        body = None
    if isinstance(body, dict):
        errors = body.get("errors") or body.get("detail") or body.get("title") or body.get("error")
        if errors:
            detail = str(errors)[:240]
    if not detail:
        detail = (response.text or "")[:240]
    raise XApiError(f"Xへの{action}に失敗しました ({response.status_code}) {detail}")


def image_bytes(path: Path) -> bytes:
    return path.read_bytes()
