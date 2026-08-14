from __future__ import annotations

import time
import xmlrpc.client
from typing import Any

from .base import ProviderSearchResult

# Same free/legacy endpoint VLC (VLsub) and QNapi use: anonymous login, no
# personal API key or account needed, just a registered client "User-Agent".
# It has no daily download quota, only a per-second throttle, so it never
# runs out for a whole day the way the REST-based providers do.
_XMLRPC_URL = "https://api.opensubtitles.org/xml-rpc"

# Server limit is ~40 requests / 10s per IP; stay well under it on purpose.
_MIN_REQUEST_INTERVAL_SECONDS = 1.0
_MAX_ATTEMPTS = 5
_RETRY_BACKOFF_SECONDS = 5.0


class _TimeoutTransport(xmlrpc.client.SafeTransport):
    def __init__(self, timeout: int, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._timeout = timeout

    def make_connection(self, host: Any) -> Any:
        connection = super().make_connection(host)
        connection.timeout = self._timeout
        return connection


class OpenSubtitlesLegacyProvider:
    name = "opensubtitles_legacy"

    def __init__(self, *, user_agent: str, timeout_seconds: int) -> None:
        self.user_agent = user_agent.strip()
        self.timeout_seconds = timeout_seconds
        self._token: str | None = None
        self._last_request_at: float = 0.0

    def is_enabled(self) -> bool:
        return bool(self.user_agent)

    def _server(self) -> xmlrpc.client.ServerProxy:
        transport = _TimeoutTransport(self.timeout_seconds)
        return xmlrpc.client.ServerProxy(_XMLRPC_URL, transport=transport, allow_none=True)

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        remaining = _MIN_REQUEST_INTERVAL_SECONDS - elapsed
        if remaining > 0:
            time.sleep(remaining)
        self._last_request_at = time.monotonic()

    def _call_with_retry(self, fn: Any) -> Any:
        last_exc: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            self._throttle()
            try:
                return fn()
            except (xmlrpc.client.ProtocolError, OSError) as exc:
                last_exc = exc
                time.sleep(_RETRY_BACKOFF_SECONDS * attempt)
        raise RuntimeError(f"OpenSubtitles legacy request failed after retries: {last_exc}")

    def _login(self, server: xmlrpc.client.ServerProxy) -> str:
        if self._token:
            return self._token

        response = self._call_with_retry(lambda: server.LogIn("", "", "en", self.user_agent))
        if not isinstance(response, dict) or response.get("status") != "200 OK":
            raise RuntimeError(f"OpenSubtitles legacy login failed: {response}")
        token = response.get("token")
        if not isinstance(token, str) or not token:
            raise RuntimeError("OpenSubtitles legacy login did not return a token")
        self._token = token
        return token

    def search_movie(self, *, title: str, year: int, tmdb_id: int | None) -> list[ProviderSearchResult]:
        server = self._server()
        token = self._login(server)
        response = self._call_with_retry(
            lambda: server.SearchSubtitles(token, [{"query": title, "sublanguageid": "eng"}])
        )

        if not isinstance(response, dict) or response.get("status") != "200 OK":
            if isinstance(response, dict) and str(response.get("status", "")).startswith(("401", "403")):
                # Token expired/invalid: drop it and let the next call re-login.
                self._token = None
            return []

        items = response.get("data")
        if not isinstance(items, list):
            return []

        results: list[ProviderSearchResult] = []
        for item in items:
            if not isinstance(item, dict):
                continue

            movie_year = item.get("MovieYear")
            if movie_year and str(movie_year).strip() not in ("", str(year)):
                continue

            download_url = item.get("SubDownloadLink")
            if not isinstance(download_url, str) or not download_url:
                continue

            sub_id = item.get("IDSubtitleFile") or item.get("IDSubtitle")
            file_name = item.get("SubFileName") if isinstance(item.get("SubFileName"), str) else "subtitle"
            release_name = (
                item.get("MovieReleaseName") if isinstance(item.get("MovieReleaseName"), str) else None
            )

            results.append(
                ProviderSearchResult(
                    provider=self.name,
                    provider_sub_id=str(sub_id) if sub_id is not None else file_name,
                    file_name=file_name,
                    download_url=download_url,
                    release_name=release_name,
                    language="en",
                )
            )

        return results
