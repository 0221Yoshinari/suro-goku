"""./images/ 内の画像を EasyOCR で読み、抽出結果を JSON に保存する。"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.ocr.parser import machine_name_from_filename, parse_ocr_items

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}


def _to_builtin(value: Any) -> Any:
    """NumPy の int32 / float64 などを JSON 可能な Python 型へ変換する。"""
    if isinstance(value, dict):
        return {key: _to_builtin(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_builtin(val) for val in value]
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value

    module = type(value).__module__
    if module == "numpy" or module.startswith("numpy."):
        if hasattr(value, "tolist"):
            converted = value.tolist()
            if converted is not value:
                return _to_builtin(converted)
        if hasattr(value, "item"):
            return _to_builtin(value.item())

    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    return value


def _dumps(payload: Any) -> str:
    return json.dumps(_to_builtin(payload), ensure_ascii=False, indent=2)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_IMAGES_DIR = PROJECT_ROOT / "images"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "ocr_results"


def _list_images(images_dir: Path) -> list[Path]:
    files = [
        path
        for path in images_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in IMAGE_EXTS
    ]
    return sorted(files)


def _output_json_name(image_path: Path, images_dir: Path) -> str:
    try:
        relative = image_path.relative_to(images_dir)
    except ValueError:
        relative = Path(image_path.name)
    return "__".join(relative.with_suffix("").parts) + ".json"


def extract_from_image(image_path: Path, reader: Any) -> dict[str, Any]:
    """1枚の画像からテキストと数値フィールドを抽出する。"""
    raw_items = reader.readtext(str(image_path))
    parsed = parse_ocr_items(raw_items)
    texts = [
        {
            "text": text,
            "confidence": round(float(conf), 4),
            "bbox": [[float(x), float(y)] for x, y in bbox],
        }
        for bbox, text, conf in raw_items
    ]
    fields = parsed.to_dict()
    if not fields.get("machine_name"):
        fields["machine_name"] = machine_name_from_filename(image_path.name)
    return _to_builtin(
        {
            "source_file": image_path.name,
            "source_path": str(image_path.resolve()),
            "extracted_at": datetime.now(timezone.utc).isoformat(),
            "fields": fields,
            "raw_texts": texts,
        }
    )


def extract_from_directory(
    images_dir: Path = DEFAULT_IMAGES_DIR,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    languages: list[str] | None = None,
    gpu: bool = False,
) -> dict[str, Any]:
    """フォルダ内の全画像を解析し、個別JSONとサマリJSONを保存する。"""
    try:
        import easyocr
    except ImportError as exc:
        raise SystemExit(
            "EasyOCR が見つかりません。`pip install -r requirements.txt` を実行してください。"
        ) from exc

    images_dir = images_dir.resolve()
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    if not images_dir.is_dir():
        raise FileNotFoundError(f"画像フォルダがありません: {images_dir}")

    image_paths = _list_images(images_dir)
    reader = easyocr.Reader(languages or ["ja", "en"], gpu=gpu)

    results: list[dict[str, Any]] = []
    for path in image_paths:
        result = extract_from_image(path, reader)
        results.append(result)
        out_file = output_dir / _output_json_name(path, images_dir)
        out_file.write_text(_dumps(result), encoding="utf-8")

    summary = {
        "images_dir": str(images_dir),
        "count": len(results),
        "results": results,
    }
    summary_path = output_dir / "summary.json"
    summary_path.write_text(_dumps(summary), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="ローカル画像から数字・ラベルをOCR抽出する")
    parser.add_argument(
        "--images-dir",
        type=Path,
        default=DEFAULT_IMAGES_DIR,
        help="入力画像フォルダ（デフォルト: ./images）",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="JSON出力先（デフォルト: ./data/ocr_results）",
    )
    parser.add_argument("--gpu", action="store_true", help="GPUを使う（未指定時はCPU）")
    args = parser.parse_args()

    summary = extract_from_directory(
        images_dir=args.images_dir,
        output_dir=args.output_dir,
        gpu=args.gpu,
    )
    print(f"解析件数: {summary['count']}")
    print(f"出力: {args.output_dir.resolve() / 'summary.json'}")
    for item in summary["results"]:
        fields = item["fields"]
        print(
            f"- {item['source_file']}: "
            f"機種={fields.get('machine_name')} "
            f"BB={fields.get('bb_count')} "
            f"RB={fields.get('rb_count')} "
            f"BB確率={fields.get('bb_probability')} "
            f"RB確率={fields.get('rb_probability')} "
            f"合成確率={fields.get('synthetic_probability')} "
            f"総ゲーム={fields.get('total_games')} "
            f"初当たり={fields.get('first_hit_count')}"
        )


if __name__ == "__main__":
    main()
