"""localhost のダッシュボードを HTTPS で外部公開する。"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from src.analyzer.judgment import PROJECT_ROOT
from src.scraper.site7 import _load_dotenv

STATE_PATH = PROJECT_ROOT / "data" / "tunnel_state.json"
LOCK_PATH = PROJECT_ROOT / "data" / "logs" / "tunnel.lock"
LOG_DIR = PROJECT_ROOT / "data" / "logs"
PUBLIC_URL_RE = re.compile(
    r"https://[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)*\.(?:trycloudflare\.com|ngrok-free\.app|ngrok\.app|ngrok\.io)"
)
DISABLED = {"0", "false", "no", "off"}
DEAD_LOG_MARKERS = (
    "Tunnel not found",
    "failed to serve tunnel connection",
    "control stream encountered a failure",
)


def _ensure_env() -> None:
    _load_dotenv(PROJECT_ROOT / ".env")


def tunnel_enabled() -> bool:
    _ensure_env()
    return (os.getenv("TUNNEL_ENABLED") or "1").strip().lower() not in DISABLED


def _provider() -> str:
    _ensure_env()
    return (os.getenv("TUNNEL_PROVIDER") or "cloudflared").strip().lower()


def extract_public_url(text: str) -> str | None:
    match = PUBLIC_URL_RE.search(text or "")
    return match.group(0) if match else None


def tunnel_log_is_dead(text: str) -> bool:
    """直近ログで、接続成功より後にトンネル失効が出ていれば死んでいる。"""
    if not text:
        return False
    dead_at = max(text.rfind(marker) for marker in DEAD_LOG_MARKERS)
    if dead_at < 0:
        return False
    return dead_at > text.rfind("Registered tunnel connection")


def _read_state() -> dict:
    if not STATE_PATH.is_file():
        return {}
    try:
        payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_state(payload: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def current_public_url(port: int | None = None) -> str | None:
    state = _read_state()
    url = str(state.get("url") or "")
    try:
        pid = int(state.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    if not url.startswith("https://") or not _pid_alive(pid):
        return None
    if port is not None:
        try:
            saved_port = int(state.get("local_port") or 0)
        except (TypeError, ValueError):
            saved_port = 0
        if saved_port and saved_port != port:
            return None
    return url


def _binary(name: str, env_key: str, fallback: str) -> str:
    configured = (os.getenv(env_key) or "").strip()
    if configured:
        return configured
    found = shutil.which(name)
    if found:
        return found
    if Path(fallback).is_file():
        return fallback
    raise RuntimeError(f"{name} が見つかりません。`brew install {name}` を実行してください。")


def _cloudflared_command(port: int) -> list[str]:
    binary = _binary("cloudflared", "CLOUDFLARED_BIN", "/opt/homebrew/bin/cloudflared")
    # QUIC は IPv6 の UDP を優先する。経路が一瞬欠けるとクイックトンネルのホスト名ごと破棄される。
    return [
        binary,
        "tunnel",
        "--no-autoupdate",
        "--edge-ip-version",
        "4",
        "--url",
        f"http://127.0.0.1:{port}",
    ]


def _ngrok_command(port: int) -> list[str]:
    binary = _binary("ngrok", "NGROK_BIN", "/opt/homebrew/bin/ngrok")
    command = [binary, "http", f"127.0.0.1:{port}", "--log", "stdout"]
    token = (os.getenv("NGROK_AUTHTOKEN") or "").strip()
    if token:
        command.extend(["--authtoken", token])
    return command


def _wait_for_url(proc: subprocess.Popen[bytes], log_path: Path, timeout: float, offset: int) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            tail = _read_from(log_path, offset)[0][-2000:]
            raise RuntimeError(f"トンネルプロセスが終了しました ({proc.returncode})\n{tail}")
        url = extract_public_url(_read_from(log_path, offset)[0])
        if url:
            return url
        time.sleep(0.4)
    tail = _read_from(log_path, offset)[0][-2000:]
    raise RuntimeError(f"公開URLの発行がタイムアウトしました\n{tail}")


def _read_from(path: Path, offset: int) -> tuple[str, int]:
    if not path.is_file():
        return "", offset
    with path.open("rb") as handle:
        handle.seek(offset)
        data = handle.read()
    return data.decode("utf-8", errors="replace"), offset + len(data)


def _start_process(command: list[str], provider: str, port: int) -> str:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{provider}.log"
    offset = log_path.stat().st_size if log_path.is_file() else 0
    with log_path.open("ab") as log:
        stamp = datetime.now(timezone.utc).isoformat()
        log.write(f"\n--- start {stamp} port={port} ---\n".encode())
        log.flush()
        offset = log.tell()
        proc = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    url = _wait_for_url(proc, log_path, timeout=60, offset=offset)
    _write_state(
        {
            "url": url,
            "pid": proc.pid,
            "provider": provider,
            "local_port": port,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    return url


def _log_since_last_start(provider: str, tail_bytes: int = 65536) -> str:
    log_path = LOG_DIR / f"{provider}.log"
    if not log_path.is_file():
        return ""
    try:
        size = log_path.stat().st_size
        with log_path.open("rb") as handle:
            handle.seek(max(0, size - tail_bytes))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    marker = "--- start "
    index = tail.rfind(marker)
    return tail[index:] if index >= 0 else tail


def public_url_reachable(url: str, timeout: float = 8) -> bool | None:
    """到達できれば True、トンネル障害なら False、DNS未反映などは None。"""
    target = url if url.endswith("/") else f"{url}/"
    request = urllib.request.Request(
        target,
        method="GET",
        headers={"User-Agent": "surogoku-tunnel-check", "Cache-Control": "no-cache"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status) < 500
    except urllib.error.HTTPError as exc:
        return int(exc.code) < 500
    except (TimeoutError, socket.timeout):
        return None
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return None
        text = str(reason).lower()
        if any(
            token in text
            for token in (
                "nodename",
                "name or service",
                "temporary failure in name resolution",
                "timed out",
                "timeout",
            )
        ):
            return None
        return False
    except Exception:
        return False


def _registration_is_current(provider: str) -> bool:
    text = _log_since_last_start(provider)
    if tunnel_log_is_dead(text):
        return False
    return "Registered tunnel connection" in text


def _connector_pid() -> int:
    try:
        return int(_read_state().get("pid") or 0)
    except (TypeError, ValueError):
        return 0


def _tunnel_still_serving(url: str) -> bool:
    """生きているコネクタは、自己診断の失敗では捨てない。

    同じマシンからクイックトンネルへ叩くとタイムアウトすることがある。
    それを障害とみなしてプロセスを止めると、公開中のホスト名が二度と戻らない。
    """
    provider = str(_read_state().get("provider") or "")
    log = _log_since_last_start(provider) if provider else ""
    if provider and tunnel_log_is_dead(log):
        return False
    if _connector_pid() and _pid_alive(_connector_pid()):
        return True
    probed = public_url_reachable(url)
    if probed is True:
        return True
    if probed is None and provider and _registration_is_current(provider):
        return True
    time.sleep(1.5)
    probed = public_url_reachable(url)
    if probed is True:
        return True
    return bool(provider and probed is None and _registration_is_current(provider))


def local_tunnel_pids(ps_output: str, port: int | None = None) -> list[int]:
    """cloudflared のクイックトンネルのプロセスID。port を渡すとその待受だけ。"""
    pids: list[int] = []
    for line in ps_output.splitlines():
        stripped = line.strip()
        if "cloudflared" not in stripped or "--url" not in stripped:
            continue
        if port is not None and f"127.0.0.1:{port}" not in stripped and f"localhost:{port}" not in stripped:
            continue
        head = stripped.split(None, 1)[0]
        if head.isdigit():
            pids.append(int(head))
    return pids


def _quick_tunnel_pids(port: int | None = None) -> list[int]:
    try:
        output = subprocess.check_output(["ps", "-ax", "-o", "pid=,command="], text=True)
    except (OSError, subprocess.CalledProcessError):
        return []
    return local_tunnel_pids(output, port)


def _stop_process(pid: int) -> None:
    if pid <= 0 or not _pid_alive(pid):
        return
    for sig, wait_seconds in ((signal.SIGTERM, 4), (signal.SIGKILL, 2)):
        try:
            os.kill(pid, sig)
        except OSError:
            return
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            if not _pid_alive(pid):
                return
            time.sleep(0.2)


def _issued_public_url(port: int) -> str | None:
    """一度出したホスト名。プロセスが落ちていても、別のURLへ張り替えない。"""
    state = _read_state()
    url = str(state.get("url") or "")
    if not url.startswith("https://"):
        return None
    try:
        saved_port = int(state.get("local_port") or 0)
    except (TypeError, ValueError):
        saved_port = 0
    if saved_port and saved_port != port:
        return None
    return url


def _ensure_public_url_locked(port: int) -> tuple[str, bool]:
    issued = _issued_public_url(port)
    if issued:
        return issued, False
    # まだ一度も発行していないときだけ作る。作り直すとホスト名が変わる。
    for pid in _quick_tunnel_pids(port):
        _stop_process(pid)
    state = _read_state()
    try:
        pid = int(state.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid:
        _stop_process(pid)
    provider = _provider()
    if provider == "ngrok":
        url = _start_process(_ngrok_command(port), "ngrok", port)
    elif provider in {"cloudflared", "cloudflare"}:
        url = _start_process(_cloudflared_command(port), "cloudflared", port)
    else:
        raise RuntimeError(f"未対応の TUNNEL_PROVIDER です: {provider}")
    deadline = time.time() + 45
    while time.time() < deadline:
        if public_url_reachable(url, timeout=8) is True:
            return url, True
        time.sleep(1.5)
    if _registration_is_current(provider):
        print(f"公開URLを発行しました。DNSの反映待ちの可能性があります: {url}", flush=True)
        return url, True
    raise RuntimeError(f"公開URLは発行されましたが、外部から応答がありません: {url}")


def ensure_public_url(port: int) -> tuple[str, bool]:
    """発行済みの公開URLを返す。ホスト名の張り替えはしない。"""
    from src.run_gate import require_acquisition

    require_acquisition("外部公開")
    _ensure_env()
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            return _ensure_public_url_locked(port)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def supervise_public_url(port: int, interval: float = 45) -> None:
    """互換のための入口。発行済みURLは監視せず、張り替えもしない。"""
    del interval
    url, created = ensure_public_url(port)
    print(url, flush=True)
    if created:
        announce_public_url(url)


def announce_public_url(url: str) -> None:
    from src.notify.discord import send_webhook

    page = url.rstrip("/") + "/lp"
    message = (
        "**紹介ページ**\n"
        "リンクを知っている人は、どの回線からでも開けます。\n"
        f"[紹介ページを開く]({page})"
    )
    try:
        send_webhook(message)
    except Exception as exc:
        print(f"Discordへの公開URL通知に失敗しました: {exc}", flush=True)


def main() -> None:
    from src.run_gate import exit_if_halted

    exit_if_halted("外部公開")
    parser = argparse.ArgumentParser(description="ダッシュボードをHTTPSで外部公開する")
    parser.add_argument("--port", type=int, default=0, help="転送先ポート（未指定なら DASHBOARD_PORT か 8000）")
    parser.add_argument("--supervise", action="store_true", help="公開URLが切れたら再発行し続ける")
    args = parser.parse_args()
    _ensure_env()
    port = args.port or int(os.getenv("DASHBOARD_PORT") or "8000")
    if not tunnel_enabled():
        raise SystemExit("TUNNEL_ENABLED=0 のため外部公開は停止しています")
    if args.supervise:
        supervise_public_url(port)
        return
    url, created = ensure_public_url(port)
    print(url, flush=True)
    if created:
        announce_public_url(url)


if __name__ == "__main__":
    main()
