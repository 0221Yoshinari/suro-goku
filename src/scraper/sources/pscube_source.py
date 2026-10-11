"""P'sCUBE ソース。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.scraper.base import DataSource, FetchResult
from src.scraper.halls import halls_for_source
from src.scraper.pscube import fetch_pscube_data
from src.scraper.site7 import ingest_file, load_config


class PscubeSource(DataSource):
    source_id = "pscube"
    display_name = "P'sCUBE"

    async def fetch(self) -> FetchResult:
        return fetch_pscube_data(halls_for_source(self.source_id))

    def ingest(self, path: Path | None = None) -> list[dict[str, Any]]:
        cfg = load_config()
        target = path or self.incoming_path()
        return ingest_file(target, cfg.hall_name, self.data_dir())
