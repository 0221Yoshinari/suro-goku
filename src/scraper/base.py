"""データソース共通の契約。サイトセブン以外も同じ FetchResult に載せる。"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class FetchResult:
    source: str
    hall_name: str
    machines: list[dict[str, Any]]
    fetched_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    extra: dict[str, Any] = field(default_factory=dict)

    def to_incoming_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "hall_name": self.hall_name,
            "source": self.source,
            "fetched_at": self.fetched_at,
            "machines": self.machines,
        }
        payload.update(self.extra)
        return payload

    def write_incoming(self, output_dir: Path | None = None) -> Path:
        dest = output_dir or (PROJECT_ROOT / "data" / self.source)
        dest.mkdir(parents=True, exist_ok=True)
        path = dest / "incoming.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self.to_incoming_payload(), ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
        return path


class DataSource(ABC):
    """公式データサイトごとの取得・正規化。新しいソースはこれを実装して registry に登録する。"""

    source_id: str
    display_name: str

    def data_dir(self) -> Path:
        return PROJECT_ROOT / "data" / self.source_id

    def incoming_path(self) -> Path:
        return self.data_dir() / "incoming.json"

    async def fetch(self) -> FetchResult:
        raise NotImplementedError(f"{self.source_id} はライブ取得未対応です。")

    @abstractmethod
    def ingest(self, path: Path | None = None) -> list[dict[str, Any]]:
        """incoming JSON などを判定用レコードへ変換する。"""
