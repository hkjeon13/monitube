"""Pure parsing for channel input; this module deliberately makes no network calls."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from urllib.parse import unquote, urlsplit


class ChannelInputError(ValueError):
    pass


class ChannelInputKind(str, Enum):
    CHANNEL_ID = "channel_id"
    HANDLE = "handle"
    LEGACY_USERNAME = "legacy_username"
    AMBIGUOUS_NAME = "ambiguous_name"


@dataclass(frozen=True, slots=True)
class ChannelResolution:
    kind: ChannelInputKind
    normalized: str
    lookup_parameter: str
    requires_search: bool


_CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_LEGACY_USERNAME = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_YOUTUBE_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com"})
_URL_PREFIX = re.compile(r"^(?:https?://)?(?:www\.|m\.)?youtube\.com(?:/|$)", re.IGNORECASE)


def _clean(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", value).strip()
    if not cleaned:
        raise ChannelInputError("Channel input cannot be empty")
    return cleaned


def _validate_handle(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    if not normalized.startswith("@"):
        raise ChannelInputError("Invalid YouTube handle")
    body = normalized[1:]
    separators = frozenset("._-·")
    if not 1 <= len(body) <= 30 or body[0] in separators or body[-1] in separators:
        raise ChannelInputError("Invalid YouTube handle")
    if any(
        not (
            character.isalnum()
            or unicodedata.category(character).startswith("M")
            or character in separators
        )
        for character in body
    ):
        raise ChannelInputError("Invalid YouTube handle")
    return normalized


def _from_youtube_url(value: str) -> ChannelResolution:
    url = value if "://" in value else f"https://{value}"
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if host not in _YOUTUBE_HOSTS:
        raise ChannelInputError("Only youtube.com channel URLs are accepted")

    pieces = [unquote(piece) for piece in parsed.path.split("/") if piece]
    if not pieces:
        raise ChannelInputError("A YouTube channel URL must include a channel identifier")

    first = pieces[0]
    if first == "channel" and len(pieces) >= 2:
        channel_id = pieces[1]
        if not _CHANNEL_ID.fullmatch(channel_id):
            raise ChannelInputError("Invalid YouTube channel ID in URL")
        return ChannelResolution(ChannelInputKind.CHANNEL_ID, channel_id, "id", False)
    if first.startswith("@"):
        handle = _validate_handle(first)
        return ChannelResolution(ChannelInputKind.HANDLE, handle, "forHandle", False)
    if first == "user" and len(pieces) >= 2:
        username = pieces[1]
        if not _LEGACY_USERNAME.fullmatch(username):
            raise ChannelInputError("Invalid legacy YouTube username")
        return ChannelResolution(ChannelInputKind.LEGACY_USERNAME, username, "forUsername", False)
    if first == "c" and len(pieces) >= 2:
        # /c names have no stable API lookup parameter. Resolve with a bounded search later.
        return ChannelResolution(ChannelInputKind.AMBIGUOUS_NAME, pieces[1], "search", True)
    raise ChannelInputError("Unsupported YouTube channel URL format")


def resolve_channel_input(value: str) -> ChannelResolution:
    """Normalize a channel ID, handle, supported YouTube URL, or ambiguous display name.

    A caller can use ``lookup_parameter`` directly with ``channels.list``.  Ambiguous
    names deliberately return a search instruction rather than guessing a channel.
    """

    cleaned = _clean(value)
    if _URL_PREFIX.match(cleaned):
        return _from_youtube_url(cleaned)
    if re.match(r"^[a-z][a-z0-9+.-]*://", cleaned, re.IGNORECASE):
        raise ChannelInputError("Only YouTube channel URLs are accepted")
    if _CHANNEL_ID.fullmatch(cleaned):
        return ChannelResolution(ChannelInputKind.CHANNEL_ID, cleaned, "id", False)
    if cleaned.startswith("@"):
        return ChannelResolution(ChannelInputKind.HANDLE, _validate_handle(cleaned), "forHandle", False)
    return ChannelResolution(ChannelInputKind.AMBIGUOUS_NAME, cleaned, "search", True)
