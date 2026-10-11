"""OCRまたはサイトセブン由来JSONを判定する。"""

from __future__ import annotations

import argparse
from pathlib import Path

from src.analyzer.judgment import (
    DEFAULT_MASTERS_PATH,
    DEFAULT_OCR_DIR,
    DEFAULT_OUTPUT_DIR,
    judge_all,
    judge_records,
)
from src.ocr.extract import DEFAULT_IMAGES_DIR, extract_from_directory
from src.scraper.registry import get_source, list_sources


def run_store(
    images_dir: Path = DEFAULT_IMAGES_DIR,
    ocr_dir: Path = DEFAULT_OCR_DIR,
    masters_path: Path = DEFAULT_MASTERS_PATH,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    skip_ocr: bool = False,
    gpu: bool = False,
) -> dict:
    ocr_summary = None
    if not skip_ocr and images_dir.is_dir():
        ocr_summary = extract_from_directory(images_dir=images_dir, output_dir=ocr_dir, gpu=gpu)
    judgment = judge_all(ocr_dir=ocr_dir, masters_path=masters_path, output_dir=output_dir)
    return {"ocr": ocr_summary, "judgment": judgment}


def run_source(
    source_id: str,
    input_path: Path | None = None,
    masters_path: Path = DEFAULT_MASTERS_PATH,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> dict:
    source = get_source(source_id)
    records = source.ingest(input_path)
    judgment = judge_records(
        records,
        masters_path=masters_path,
        output_dir=output_dir,
        source=source.source_id,
    )
    return {"records": records, "judgment": judgment}


def run_site7(
    input_path: Path | None = None,
    masters_path: Path = DEFAULT_MASTERS_PATH,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> dict:
    return run_source("site7", input_path=input_path, masters_path=masters_path, output_dir=output_dir)


def _print_judgment(judgment: dict, output_dir: Path) -> None:
    machines = judgment.get("machines") or judgment.get("results") or []
    machine_count = judgment.get("machine_count", len(machines))
    unit_count = judgment.get("unit_count", judgment.get("count", 0))
    print(f"判定機種: {machine_count} 機種（全 {unit_count} 台）")
    print(f"全6数値: {judgment.get('all_high_count', 0)} 機種")
    print(f"全56数値: {judgment.get('high_56_count', 0)} 機種")
    print(f"全456数値: {judgment.get('high_base_count', 0)} 機種")
    if judgment.get("hall_counts"):
        for hall, counts in judgment["hall_counts"].items():
            print(
                f"  {hall}: {counts.get('machine_count', counts.get('count', 0))} 機種 / "
                f"{counts.get('count', 0)} 台 / "
                f"全6 {counts.get('all_high_count', 0)} / "
                f"全56 {counts.get('high_56_count', 0)} / "
                f"全456 {counts.get('high_base_count', 0)}"
            )
    for board in judgment.get("hall_digit_summaries") or []:
        print(f"全館末尾 {board.get('hall_name')}:")
        for row in board.get("digits") or []:
            print(f"  {row.get('rank_label')} 平均設定={row.get('avg_setting')} 台数={row.get('unit_count')} 総G={row.get('total_games')}")
    print(f"出力: {output_dir.resolve() / 'latest.json'}")
    for item in machines:
        if item.get("is_all_high"):
            flag = "★"
        elif item.get("is_high_base"):
            flag = "◆"
        elif item.get("is_early_alert"):
            flag = "▲"
        else:
            flag = "-"
        print(
            f"{flag} {item.get('hall_name') or ''} {item.get('machine_name') or '(機種不明)'} "
            f"{item.get('unit_count', 1)}台 "
            f"[{item['status']}/{item['status_label']}] "
            f"総ゲーム={item.get('total_games')} "
            f"合成確率={item.get('synthetic_probability')} "
            f"設定6={((item.get('criteria') or {}).get('synthetic_probability_threshold'))}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="台データを判定する")
    parser.add_argument(
        "--source",
        default="ocr",
        help="ocr、または登録済みデータソース（" + ", ".join(["ocr", *list_sources()]) + "）",
    )
    parser.add_argument("--images-dir", type=Path, default=DEFAULT_IMAGES_DIR)
    parser.add_argument("--ocr-dir", type=Path, default=DEFAULT_OCR_DIR)
    parser.add_argument("--input", type=Path, help="データソースの入力JSON")
    parser.add_argument("--masters", type=Path, default=DEFAULT_MASTERS_PATH)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--skip-ocr", action="store_true")
    parser.add_argument("--gpu", action="store_true")
    args = parser.parse_args()

    if args.source != "ocr":
        result = run_source(
            args.source,
            input_path=args.input,
            masters_path=args.masters,
            output_dir=args.output_dir,
        )
        print(f"ソース: {args.source}")
        print(f"変換件数: {len(result['records'])}")
        _print_judgment(result["judgment"], args.output_dir)
        return

    result = run_store(
        images_dir=args.images_dir,
        ocr_dir=args.ocr_dir,
        masters_path=args.masters,
        output_dir=args.output_dir,
        skip_ocr=args.skip_ocr,
        gpu=args.gpu,
    )
    ocr = result["ocr"]
    if ocr is not None:
        print(f"OCR件数: {ocr['count']}")
    _print_judgment(result["judgment"], args.output_dir)


__all__ = ["run_store", "run_site7", "run_source", "main"]
