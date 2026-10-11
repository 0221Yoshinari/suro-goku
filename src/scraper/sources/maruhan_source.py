"""マルハンアプリ（ゲスト通信）ソース。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.scraper.base import DataSource, FetchResult
from src.scraper.halls import halls_for_source
from src.scraper.maruhan import SOURCE_ID, fetch_maruhan_data
from src.scraper.site7 import ingest_file, load_config


class MaruhanSource(DataSource):
    source_id = SOURCE_ID
    display_name = "マルハンアプリ"

    async def fetch(self) -> FetchResult:
        return fetch_maruhan_data(halls_for_source(self.source_id))

    def ingest(self, path: Path | None = None) -> list[dict[str, Any]]:
        cfg = load_config()
        target = path or self.incoming_path()
        return ingest_file(target, cfg.hall_name, self.data_dir())
