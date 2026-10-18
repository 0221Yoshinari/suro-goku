"""1店舗の取得が終わった時点で、その店の判定ファイルだけを更新する。"""

from __future__ import annotations

from pathlib import Path

from src.scraper.log_safe import emit, failure_reason


def publish_hall_incoming(path: Path, hall_name: str, source: str) -> None:
    """incoming を判定し、data/judgments/halls/{id}/latest.json へ書く。"""
    try:
        from src.analyzer.judgment import DEFAULT_OUTPUT_DIR, judge_records
        from src.scraper.site7 import load_rows, to_judgment_record
        from src.scraper.targets import match_target

        raw_rows, fetched_at = load_rows(path)
        records = []
        for row in raw_rows:
            if not isinstance(row, dict):
                continue
            machine = str(row.get("machine_name") or row.get("kishu") or row.get("name") or "")
            if not match_target(machine):
                continue
            if str(row.get("dai_no") or "").strip() in {"", "平均", "合計", "計"}:
                continue
            record = to_judgment_record(row, str(row.get("hall_name") or hall_name))
            if fetched_at:
                record["fetched_at"] = fetched_at
            records.append(record)
        if not records:
            return
        judge_records(records, output_dir=DEFAULT_OUTPUT_DIR, source=source, incremental=True)
        emit(f"  {hall_name}: 判定を画面へ反映しました", flush=True)
        from src.dashboard.judgment_sync import push_hall_file
        from src.scraper.halls import hall_dir_slug

        push_hall_file(DEFAULT_OUTPUT_DIR / "halls" / hall_dir_slug(hall_name) / "latest.json")
    except Exception as exc:
        emit(f"  {hall_name}: 判定の反映に失敗しました: {failure_reason(exc)}", flush=True)
