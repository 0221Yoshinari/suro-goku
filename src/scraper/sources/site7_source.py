"""サイトセブン（データロボ）ソース。ライブ取得と JSON 正規化を同じ契約で提供する。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.scraper.base import DataSource, FetchResult
from src.scraper.site7 import fetch_latest, ingest_file, load_config


class Site7Source(DataSource):
    source_id = "site7"
    display_name = "サイトセブン（データロボ）"

    async def fetch(self) -> FetchResult:
        from src.scraper.site7_fetch import fetch_site7_data

        result = await fetch_site7_data()
        if result is None:
            raise RuntimeError("サイトセブンの取得に失敗したため incoming.json は更新していません。")
        return result

    def ingest(self, path: Path | None = None) -> list[dict[str, Any]]:
        cfg = load_config()
        if path:
            return ingest_file(path, cfg.hall_name, self.data_dir())
        return fetch_latest(cfg)
