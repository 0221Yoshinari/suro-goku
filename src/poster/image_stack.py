"""複数の PNG を1枚に縦に並べる。"""

from __future__ import annotations

from pathlib import Path

from PIL import Image


def stack_images(paths: list[Path], dest: Path, *, width: int = 1200, gap: int = 16) -> None:
    if not paths:
        raise ValueError("画像がありません")
    scaled: list[Image.Image] = []
    for path in paths:
        with Image.open(path) as opened:
            image = opened.convert("RGB")
        if image.width != width:
            height = max(1, int(image.height * width / image.width))
            image = image.resize((width, height), Image.Resampling.LANCZOS)
        scaled.append(image)
    total = sum(item.height for item in scaled) + gap * (len(scaled) - 1)
    canvas = Image.new("RGB", (width, total), (7, 10, 18))
    y = 0
    for index, image in enumerate(scaled):
        canvas.paste(image, (0, y))
        y += image.height + (gap if index < len(scaled) - 1 else 0)
    dest.parent.mkdir(parents=True, exist_ok=True)
    temporary = dest.with_suffix(dest.suffix + ".tmp")
    canvas.save(temporary, format="PNG", optimize=True)
    temporary.replace(dest)
