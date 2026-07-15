"""Minimal YouTube Data API v3 adapter for one server-managed API credential."""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from monitube_api.domain import QuotaBucket
from monitube_api.quota import extract_youtube_error_reasons


class YouTubeApiError(RuntimeError):
    """Safe upstream error metadata; its message never includes the API key."""

    def __init__(self, *, endpoint: str, bucket: QuotaBucket, status_code: int, payload: Mapping[str, Any] | None = None) -> None:
        self.endpoint = endpoint
        self.bucket = bucket
        self.status_code = status_code
        self.payload = dict(payload or {})
        self.reasons = extract_youtube_error_reasons(self.payload)
        reason = self.reasons[0] if self.reasons else "upstream_error"
        super().__init__(f"YouTube {endpoint} request failed with HTTP {status_code} ({reason})")


Transport = Callable[[str, float], tuple[int, Mapping[str, Any]]]


def _urllib_transport(url: str, timeout_seconds: float) -> tuple[int, Mapping[str, Any]]:
    try:
        with urlopen(url, timeout=timeout_seconds) as response:  # noqa: S310 - URL is a configured Google API base URL
            raw = response.read()
            return int(response.status), json.loads(raw.decode("utf-8"))
    except HTTPError as exc:
        raw = exc.read()
        try:
            payload: Mapping[str, Any] = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
        return int(exc.code), payload
    except (URLError, TimeoutError, OSError) as exc:
        raise YouTubeApiError(endpoint="network", bucket=QuotaBucket.CORE, status_code=503, payload={"error": {"message": str(exc)}}) from exc


class YouTubeDataClient:
    """Small endpoint wrapper with injectable transport for deterministic tests.

    Only this class adds the server-managed API key to the request. Consumers should
    never log the generated URL because it contains that key.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://www.googleapis.com/youtube/v3",
        timeout_seconds: float = 20.0,
        transport: Transport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("YOUTUBE_API_KEY is required for live collection")
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._transport = transport or _urllib_transport

    @staticmethod
    def bucket_for(endpoint: str) -> QuotaBucket:
        return QuotaBucket.SEARCH_QUERIES if endpoint == "search" else QuotaBucket.CORE

    def request(self, endpoint: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        bucket = self.bucket_for(endpoint)
        query = {key: str(value) for key, value in params.items() if value is not None}
        query["key"] = self._api_key
        status_code, payload = self._transport(f"{self.base_url}/{endpoint}?{urlencode(query)}", self.timeout_seconds)
        if status_code < 200 or status_code >= 300:
            raise YouTubeApiError(endpoint=endpoint, bucket=bucket, status_code=status_code, payload=payload)
        return payload

    def channels(self, **params: Any) -> Mapping[str, Any]:
        return self.request("channels", params)

    def search(self, **params: Any) -> Mapping[str, Any]:
        return self.request("search", params)

    def playlist_items(self, **params: Any) -> Mapping[str, Any]:
        return self.request("playlistItems", params)

    def videos(self, **params: Any) -> Mapping[str, Any]:
        return self.request("videos", params)

    def comment_threads(self, **params: Any) -> Mapping[str, Any]:
        return self.request("commentThreads", params)
