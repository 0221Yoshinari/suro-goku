from src.scraper.base import DataSource, FetchResult
from src.scraper.registry import get_source, list_sources, register
from src.scraper.site7 import fetch_latest, ingest_file, load_config, to_judgment_record

__all__ = [
    "DataSource",
    "FetchResult",
    "fetch_latest",
    "get_source",
    "ingest_file",
    "list_sources",
    "load_config",
    "register",
    "to_judgment_record",
]
