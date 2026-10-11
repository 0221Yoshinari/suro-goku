"""設定されたホールを source ごとに取得し、判定用 incoming.json にまとめる。"""

from __future__ import annotations

from src.scraper.base import FetchResult, PROJECT_ROOT
from src.scraper.halls import configured_halls, hall_source, halls_for_source, format_duration
from src.scraper.registry import get_source


async def fetch_configured() -> FetchResult:
    from src.run_gate import exit_if_halted

    exit_if_halted("店舗データ取得")
    names = configured_halls()
    if not names:
        raise RuntimeError("有効なホールが config/halls.json にありません")
    print(f"取得オーケストレーション: {' → '.join(f'{n}({hall_source(n)})' for n in names)}", flush=True)

    combined: list[dict] = []
    fetched: list[str] = []
    failed: list[str] = []
    details: list[dict[str, str]] = []
    extras: list[dict] = []
    sources_used: list[str] = []

    async def absorb(source_id: str) -> dict:
        from src.scraper.pace import failure_record, summarize_failure

        halls = halls_for_source(source_id, names=names)
        bucket: dict = {
            "combined": [],
            "fetched": [],
            "failed": [],
            "details": [],
            "extras": [],
            "sources": [],
        }
        if not halls:
            return bucket
        try:
            result = await get_source(source_id).fetch()
        except Exception as exc:
            reason = summarize_failure(str(exc))
            print(f"{source_id} の取得失敗: {reason}", flush=True)
            bucket["failed"].extend(hall.name for hall in halls)
            bucket["details"].extend(failure_record(hall.name, reason) for hall in halls)
            bucket["extras"].append({"elapsed_sec": 0, "error": reason})
            return bucket
        extra = result.extra or {}
        if extra.get("skipped"):
            print(f"{source_id} は取得対象外のためスキップします", flush=True)
            return bucket
        if extra.get("mock"):
            print(f"{source_id} はテストデータのため、定期取得の結合には入れません", flush=True)
            return bucket
        bucket["combined"].extend(result.machines)
        bucket["fetched"].extend(extra.get("halls") or [result.hall_name])
        bucket["failed"].extend(extra.get("failed_halls") or [])
        known = {item.get("hall") for item in bucket["details"]}
        for item in extra.get("failure_details") or []:
            if isinstance(item, dict) and item.get("hall") not in known:
                bucket["details"].append({"hall": str(item.get("hall") or ""), "reason": str(item.get("reason") or "取得失敗")})
                known.add(item.get("hall"))
        for name in extra.get("failed_halls") or []:
            if name not in known:
                bucket["details"].append({"hall": str(name), "reason": "取得失敗"})
                known.add(name)
        bucket["extras"].append(extra)
        bucket["sources"].append(source_id)
        return bucket

    import asyncio
    import time

    started = time.monotonic()
    source_ids = ("teramoba", "goraggio", "pscube", "maruhan_app")
    from src.scraper.halls import max_workers

    # 同時2以下のときは、ソースも1本ずつ進める。
    if max_workers() <= 2:
        print(f"安全モード: ソースを直列で取得（同時 {max_workers()}）", flush=True)
        parts = []
        for source_id in source_ids:
            parts.append(await absorb(source_id))
    else:
        parts = list(await asyncio.gather(*(absorb(source_id) for source_id in source_ids)))
    for bucket in parts:
        combined.extend(bucket["combined"])
        fetched.extend(bucket["fetched"])
        failed.extend(bucket["failed"])
        details.extend(bucket["details"])
        extras.extend(bucket["extras"])
        sources_used.extend(bucket["sources"])

    site7_halls = halls_for_source("site7", names=names)
    if site7_halls:
        import os

        os.environ["SITE7_HALL_NAMES"] = ",".join(h.name for h in site7_halls)
        parts.append(await absorb("site7"))
        bucket = parts[-1]
        combined.extend(bucket["combined"])
        fetched.extend(bucket["fetched"])
        failed.extend(bucket["failed"])
        details.extend(bucket["details"])
        extras.extend(bucket["extras"])
        sources_used.extend(bucket["sources"])

    if not combined:
        raise RuntimeError("全ソースの取得が 0 件のため incoming.json は更新していません。")

    elapsed = time.monotonic() - started
    ordered = [name for name in names if name in fetched]
    label = ordered[0] if len(ordered) == 1 else "複数店舗"
    envelope_source = sources_used[0] if len(sources_used) == 1 else "mixed"
    result = FetchResult(
        source=envelope_source,
        hall_name=label,
        machines=combined,
        extra={
            "halls": ordered,
            "target_halls": names,
            "failed_halls": [name for name in names if name in failed],
            "failure_details": [item for item in details if item.get("hall") in failed or item.get("hall") in names],
            "sources": sources_used,
            "elapsed_sec": round(elapsed, 1),
            "elapsed_label": format_duration(elapsed),
        },
    )
    path = result.write_incoming(PROJECT_ROOT / "data" / "site7")
    print(f"結合 incoming: {path} / {len(combined)} 件 / source={envelope_source}", flush=True)
    return result
