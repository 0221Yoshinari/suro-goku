"""データソースの登録台帳。パイプラインは source_id だけで呼び出せる。"""

from __future__ import annotations

from src.scraper.base import DataSource

_SOURCES: dict[str, DataSource] = {}


def register(source: DataSource) -> DataSource:
    _SOURCES[source.source_id] = source
    return source


def _load_builtin() -> None:
    if "site7" in _SOURCES:
        return
    from src.scraper.sources.goraggio_source import GoraggioSource
    from src.scraper.sources.maruhan_source import MaruhanSource
    from src.scraper.sources.pscube_source import PscubeSource
    from src.scraper.sources.site7_source import Site7Source
    from src.scraper.sources.teramoba_source import TeramobaSource

    register(Site7Source())
    register(TeramobaSource())
    register(GoraggioSource())
    register(PscubeSource())
    register(MaruhanSource())


def get_source(source_id: str) -> DataSource:
    _load_builtin()
    if source_id not in _SOURCES:
        known = ", ".join(sorted(_SOURCES)) or "(なし)"
        raise KeyError(f"未知のデータソース: {source_id}。登録済み: {known}")
    return _SOURCES[source_id]


def list_sources() -> list[str]:
    _load_builtin()
    return sorted(_SOURCES)
