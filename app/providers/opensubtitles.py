from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .base import ProviderSearchResult


class OpenSubtitlesProvider:
    name = "opensubtitles"

    def __init__(
        self,
        *,
        api_key: str,
        username: str,
        password: str,
        user_agent: str,
        timeout_seconds: int,
    ) -> None:
        self.api_key = api_key.strip()
        self.username = username.strip()
        self.password = password.strip()
        self.user_agent = user_agent
        self.timeout_seconds = timeout_seconds
        self._token: str | None = None

    def is_enabled(self) -> bool:
        return bool(self.api_key and self.username and self.password)

    def _json_request(
        self,
        *,
        method: str,
        url: str,
        body: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Api-Key": self.api_key,
            "User-Agent": self.user_agent,
            "Accept": "application/json",
        }
        if extra_headers:
            headers.update(extra_headers)

        data_bytes: bytes | None = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data_bytes = json.dumps(body).encode("utf-8")

        request = Request(url=url, method=method, headers=headers, data=data_bytes)
        with urlopen(request, timeout=self.timeout_seconds) as response:
            payload = response.read().decode("utf-8")
        return json.loads(payload)

    def _ensure_token(self) -> str:
        if self._token:
            return self._token

        payload = self._json_request(
            method="POST",
            url="https://api.opensubtitles.com/api/v1/login",
            body={
                "username": self.username,
                "password": self.password,
            },
        )
        token = payload.get("token")
        if not isinstance(token, str) or not token:
            raise RuntimeError("OpenSubtitles login failed: token not returned")
        self._token = token
        return token

    def _build_download_link(self, file_id: int) -> str:
        token = self._ensure_token()
        payload = self._json_request(
            method="POST",
            url="https://api.opensubtitles.com/api/v1/download",
            body={"file_id": file_id},
            extra_headers={"Authorization": f"Bearer {token}"},
        )
        link = payload.get("link")
        if not isinstance(link, str) or not link:
            raise RuntimeError("OpenSubtitles download link missing")
        return link

    def search_movie(self, *, title: str, year: int, tmdb_id: int | None) -> list[ProviderSearchResult]:
        query = {
            "query": title,
            "year": str(year),
            "languages": "en",
            "type": "movie",
            "order_by": "download_count",
            "order_direction": "desc",
        }
        if tmdb_id is not None:
            query["tmdb_id"] = str(tmdb_id)

        url = "https://api.opensubtitles.com/api/v1/subtitles?" + urlencode(query)
        payload = self._json_request(method="GET", url=url)
        items = payload.get("data", [])
        if not isinstance(items, list):
            return []

        results: list[ProviderSearchResult] = []
        for item in items:
            attrs = item.get("attributes", {}) if isinstance(item, dict) else {}
            files = attrs.get("files", []) if isinstance(attrs, dict) else []
            if not isinstance(files, list):
                continue

            release_name = attrs.get("release") if isinstance(attrs.get("release"), str) else None
            for file_obj in files:
                if not isinstance(file_obj, dict):
                    continue
                file_id = file_obj.get("file_id")
                if not isinstance(file_id, int):
                    continue

                file_name = file_obj.get("file_name") if isinstance(file_obj.get("file_name"), str) else "subtitle"
                try:
                    download_url = self._build_download_link(file_id)
                except Exception:
                    continue

                results.append(
                    ProviderSearchResult(
                        provider=self.name,
                        provider_sub_id=str(file_id),
                        file_name=file_name,
                        download_url=download_url,
                        release_name=release_name,
                        language="en",
                    )
                )

        return results
