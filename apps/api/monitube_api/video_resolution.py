"""Pure parsing for direct YouTube video collection inputs; it makes no network calls."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from urllib.parse import parse_qs, unquote, urlsplit


class VideoInputError(ValueError):
    pass


class VideoInputKind(str, Enum):
    VIDEO_ID = "video_id"
    WATCH_URL = "watch_url"
    SHORT_URL = "short_url"


@dataclass(frozen=True, slots=True)
class VideoResolution:
    kind: VideoInputKind
    normalized: str


_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YOUTUBE_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com"})
_SHORT_HOSTS = frozenset({"youtu.be", "www.youtu.be"})
_URL_PREFIX = re.compile(r"^(?:https?://)?(?:www\.|m\.)?(?:youtube\.com|youtu\.be)(?:/|$)", re.IGNORECASE)


def _clean(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise VideoInputError("Video input cannot be empty")
    return cleaned


def _validated_id(value: str) -> str:
    if not _VIDEO_ID.fullmatch(value):
        raise VideoInputError("A YouTube video ID must contain exactly 11 URL-safe characters")
    return value


def _from_url(value: str) -> VideoResolution:
    url = value if "://" in value else f"https://{value}"
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    parts = [unquote(part) for part in parsed.path.split("/") if part]

    if host in _SHORT_HOSTS:
        if not parts:
            raise VideoInputError("A youtu.be URL must include a video ID")
        return VideoResolution(VideoInputKind.SHORT_URL, _validated_id(parts[0]))
    if host not in _YOUTUBE_HOSTS:
        raise VideoInputError("Only youtube.com and youtu.be video URLs are accepted")
    if parsed.path.rstrip("/") == "/watch":
        video_id = parse_qs(parsed.query).get("v", [None])[0]
        if not isinstance(video_id, str):
            raise VideoInputError("A YouTube watch URL must include its v parameter")
        return VideoResolution(VideoInputKind.WATCH_URL, _validated_id(video_id))
    if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live"}:
        return VideoResolution(VideoInputKind.WATCH_URL, _validated_id(parts[1]))
    raise VideoInputError("Unsupported YouTube video URL format")


def resolve_video_input(value: str) -> VideoResolution:
    """Normalize a video ID, YouTube watch URL, or youtu.be short URL to a video ID."""

    cleaned = _clean(value)
    if _URL_PREFIX.match(cleaned):
        return _from_url(cleaned)
    if re.match(r"^[a-z][a-z0-9+.-]*://", cleaned, re.IGNORECASE):
        raise VideoInputError("Only YouTube video URLs are accepted")
    return VideoResolution(VideoInputKind.VIDEO_ID, _validated_id(cleaned))
