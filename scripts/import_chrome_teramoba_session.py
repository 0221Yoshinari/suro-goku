#!/usr/bin/env python3
"""普段の Google Chrome に保存されたテラモバ2の Cookie を取得用スナップショットへ写す。

Playwright は起動しない。事前に標準 Chrome で台詳細の規約同意と Turnstile を通しておく。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scraper.browser import detect_chrome_version, teramoba_browser_profile
from src.scraper.halls import halls_for_source
from src.scraper.teramoba import STORAGE_STATE_PATH, storage_state_path

CHROME_ROOT = Path.home() / "Library/Application Support/Google/Chrome"
HOST_NEEDLES = ("teramoba2.com",)
CONSENT_PREFIXES = ("agreeServiceTerm[", "captchaProtectionKey[")
AES_IV = b" " * 16


def chrome_user_agent() -> str:
    major, full = detect_chrome_version()
    version = full if full else f"{major}.0.0.0"
    return (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{version} Safari/537.36"
    )


def chrome_safe_storage_key() -> bytes:
    password = subprocess.check_output(
        ["security", "find-generic-password", "-w", "-s", "Chrome Safe Storage", "-a", "Chrome"],
        text=True,
    ).strip()
    if not password:
        raise SystemExit("Chrome Safe Storage のキーを Keychain から読めませんでした")
    return hashlib.pbkdf2_hmac("sha1", password.encode("utf-8"), b"saltysalt", 1003, dklen=16)


def decrypt_cookie(encrypted: bytes, key: bytes) -> str:
    if not encrypted:
        return ""
    if encrypted.startswith(b"v10") or encrypted.startswith(b"v11"):
        blob = encrypted[3:]
    else:
        return ""
    result = subprocess.run(
        [
            "openssl",
            "enc",
            "-d",
            "-aes-128-cbc",
            "-K",
            key.hex(),
            "-iv",
            AES_IV.hex(),
            "-nopad",
        ],
        input=blob,
        capture_output=True,
        check=False,
    )
    plain = result.stdout
    if not plain:
        return ""
    pad = plain[-1]
    if isinstance(pad, str):
        pad = ord(pad)
    if 1 <= pad <= 16 and plain.endswith(bytes([pad]) * pad):
        plain = plain[:-pad]
    # Chrome 130 以降は平文の先頭 32 バイトがホストの検証用ハッシュ。
    if len(plain) > 32:
        plain = plain[32:]
    try:
        return plain.decode("utf-8")
    except UnicodeDecodeError:
        return ""


def cookie_dbs() -> list[Path]:
    if not CHROME_ROOT.is_dir():
        return []
    found: list[Path] = []
    for path in sorted(CHROME_ROOT.glob("*/Cookies")):
        if path.is_file() and "System Profile" not in str(path):
            found.append(path)
    return found


def chrome_epoch_to_unix(value: int) -> float:
    if value <= 0:
        return -1
    return value / 1_000_000 - 11_644_473_600


def read_profile_cookies(db_path: Path, key: bytes) -> list[dict]:
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "Cookies"
        shutil.copy2(db_path, copy)
        for suffix in ("-wal", "-journal", "-shm"):
            sidecar = Path(str(db_path) + suffix)
            if sidecar.is_file():
                shutil.copy2(sidecar, Path(str(copy) + suffix))
        connection = sqlite3.connect(copy)
        try:
            rows = connection.execute(
                """
                SELECT host_key, name, value, encrypted_value, path, expires_utc, is_secure, is_httponly, samesite
                FROM cookies
                """
            ).fetchall()
        finally:
            connection.close()
    cookies: list[dict] = []
    for host, name, value, encrypted, path, expires, secure, httponly, samesite in rows:
        host = str(host or "")
        name = str(name or "")
        consent = name.startswith(CONSENT_PREFIXES)
        if not consent and not any(needle in host for needle in HOST_NEEDLES):
            continue
        raw = str(value or "")
        if not raw and encrypted:
            raw = decrypt_cookie(bytes(encrypted), key)
        if not raw:
            if consent:
                print(f"  復号できない同意Cookieをスキップ: {name} host={host}", flush=True)
            continue
        same = {0: "None", 1: "Lax", 2: "Strict"}.get(int(samesite or 0), "Lax")
        item = {
            "name": str(name),
            "value": raw,
            "domain": host,
            "path": str(path or "/"),
            "expires": chrome_epoch_to_unix(int(expires or 0)),
            "httpOnly": bool(httponly),
            "secure": bool(secure),
            "sameSite": same,
        }
        cookies.append(item)
    return cookies


def write_snapshot(cookies: list[dict], user_agent: str) -> tuple[Path, Path]:
    payload = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "source": "google-chrome-local",
        "user_agent": user_agent,
        "cookies": cookies,
        "origins": [],
    }
    state_path = storage_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    profile = teramoba_browser_profile()
    profile.mkdir(parents=True, exist_ok=True)
    snapshot = profile / "session_snapshot.json"
    snapshot.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    legacy = STORAGE_STATE_PATH
    if legacy.resolve() != state_path.resolve():
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return state_path, snapshot


def _keep_missing_consent(merged: dict[tuple[str, str, str], dict]) -> int:
    """新しい Chrome 読み取りに無い同意 Cookie は、前回保存分を残す。"""
    path = storage_state_path()
    if not path.is_file():
        return 0
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    have = {str(item.get("name") or "") for item in merged.values()}
    kept = 0
    for item in previous.get("cookies") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if not name.startswith(CONSENT_PREFIXES) or name in have:
            continue
        domain = str(item.get("domain") or "")
        cookie_path = str(item.get("path") or "/")
        merged[(domain, name, cookie_path)] = item
        have.add(name)
        kept += 1
    return kept


def _hall_id_from_cookie(name: str, prefix: str) -> int | None:
    if not name.startswith(prefix) or not name.endswith("]"):
        return None
    raw = name[len(prefix) : -1]
    return int(raw) if raw.isdigit() else None


def apply_global_consent(cookies: list[dict]) -> list[dict]:
    """代表店舗の同意を、設定済み全店舗の agreeServiceTerm として同じセッションに載せる。

    captchaProtectionKey の値は店舗 ID に紐づく。別の店舗 ID へコピーすると
    サーバが認証を拒否するため、Chrome に入っているキーだけを残す。
    """
    halls = halls_for_source("teramoba")
    if not halls:
        return cookies
    template = next(
        (
            item
            for item in cookies
            if str(item.get("name") or "").startswith("agreeServiceTerm[")
            and "teramoba2.com" in str(item.get("domain") or "")
        ),
        None,
    )
    domain = str((template or {}).get("domain") or "tamaya.pt.teramoba2.com")
    by_key = {(str(item.get("domain")), str(item.get("name")), str(item.get("path") or "/")): item for item in cookies}
    added = 0
    for hall in halls:
        hid = int((hall.source_config or {}).get("hall_id") or 0)
        if not hid:
            continue
        name = f"agreeServiceTerm[{hid}]"
        key = (domain, name, "/")
        current = by_key.get(key)
        if current and str(current.get("value") or "") == "true":
            continue
        by_key[key] = {
            "name": name,
            "value": "true",
            "domain": domain,
            "path": "/",
            "expires": (template or {}).get("expires") or -1,
            "httpOnly": False,
            "secure": True,
            "sameSite": "Lax",
        }
        added += 1
    if added:
        print(f"  共有セッションの規約同意を {added} 店舗分へ展開しました", flush=True)
    return list(by_key.values())


def report_hall_coverage(cookies: list[dict]) -> None:
    """共有セッションで全店舗を対象にできるかを出す。"""
    halls = halls_for_source("teramoba")
    agree_ids = {
        hid
        for item in cookies
        if (hid := _hall_id_from_cookie(str(item.get("name") or ""), "agreeServiceTerm[")) is not None
        and str(item.get("value") or "") == "true"
    }
    captcha_ids = {
        hid
        for item in cookies
        if (hid := _hall_id_from_cookie(str(item.get("name") or ""), "captchaProtectionKey[")) is not None
        and str(item.get("value") or "")
    }
    domain_cookies = [
        item
        for item in cookies
        if "teramoba2.com" in str(item.get("domain") or "")
        and not str(item.get("name") or "").startswith(CONSENT_PREFIXES)
    ]
    print(
        f"共有セッション: teramoba2.com Cookie {len(domain_cookies)} 件 / 対象 {len(halls)} 店舗",
        flush=True,
    )
    print("店舗別 captchaProtectionKey が無くても、このセッションで取得を試みます。", flush=True)
    for hall in halls:
        hid = int((hall.source_config or {}).get("hall_id") or 0)
        has_agree = hid in agree_ids
        has_captcha = hid in captcha_ids
        extra = "captchaキーあり" if has_captcha else "captchaキーなし（共有セッションで取得）"
        print(f"  対象 {hall.name} hall_id={hid} agreeServiceTerm={has_agree} {extra}", flush=True)
    print(f"一括取得の対象: {len(halls)}/{len(halls)}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="標準 Chrome のテラモバ2 Cookie を data/teramoba のスナップショットへコピーする"
    )
    parser.add_argument(
        "--profile-dir",
        type=Path,
        default=None,
        help="Cookies DB がある Chrome プロファイル（未指定時は全プロファイルから teramoba2.com を探す）",
    )
    args = parser.parse_args()
    print("普段の Chrome で、代表店舗の規約同意と Turnstile を済ませてから実行してください。", flush=True)
    print("  https://tamaya.pt.teramoba2.com/muromi/rack_info_kt?kind_code=21", flush=True)
    print("台番号の表が見えた状態の Cookie を、全店舗共通のセッションとして読みます。", flush=True)
    key = chrome_safe_storage_key()
    dbs = [args.profile_dir / "Cookies"] if args.profile_dir else cookie_dbs()
    dbs = [path for path in dbs if path.is_file()]
    if not dbs:
        raise SystemExit(f"Chrome の Cookie DB が見つかりません: {CHROME_ROOT}")
    merged: dict[tuple[str, str, str], dict] = {}
    for db in dbs:
        found = read_profile_cookies(db, key)
        print(f"  {db.parent.name}: teramoba2 Cookie {len(found)} 件", flush=True)
        for item in found:
            merged[(item["domain"], item["name"], item["path"])] = item
    kept = _keep_missing_consent(merged)
    if kept:
        print(f"  Chrome に無い同意Cookie {kept} 件は、前回のスナップショットを維持しました", flush=True)
    cookies = apply_global_consent(list(merged.values()))
    if not cookies:
        raise SystemExit(
            "teramoba2.com の Cookie が標準 Chrome にありません。"
            "普段の Chrome で台詳細まで開き、規約に同意してから再度実行してください。"
        )
    names = sorted({item["name"] for item in cookies})
    ua = chrome_user_agent()
    state_path, snapshot = write_snapshot(cookies, ua)
    interesting = [name for name in names if any(token in name.lower() for token in ("cf_", "agree", "turnstile", "captcha", "session"))]
    print(f"書き出し: {state_path}", flush=True)
    print(f"写し: {snapshot}", flush=True)
    print(f"Cookie {len(cookies)} 件 / UA {ua}", flush=True)
    print("主な名前: " + (", ".join(interesting) or ", ".join(names[:12])), flush=True)
    report_hall_coverage(cookies)


if __name__ == "__main__":
    main()
