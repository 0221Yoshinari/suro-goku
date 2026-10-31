"""朝の宣言用に、数値を含まない注目ホール一覧を縦長画像にする。"""

from __future__ import annotations

import html
import tempfile
from pathlib import Path

from src.poster.capture import CaptureError
from src.poster.chase import ChaseTarget
from src.poster.copy import _MORNING_LEAD, _reason_phrase, morning_sections

_SHEET_WIDTH = 1080


def morning_area_card_html(*, date: str) -> str:
    """大阪・東京・福岡の全店対象であることを、店名羅列なしで示す。"""
    from src.poster.day_flow import poster_area_labels

    areas = "\n".join(
        f'<div class="hall"><p class="name">{html.escape(label)}</p></div>'
        for _area, label in poster_area_labels()
    )
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<style>
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; background: #070709; }}
  .sheet {{
    width: {_SHEET_WIDTH}px;
    padding: 0 0 36px;
    background: #070709;
    color: #fff8ee;
    font-family: "Hiragino Sans", "Hiragino Kaku Gothic ProN", "YuGothic", Meiryo, sans-serif;
  }}
  .rainbow {{
    height: 10px;
    background: linear-gradient(90deg, #ff2a2a, #ff7a00, #ffe14a, #39ff6a, #2ad4ff, #7a5cff, #ff2a6a);
  }}
  .head {{ padding: 28px 40px 8px; }}
  h1 {{
    margin: 0;
    font-size: 92px;
    font-weight: 900;
    line-height: 1.05;
    letter-spacing: 0.02em;
    color: #ffe56a;
  }}
  .lead {{
    margin: 18px 0 0;
    font-size: 40px;
    font-weight: 700;
    line-height: 1.35;
    color: #fff8ee;
  }}
  .date {{
    margin: 14px 0 0;
    color: #ffb25a;
    font-size: 32px;
    font-weight: 700;
  }}
  .areas {{ padding: 12px 40px 0; }}
  .name {{
    margin: 0 0 18px;
    font-size: 48px;
    font-weight: 900;
    line-height: 1.25;
    color: #fff8e8;
  }}
</style>
</head>
<body>
  <article class="sheet">
    <div class="rainbow"></div>
    <header class="head">
      <h1>今日追うエリア</h1>
      <p class="lead">{html.escape(_MORNING_LEAD)}</p>
      <p class="date">{html.escape(_date_label(date))}</p>
    </header>
    <div class="areas">{areas}</div>
  </article>
</body>
</html>
"""


def render_morning_area_card(dest: Path, *, date: str) -> None:
    page_html = morning_area_card_html(date=date)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise CaptureError("Playwright が無いため、朝のまとめ画像を作れません") from exc
    with tempfile.TemporaryDirectory() as folder:
        source = Path(folder) / "morning.html"
        source.write_text(page_html, encoding="utf-8")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    viewport={"width": _SHEET_WIDTH, "height": 1200},
                    device_scale_factor=2,
                    locale="ja-JP",
                )
                page = context.new_page()
                page.goto(source.as_uri())
                page.locator(".sheet").screenshot(path=str(dest))
            finally:
                browser.close()
    if not dest.is_file() or dest.stat().st_size == 0:
        raise CaptureError("朝のまとめ画像を保存できませんでした")


def morning_card_html(targets: list[ChaseTarget], *, date: str) -> str:
    """店舗名とイベント名だけの縦画像。枠も数値も置かない。"""
    sections = morning_sections(targets)
    if not sections:
        raise CaptureError("朝のまとめにするホールがありません")
    left, right = _columns(sections)
    body = (
        f'<div class="col">{_column_html(left)}</div>'
        f'<div class="col">{_column_html(right)}</div>'
    )
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<style>
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; background: #070709; }}
  .sheet {{
    width: {_SHEET_WIDTH}px;
    padding: 0 0 36px;
    background: #070709;
    color: #fff8ee;
    font-family: "Hiragino Sans", "Hiragino Kaku Gothic ProN", "YuGothic", Meiryo, sans-serif;
  }}
  .rainbow {{
    height: 10px;
    background: linear-gradient(90deg, #ff2a2a, #ff7a00, #ffe14a, #39ff6a, #2ad4ff, #7a5cff, #ff2a6a);
  }}
  .head {{ padding: 28px 40px 8px; }}
  h1 {{
    margin: 0;
    font-size: 92px;
    font-weight: 900;
    line-height: 1.05;
    letter-spacing: 0.02em;
    color: #ffe56a;
  }}
  .lead {{
    margin: 18px 0 0;
    font-size: 40px;
    font-weight: 700;
    line-height: 1.35;
    color: #fff8ee;
  }}
  .date {{
    margin: 14px 0 0;
    color: #ffb25a;
    font-size: 32px;
    font-weight: 700;
  }}
  .cols {{
    display: flex;
    gap: 28px;
    padding: 12px 40px 0;
  }}
  .col {{ flex: 1; min-width: 0; }}
  h2 {{
    margin: 22px 0 8px;
    color: #ffe14a;
    font-size: 34px;
    font-weight: 900;
    letter-spacing: 0.12em;
  }}
  .hall {{ margin: 0 0 14px; }}
  .name {{
    margin: 0;
    font-size: 42px;
    font-weight: 900;
    line-height: 1.25;
    color: #fff8e8;
  }}
  .reason {{
    margin: 2px 0 0;
    color: #ffb15a;
    font-size: 36px;
    font-weight: 700;
    line-height: 1.3;
  }}
</style>
</head>
<body>
  <article class="sheet">
    <div class="rainbow"></div>
    <header class="head">
      <h1>今日追うホール</h1>
      <p class="lead">{html.escape(_MORNING_LEAD)}</p>
      <p class="date">{html.escape(_date_label(date))}</p>
    </header>
    <div class="cols">{body}</div>
  </article>
</body>
</html>
"""


def render_morning_card(targets: list[ChaseTarget], dest: Path, *, date: str) -> None:
    """モバイル幅の縦長PNGにする。数値は描かない。"""
    page_html = morning_card_html(targets, date=date)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise CaptureError("Playwright が無いため、朝のまとめ画像を作れません") from exc
    with tempfile.TemporaryDirectory() as folder:
        source = Path(folder) / "morning.html"
        source.write_text(page_html, encoding="utf-8")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    viewport={"width": _SHEET_WIDTH, "height": 1600},
                    device_scale_factor=2,
                    locale="ja-JP",
                )
                page = context.new_page()
                page.goto(source.as_uri())
                page.locator(".sheet").screenshot(path=str(dest))
            finally:
                browser.close()
    if not dest.is_file() or dest.stat().st_size == 0:
        raise CaptureError("朝のまとめ画像を保存できませんでした")


