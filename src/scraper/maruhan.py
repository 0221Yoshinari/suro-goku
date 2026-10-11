"""マルハンアプリのゲスト通信から台データを取る。

ログインAPIは呼ばない。店舗を切り替える前と、台ごとの履歴を追加で取る前に
1.5〜3.5秒のランダム待機を入れる。通信URL・User-Agent・アプリ固有ヘッダーは
config/halls.json の maruhan_app と環境変数で差し替える。

取得行は P'sCUBE と同じキー（machine_name, dai_no, total_games, bb, rb,
合成確率, BB確率, RB確率, art, hall_name）に揃え、詳細履歴は history に載せる。
"""

from __future__ import annotations

import json
import os
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from src.scraper.base import PROJECT_ROOT, FetchResult
from src.scraper.halls import Hall, format_duration, hall_data_dir, halls_for_source, load_halls_config, sleep_hall_gap
from src.scraper.pscube import _as_int, _dai_no, _row_from_counts
from src.scraper.targets import is_watched

SOURCE_ID = "maruhan_app"
PAUSE_MIN_SEC = 1.5
PAUSE_MAX_SEC = 3.5
DEFAULT_USER_AGENT = "MaruhanApp/1.0"
DEFAULT_APP_HEADERS = {"X-App-Version": "1.0.0"}
DEFAULT_MACHINES_PATH = "/guest/halls/{hall_id}/machines"

_NAME_KEYS = ("machine_name", "name", "model_name", "kishu", "kishu_name")
_DAI_KEYS = ("dai_no", "dai", "machine_no", "rack_no", "unit_no", "no")
_GAME_KEYS = (
    "total_games",
    "total_game",
    "game_count",
    "games",
    "start",
    "all_game_count",
    "cumulative_games",
)
_BB_KEYS = ("bb", "bb_count", "bb_num", "big", "big_count", "bonus_1")
_RB_KEYS = ("rb", "rb_count", "reg", "reg_count", "reg_num", "bonus_2")
_ART_KEYS = ("art", "art_count", "at", "at_count", "bonus_3")
_DIFF_KEYS = ("差玉", "差枚", "diff", "sadama", "substraction")
_HISTORY_KEYS = ("history", "histories", "detail_history", "bonus_history", "records")
_LIST_KEYS = ("machines", "units", "racks", "data", "list", "items", "result", "results")
_DAY_KEYS = ("day", "date", "business_day", "businessDay")


class MaruhanError(RuntimeError):
    """マルハンアプリからの取得失敗。"""


@dataclass(frozen=True)
class MaruhanConfig:
    hall_id: str
    hall_name: str
    base_url: str
    machines_path: str
    history_path: str
    user_agent: str
    headers: dict[str, str]
    query: dict[str, str]
    pause_min_sec: float = PAUSE_MIN_SEC
    pause_max_sec: float = PAUSE_MAX_SEC
    fixture_path: str = ""
    timeout_sec: float = 30.0


def _block(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    return dict(value) if isinstance(value, dict) else {}


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _env(name: str) -> str:
    return _text(os.getenv(name))


def _fold_key(key: str) -> str:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key))
    return text.lower().replace("-", "_")


def _pick(raw: dict[str, Any], keys: tuple[str, ...]) -> Any:
    folded = {_fold_key(key): value for key, value in raw.items()}
    for key in keys:
        if key in raw and raw[key] not in (None, ""):
            return raw[key]
        value = folded.get(_fold_key(key))
        if value not in (None, ""):
            return value
    return None


def _headers_from(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items() if _text(key) and item is not None}


def _query_from(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items() if _text(key) and item is not None}


