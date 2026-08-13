"""SearchAPI.io adapter for YouTube discovery and transcripts."""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class SearchApiError(RuntimeError):
    """Safe provider error that never exposes keys, tokens, URLs, or bodies."""

    def __init__(
        self,
        *,
        operation: str,
        status_code: int,
        error_code: str = "upstream_error",
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        self.operation = operation
        self.status_code = status_code
        self.error_code = error_code[:80]
        # Keep only the one structured field needed for transcript fallback.
        # Provider metadata can contain request URLs and opaque page tokens.
        self.payload = {
            "available_languages": list((payload or {}).get("available_languages") or [])
        }
        super().__init__(
            f"SearchAPI.io {operation} failed with HTTP {status_code} ({self.error_code})"
        )


Transport = Callable[
    [str, str, Mapping[str, str], bytes | None, float],
    tuple[int, Mapping[str, Any]],
]


def _urllib_transport(
    url: str,
    method: str,
    headers: Mapping[str, str],
    body: bytes | None,
    timeout_seconds: float,
) -> tuple[int, Mapping[str, Any]]:
    request = Request(url, data=body, headers=dict(headers), method=method)  # noqa: S310
    try:
        with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
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
        raise SearchApiError(
            operation="network", status_code=503, error_code="network_error"
        ) from exc


def _error_code(payload: Mapping[str, Any]) -> str:
    error = payload.get("error")
    if isinstance(error, Mapping):
        value = error.get("code") or error.get("type") or error.get("status")
        if value:
            return str(value)
    if isinstance(error, str) and error:
        return error
    return "upstream_error"


def _numeric_count(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        digits = "".join(character for character in value if character.isdigit())
        return int(digits) if digits else None
    return None


class SearchApiClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://www.searchapi.io/api/v1/search",
        timeout_seconds: float = 20.0,
        gl: str = "kr",
        hl: str = "ko",
        zero_retention: bool = False,
        channel_token_post_threshold_bytes: int = 1800,
        transport: Transport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("SEARCH_API_KEY is required")
        self._api_key = api_key
        self.base_url = base_url.rstrip("?")
        self.timeout_seconds = timeout_seconds
        self.gl = gl
        self.hl = hl
        self.zero_retention = zero_retention
        self.channel_token_post_threshold_bytes = channel_token_post_threshold_bytes
        self._transport = transport or _urllib_transport

    def request(
        self,
        operation: str,
        params: Mapping[str, Any],
        *,
        use_post: bool = False,
    ) -> Mapping[str, Any]:
        clean = {key: value for key, value in params.items() if value is not None}
        if self.zero_retention:
            clean["zero_retention"] = True
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }
        body: bytes | None = None
        if use_post:
            headers["Content-Type"] = "application/json"
            url = self.base_url
            body = json.dumps(clean, separators=(",", ":")).encode("utf-8")
            method = "POST"
        else:
            url = f"{self.base_url}?{urlencode({key: str(value).lower() if isinstance(value, bool) else str(value) for key, value in clean.items()})}"
            method = "GET"
        status_code, payload = self._transport(
            url, method, headers, body, self.timeout_seconds
        )
        if not 200 <= status_code < 300:
            raise SearchApiError(
                operation=operation,
                status_code=status_code,
                error_code=_error_code(payload),
                payload=payload,
            )
        if payload.get("error") is not None:
            raise SearchApiError(
                operation=operation,
                status_code=502,
                error_code="provider_error_payload",
            )
        return payload

    def channel(self, *, channel_id: str) -> Mapping[str, Any]:
        return self.request(
            "youtube_channel",
            {
                "engine": "youtube_channel",
                "channel_id": channel_id,
                "gl": self.gl,
                "hl": self.hl,
            },
        )

    def channel_videos(
        self, *, channel_id: str, next_page_token: str | None = None
    ) -> Mapping[str, Any]:
        use_post = bool(
            next_page_token
            and len(next_page_token.encode("utf-8"))
            >= self.channel_token_post_threshold_bytes
        )
        payload = self.request(
            "youtube_channel_videos",
            {
                "engine": "youtube_channel_videos",
                "channel_id": channel_id,
                "next_page_token": next_page_token,
                "gl": self.gl,
                "hl": self.hl,
            },
            use_post=use_post,
        )
        item_arrays = [
            payload.get(key)
            for key in ("videos", "sections")
            if isinstance(payload.get(key), list)
        ]
        has_item_array = bool(item_arrays)
        has_items = any(items for items in item_arrays)
        channel = payload.get("channel") or {}
        pagination = payload.get("pagination") or {}
        channel_id_present = bool(str(channel.get("id") or "").strip())
        has_next_page = bool(str(pagination.get("next_page_token") or "").strip())
        reported_count = _numeric_count(channel.get("videos"))
        if not (
            has_items
            or (has_next_page and (next_page_token is not None or channel_id_present))
            or reported_count == 0
            or (next_page_token is not None and has_item_array)
        ):
            raise SearchApiError(
                operation="youtube_channel_videos",
                status_code=502,
                error_code="provider_incomplete_payload",
            )
        return payload

    def youtube(
        self, *, query: str, page_token: str | None = None
    ) -> Mapping[str, Any]:
        return self.request(
            "youtube",
            {
                "engine": "youtube",
                "q": query,
                "sp": page_token,
                "gl": self.gl,
                "hl": self.hl,
            },
        )

    def transcripts(
        self,
        *,
        video_id: str,
        language: str,
        transcript_type: str,
    ) -> Mapping[str, Any]:
        try:
            return self.request(
                "youtube_transcripts",
                {
                    "engine": "youtube_transcripts",
                    "video_id": video_id,
                    "lang": language,
                    "transcript_type": transcript_type,
                },
            )
        except SearchApiError as exc:
            # A missing selected language is a normal routing result. SearchAPI
            # may expose the alternatives with either a 2xx or a 4xx response.
            if exc.payload.get("available_languages"):
                return exc.payload
            raise
