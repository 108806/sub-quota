from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class ProviderSearchResult:
    provider: str
    provider_sub_id: str
    file_name: str
    download_url: str
    release_name: str | None = None
    language: str = "en"


class SubtitleProvider(Protocol):
    name: str

    def is_enabled(self) -> bool:
        ...

    def search_movie(self, *, title: str, year: int, tmdb_id: int | None) -> list[ProviderSearchResult]:
        ...
