from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .base import ProviderSearchResult


class SubDLProvider:
    name = "subdl"

    def __init__(self, *, api_key: str, timeout_seconds: int, user_agent: str) -> None:
        self.api_key = api_key.strip()
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent

    def is_enabled(self) -> bool:
        return bool(self.api_key)

    def _request(self, url: str) -> dict[str, Any]:
        request = Request(
            url=url,
            method="GET",
            headers={
                "Accept": "application/json",
                "User-Agent": self.user_agent,
            },
        )
        with urlopen(request, timeout=self.timeout_seconds) as response:
            payload = response.read().decode("utf-8")
        return json.loads(payload)

    def search_movie(self, *, title: str, year: int, tmdb_id: int | None) -> list[ProviderSearchResult]:
        query = {
            "api_key": self.api_key,
            "type": "movie",
            "languages": "EN",
            "year": str(year),
            "film_name": title,
        }
        if tmdb_id is not None:
            query["tmdb_id"] = str(tmdb_id)

        url = "https://api.subdl.com/api/v1/subtitles?" + urlencode(query)
        payload = self._request(url)

        # SubDL responses can use "subtitles" or "results" depending on API version.
        raw_items = payload.get("subtitles")
        if not isinstance(raw_items, list):
            raw_items = payload.get("results")
        if not isinstance(raw_items, list):
            return []

        results: list[ProviderSearchResult] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue

            download_url = item.get("url")
            if not isinstance(download_url, str) or not download_url:
                download_url = item.get("download_url")
            if not isinstance(download_url, str) or not download_url:
                continue
            if download_url.startswith("/"):
                # SubDL returns paths relative to its download host, not full URLs.
                download_url = "https://dl.subdl.com" + download_url

            file_name = item.get("name")
            if not isinstance(file_name, str) or not file_name:
                file_name = item.get("filename") if isinstance(item.get("filename"), str) else "subtitle"

            sub_id = item.get("id")
            if not isinstance(sub_id, (str, int)):
                sub_id = file_name

            release_name = item.get("release_name") if isinstance(item.get("release_name"), str) else None

            results.append(
                ProviderSearchResult(
                    provider=self.name,
                    provider_sub_id=str(sub_id),
                    file_name=file_name,
                    download_url=download_url,
                    release_name=release_name,
                    language="en",
                )
            )

        return results