def _pause_bound(value: Any, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if number >= 0 else fallback


def config_for_hall(hall: Hall) -> MaruhanConfig:
    """店舗設定 → halls.json の共通 maruhan_app → 環境変数の順で上書きする。"""
    local = dict(hall.source_config or {})
    shared = _block(load_halls_config(), "maruhan_app")
    header_env = _env("MARUHAN_HEADERS_JSON")
    extra_headers = dict(DEFAULT_APP_HEADERS)
    extra_headers.update(_headers_from(shared.get("headers")))
    extra_headers.update(_headers_from(local.get("headers")))
    if header_env:
        try:
            parsed = json.loads(header_env)
        except json.JSONDecodeError as exc:
            raise MaruhanError("MARUHAN_HEADERS_JSON がJSONオブジェクトではありません") from exc
        extra_headers.update(_headers_from(parsed))
    query = _query_from(shared.get("query"))
    query.update(_query_from(local.get("query")))
    lo = _pause_bound(
        _env("MARUHAN_PAUSE_MIN_SEC") or local.get("pause_min_sec") or shared.get("pause_min_sec"),
        PAUSE_MIN_SEC,
    )
    hi = _pause_bound(
        _env("MARUHAN_PAUSE_MAX_SEC") or local.get("pause_max_sec") or shared.get("pause_max_sec"),
        PAUSE_MAX_SEC,
    )
    if hi < lo:
        hi = lo
    return MaruhanConfig(
        hall_id=_env("MARUHAN_HALL_ID") or hall.maruhan_hall_id or _text(local.get("maruhan_hall_id")),
        hall_name=hall.name,
        base_url=_env("MARUHAN_BASE_URL") or _text(local.get("base_url")) or _text(shared.get("base_url")),
        machines_path=(
            _env("MARUHAN_MACHINES_PATH")
            or _text(local.get("machines_path"))
            or _text(shared.get("machines_path"))
            or DEFAULT_MACHINES_PATH
        ),
        history_path=_env("MARUHAN_HISTORY_PATH") or _text(local.get("history_path")) or _text(shared.get("history_path")),
        user_agent=_env("MARUHAN_USER_AGENT") or _text(local.get("user_agent")) or _text(shared.get("user_agent")) or DEFAULT_USER_AGENT,
        headers=extra_headers,
        query=query,
        pause_min_sec=lo,
        pause_max_sec=hi,
        fixture_path=_env("MARUHAN_FIXTURE") or _text(local.get("fixture_path")),
        timeout_sec=_pause_bound(_env("MARUHAN_TIMEOUT_SEC") or local.get("timeout_sec") or shared.get("timeout_sec"), 30.0) or 30.0,
    )


def build_guest_headers(config: MaruhanConfig) -> dict[str, str]:
    """未ログインのリクエストヘッダー。Cookie（ログインセッション）は付けない。"""
    headers = {
        "Accept": "application/json",
        "Accept-Language": "ja-JP,ja;q=0.9",
        "User-Agent": config.user_agent,
    }
    for key, value in config.headers.items():
        if key.lower() == "cookie":
            continue
        headers[key] = value
    headers.pop("Cookie", None)
    return headers


def build_guest_url(config: MaruhanConfig, path_template: str, **fmt: Any) -> str:
    if not config.base_url:
        raise MaruhanError(
            f"{config.hall_name}: base_url が未設定です。"
            "config/halls.json の maruhan_app.base_url か MARUHAN_BASE_URL を設定してください。"
        )
    if not path_template:
        raise MaruhanError(f"{config.hall_name}: リクエストパスが未設定です。")
    mapping = {"hall_id": config.hall_id, **{key: "" if value is None else str(value) for key, value in fmt.items()}}
    try:
        path = path_template.format(**mapping)
    except KeyError as exc:
        raise MaruhanError(f"{config.hall_name}: パスの置換に失敗しました ({exc})") from exc
    url = urllib.parse.urljoin(config.base_url.rstrip("/") + "/", path.lstrip("/"))
    query = {}
    for key, value in config.query.items():
        try:
            query[key] = value.format(**mapping)
        except KeyError as exc:
            raise MaruhanError(f"{config.hall_name}: クエリの置換に失敗しました ({exc})") from exc
    if query:
        joiner = "&" if "?" in url else "?"
        url = f"{url}{joiner}{urllib.parse.urlencode(query)}"
    return url


def build_guest_request(config: MaruhanConfig, path_template: str, **fmt: Any) -> urllib.request.Request:
    """GET のみ。アカウントの送信本体は作らない。"""
    return urllib.request.Request(
        build_guest_url(config, path_template, **fmt),
        data=None,
        headers=build_guest_headers(config),
        method="GET",
    )


def wait_between_requests(
    config: MaruhanConfig | None = None,
    *,
    sleeper: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> float:
    """店舗間・追加の履歴リクエストの前に 1.5〜3.5 秒（設定で変更可）待つ。"""
    lo = config.pause_min_sec if config else PAUSE_MIN_SEC
    hi = config.pause_max_sec if config else PAUSE_MAX_SEC
    delay = (rng or random).uniform(lo, hi)
    sleeper(delay)
    return delay


def map_history(payload: Any) -> list[dict[str, Any]] | None:
    """台の詳細履歴を、判定側が読む history 配列にする。"""
    rows = payload
    if isinstance(payload, dict):
        rows = _pick(payload, _HISTORY_KEYS + _LIST_KEYS)
        if rows is None:
            rows = [payload]
    if not isinstance(rows, list):
        return None
    history: list[dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        day = _pick(item, _DAY_KEYS)
        bb = _as_int(_pick(item, _BB_KEYS))
        rb = _as_int(_pick(item, _RB_KEYS))
        art = _as_int(_pick(item, _ART_KEYS))
        games = _as_int(_pick(item, _GAME_KEYS))
        if day in (None, "") and bb is None and rb is None and art is None and games is None:
            continue
        entry: dict[str, Any] = {"day": day, "bb": bb, "rb": rb, "art": art}
        if games is not None:
            entry["games"] = games
        history.append(entry)
    return history or None


def map_maruhan_machine(raw: dict[str, Any], hall_name: str) -> dict[str, Any] | None:
    """マルハンの1台を、P'sCUBE と同じ台行へ変換する。"""
    name = _text(_pick(raw, _NAME_KEYS))
    number = _dai_no(_text(_pick(raw, _DAI_KEYS)))
    if not name or not number:
        return None
    games = _as_int(_pick(raw, _GAME_KEYS))
    bb = _as_int(_pick(raw, _BB_KEYS))
    rb = _as_int(_pick(raw, _RB_KEYS))
    art = _as_int(_pick(raw, _ART_KEYS))
    row = _row_from_counts(
        machine_name=name,
        dai_no=number,
        games=games,
        bb=bb,
        rb=rb,
        art=art,
        hall_name=hall_name,
        diff=_as_int(_pick(raw, _DIFF_KEYS)),
    )
    history = map_history(_pick(raw, _HISTORY_KEYS))
    if history:
        row["history"] = history
    return row


def extract_machines(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in _LIST_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = extract_machines(value)
            if nested:
                return nested
    if _pick(payload, _NAME_KEYS) and _pick(payload, _DAI_KEYS):
        return [payload]
    return []


def map_maruhan_payload(payload: Any, hall_name: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw in extract_machines(payload):
        row = map_maruhan_machine(raw, hall_name)
        if row is None:
            continue
        key = (str(row["machine_name"]), str(row["dai_no"]))
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)
    return rows


class MaruhanClient:
    """ゲストの GET だけを送る。"""

    def __init__(self, config: MaruhanConfig) -> None:
        self.config = config

    def get_json(self, path_template: str, **fmt: Any) -> Any:
        request = build_guest_request(self.config, path_template, **fmt)
        from src.scraper.proxy_config import urlopen as proxied_urlopen

        try:
            with proxied_urlopen(request, timeout=self.config.timeout_sec) as response:
                body = response.read().decode("utf-8", errors="replace")
                status = response.status
        except urllib.error.HTTPError as exc:
            raise MaruhanError(f"{self.config.hall_name}: HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise MaruhanError(f"{self.config.hall_name}: 接続できません") from exc
        if status != 200:
            raise MaruhanError(f"{self.config.hall_name}: HTTP {status}")
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise MaruhanError(f"{self.config.hall_name}: JSONではありません {request.full_url}") from exc


def uses_mock(config: MaruhanConfig) -> bool:
    """通信先か店舗IDが空なら、推測URLへは投げずテスト用の台データを使う。"""
    return not config.fixture_path and (not config.base_url or not config.hall_id)


def mock_guest_payload(hall_name: str) -> dict[str, Any]:
    """アプリ応答の形をしたテストデータ。全6と全56が1台ずつ出るマイジャグラーV。"""
    return {
        "mock": True,
        "hall_name": hall_name,
        "machines": [
            {
                "machineName": "マイジャグラーV",
                "machineNo": "101",
                "bbCount": 20,
                "rbCount": 20,
                "totalGame": 4580,
                "detailHistory": [{"date": "2026-10-02", "bb": 20, "rb": 20, "games": 4580}],
            },
            {
                "machineName": "マイジャグラーV",
                "machineNo": "102",
                "bbCount": 20,
                "rbCount": 20,
                "totalGame": 4760,
                "detailHistory": [{"date": "2026-10-02", "bb": 20, "rb": 20, "games": 4760}],
            },
        ],
    }


def _load_fixture(path: str) -> Any:
    target = Path(path)
    if not target.is_file():
        target = PROJECT_ROOT / path
    if not target.is_file():
        raise MaruhanError(f"フィクスチャがありません: {path}")
    return json.loads(target.read_text(encoding="utf-8"))


def fetch_maruhan_hall(
    hall: Hall,
    client: MaruhanClient | None = None,
    *,
    sleeper: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> list[dict[str, Any]]:
    config = config_for_hall(hall)
    mocked = uses_mock(config)
    if config.fixture_path:
        payload = _load_fixture(config.fixture_path)
    elif mocked:
        print(
            f"{hall.name}: base_url と maruhan_hall_id が未設定のため、テスト用の台データで変換します",
            flush=True,
        )
        payload = mock_guest_payload(hall.name)
    else:
        session = client or MaruhanClient(config)
        print(f"マルハンアプリ（ゲスト）へアクセス: {hall.name} hall_id={config.hall_id}", flush=True)
        payload = session.get_json(config.machines_path)
    rows = [row for row in map_maruhan_payload(payload, hall.name) if is_watched(str(row.get("machine_name") or ""))]
    if mocked:
        for row in rows:
            row["mock"] = True
    if config.fixture_path or not config.history_path:
        print(f"  取得 {len(rows)} 台", flush=True)
        return rows
    session = client or MaruhanClient(config)
    for index, row in enumerate(rows):
        if index:
            delay = wait_between_requests(config, sleeper=sleeper, rng=rng)
            print(f"  履歴の前に {delay:.1f}秒 待機", flush=True)
        history_payload = session.get_json(config.history_path, dai_no=row["dai_no"], machine_name=row["machine_name"])
        history = map_history(history_payload)
        if history:
            row["history"] = history
    print(f"  取得 {len(rows)} 台", flush=True)
    return rows


def fetch_maruhan_data(
    halls: list[Hall] | None = None,
    *,
    sleeper: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> FetchResult:
    # アプリ専用の台データは、ブラウザ向けの取得経路では読めない。後回しにする。
    if (os.getenv("MARUHAN_FETCH") or "").strip() == "1":
        return _fetch_maruhan_data_live(halls, sleeper=sleeper, rng=rng)
    print("マルハンはアプリ専用データのため、今回の取得からスキップします", flush=True)
    return FetchResult(
        source=SOURCE_ID,
        hall_name="マルハン",
        machines=[],
        extra={"skipped": True, "reason": "アプリ専用データのため後回し", "halls": [], "failed_halls": []},
    )


def _fetch_maruhan_data_live(
    halls: list[Hall] | None = None,
    *,
    sleeper: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> FetchResult:
    targets = halls if halls is not None else halls_for_source(SOURCE_ID)
    if not targets:
        raise MaruhanError("source=maruhan_app の有効ホールが config/halls.json にありません")
    started = time.monotonic()
    combined: list[dict[str, Any]] = []
    fetched: list[str] = []
    failed: list[str] = []
    reasons: list[str] = []
    mocked_halls: list[str] = []
    for index, hall in enumerate(targets):
        if index:
            sleep_hall_gap()
        try:
            payload = fetch_maruhan_hall(hall, sleeper=sleeper, rng=rng)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            print(f"  {hall.name} の取得失敗: {exc}", flush=True)
            failed.append(hall.name)
            reasons.append(f"{hall.name}: {exc}")
            continue
        if not payload:
            failed.append(hall.name)
            reasons.append(f"{hall.name}: 0 件")
            print(f"  {hall.name} の取得失敗: 0 件", flush=True)
            continue
        hall_result = FetchResult(
            source=SOURCE_ID,
            hall_name=hall.name,
            machines=payload,
            extra={"mock": uses_mock(config_for_hall(hall))},
        )
        hall_path = hall_result.write_incoming(hall_data_dir(hall.name, PROJECT_ROOT / "data" / SOURCE_ID))
        print(f"  {hall.name}: {len(payload)} 件を {hall_path} に保存", flush=True)
        combined.extend(payload)
        fetched.append(hall.name)
        if uses_mock(config_for_hall(hall)):
            mocked_halls.append(hall.name)
    elapsed = time.monotonic() - started
    if not combined:
        detail = " / ".join(reasons)
        raise MaruhanError("マルハンの取得に失敗したため incoming.json は更新していません。" + (f" {detail}" if detail else ""))
    result = FetchResult(
        source=SOURCE_ID,
        hall_name=fetched[0] if len(fetched) == 1 else "複数店舗",
        machines=combined,
        extra={
            "halls": fetched,
            "failed_halls": failed,
            "mock": bool(fetched) and len(mocked_halls) == len(fetched),
            "elapsed_sec": round(elapsed, 1),
            "elapsed_label": format_duration(elapsed),
        },
    )
    result.write_incoming(PROJECT_ROOT / "data" / SOURCE_ID)
    return result
