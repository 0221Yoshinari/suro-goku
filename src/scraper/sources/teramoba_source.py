"""テラモバ2（ホール公式データサイト）ソース。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.scraper.base import DataSource, FetchResult
from src.scraper.halls import halls_for_source
from src.scraper.site7 import ingest_file, load_config
from src.scraper.teramoba import fetch_teramoba_data_async


class TeramobaSource(DataSource):
    source_id = "teramoba"
    display_name = "テラモバ2（ホール公式）"

    async def fetch(self) -> FetchResult:
        halls = halls_for_source(self.source_id)
        return await fetch_teramoba_data_async(halls)

    def ingest(self, path: Path | None = None) -> list[dict[str, Any]]:
        cfg = load_config()
        target = path or self.incoming_path()
        return ingest_file(target, cfg.hall_name, self.data_dir())