def _columns(
    sections: list[tuple[str, list[ChaseTarget]]],
) -> tuple[list[tuple[str, list[ChaseTarget]]], list[tuple[str, list[ChaseTarget]]]]:
    total = sum(len(rows) for _label, rows in sections)
    half = (total + 1) // 2
    left: list[tuple[str, list[ChaseTarget]]] = []
    right: list[tuple[str, list[ChaseTarget]]] = []
    count = 0
    for section in sections:
        if count < half:
            left.append(section)
            count += len(section[1])
        else:
            right.append(section)
    return left, right


def _column_html(sections: list[tuple[str, list[ChaseTarget]]]) -> str:
    chunks: list[str] = []
    for label, rows in sections:
        halls = "\n".join(_hall_card(target) for target in rows)
        chunks.append(f"<section><h2>{html.escape(label)}</h2>{halls}</section>")
    return "\n".join(chunks)


def _hall_card(target: ChaseTarget) -> str:
    name = html.escape(target.hall_name)
    reason = html.escape(_reason_phrase(target))
    return f'<div class="hall"><p class="name">{name}</p><p class="reason">{reason}</p></div>'


def _date_label(date: str) -> str:
    parts = date.split("-")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return date
    year, month, day = (int(part) for part in parts)
    return f"{year}年{month}月{day}日"
