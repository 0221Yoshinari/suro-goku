"""HTMLから見出し・リンク・表を順に取り出す。外部ライブラリは使わない。"""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser


@dataclass
class HtmlBlock:
    kind: str
    text: str = ""
    href: str = ""
    rows: list[list[str]] = field(default_factory=list)


class DocumentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[HtmlBlock] = []
        self._ignore = 0
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._heading: list[str] | None = None
        self._href: str | None = None
        self._link_text: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self._ignore += 1
            return
        if self._ignore:
            return
        attr = {key: value or "" for key, value in attrs}
        if tag == "table":
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []
        elif tag in {"h1", "h2", "h3", "h4"}:
            self._heading = []
        elif tag == "a":
            self._href = attr.get("href") or ""
            self._link_text = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"}:
            if self._ignore:
                self._ignore -= 1
            return
        if self._ignore:
            return
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append(_clean("".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._row is not None and self._table is not None:
            if any(cell for cell in self._row):
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            if self._table:
                self.blocks.append(HtmlBlock(kind="table", rows=self._table))
            self._table = None
        elif tag in {"h1", "h2", "h3", "h4"} and self._heading is not None:
            text = _clean("".join(self._heading))
            if text:
                self.blocks.append(HtmlBlock(kind="heading", text=text))
            self._heading = None
        elif tag == "a" and self._href is not None:
            text = _clean("".join(self._link_text or []))
            if self._href:
                self.blocks.append(HtmlBlock(kind="link", text=text, href=self._href))
            self._href = None
            self._link_text = None

    def handle_data(self, data: str) -> None:
        if self._ignore or not data:
            return
        if self._cell is not None:
            self._cell.append(data)
        if self._heading is not None:
            self._heading.append(data)
        if self._link_text is not None:
            self._link_text.append(data)


def parse_document(html: str) -> list[HtmlBlock]:
    parser = DocumentParser()
    parser.feed(html)
    parser.close()
    return parser.blocks


def _clean(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split())
