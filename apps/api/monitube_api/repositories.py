"""Shared persistence errors, cursors, policies, and legacy imports.

New code should depend on protocols from :mod:`monitube_api.ports` and concrete
adapters from :mod:`monitube_api.infrastructure`. The legacy repository export
remains until external callers have migrated to those package boundaries.
"""

from __future__ import annotations

import base64
import binascii
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
import hashlib
import json
from threading import RLock
from typing import Any, Iterable

from .analysis import build_summary
from .collection_policy import (
    coverage_satisfies,
    desired_coverage,
    job_coverage,
    merge_collection_config,
)
from .domain import (
    CollectionRequestRecord,
    CollectionSubmission,
    CollectionSubscriptionRecord,
    CollectionTargetRecord,
    CommentRecord,
    JobRecord,
    JobState,
    QuotaBucket,
    SourceRecord,
    SourceType,
    VideoRecord,
    new_id,
    utcnow,
)
from .fuzzy_search import normalize_search_text, rank_text_fields
from .ports import (
    CollectionRepository,
    CollectionRequestRepository,
    JobRepository,
    SourceRepository,
)
from .ports.results import CommentThreadSort


class RepositoryError(RuntimeError):
    pass


class RepositoryUnavailableError(RepositoryError):
    """The persistence layer is temporarily unavailable and may be retried."""


class NotFoundError(RepositoryError):
    pass


class InvalidStateTransitionError(RepositoryError):
    pass


class InvalidCursorError(RepositoryError):
    """A syntactically invalid cursor or one bound to another request scope."""


SOURCE_VIDEO_CURSOR_VERSION = 1
SOURCE_VIDEO_SORT = "effective_published_desc"
EXPLORE_VIDEO_CURSOR_VERSION = 1
EXPLORE_VIDEO_SORT = "effective_published_fetched_desc"


@dataclass(frozen=True, slots=True)
class SourceVideoCursor:
    effective_at: datetime
    youtube_video_id: str
    snapshot_at: datetime


@dataclass(frozen=True, slots=True)
class ExploreVideoCursor:
    effective_at: datetime
    fetched_at: datetime
    youtube_video_id: str
    snapshot_at: datetime


def source_video_filter_hash() -> str:
    """Fingerprint the normalized filter contract, currently the empty filter."""

    return hashlib.sha256(b"{}").hexdigest()


def _effective_video_timestamp(video: VideoRecord) -> datetime:
    return video.published_at or video.source_fetched_at


def _video_sort_key(video: VideoRecord) -> tuple[datetime, str]:
    return (_effective_video_timestamp(video), video.youtube_video_id)


def encode_source_video_cursor(
    video: VideoRecord,
    *,
    snapshot_at: datetime,
    scope: str,
    filter_hash: str,
    sort: str = SOURCE_VIDEO_SORT,
) -> str:
    payload = {
        "v": SOURCE_VIDEO_CURSOR_VERSION,
        "at": _effective_video_timestamp(video).isoformat(),
        "id": video.youtube_video_id,
        "snapshot": snapshot_at.isoformat(),
        "scope": scope,
        "filter": filter_hash,
        "sort": sort,
    }
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def decode_source_video_cursor(
    cursor: str | None,
    *,
    scope: str,
    filter_hash: str,
    sort: str = SOURCE_VIDEO_SORT,
) -> SourceVideoCursor | None:
    if not cursor:
        return None
    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.b64decode(cursor + padding, altchars=b"-_", validate=True)
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("cursor payload must be an object")
        if payload.get("v") != SOURCE_VIDEO_CURSOR_VERSION:
            raise ValueError("unsupported cursor version")
        if payload.get("scope") != scope:
            raise ValueError("cursor scope does not match request")
        if payload.get("filter") != filter_hash:
            raise ValueError("cursor filter does not match request")
        if payload.get("sort") != sort:
            raise ValueError("cursor sort does not match request")
        effective_at = datetime.fromisoformat(str(payload["at"]))
        snapshot_at = datetime.fromisoformat(str(payload["snapshot"]))
        youtube_video_id = str(payload["id"])
        if effective_at.tzinfo is None or snapshot_at.tzinfo is None or not youtube_video_id:
            raise ValueError("cursor fields are invalid")
        return SourceVideoCursor(
            effective_at=effective_at,
            youtube_video_id=youtube_video_id,
            snapshot_at=snapshot_at,
        )
    except (binascii.Error, UnicodeDecodeError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise InvalidCursorError("Invalid source video cursor") from exc


def explore_video_filter_hash(channel_id: str | None) -> str:
    normalized = json.dumps(
        {"channelId": channel_id or None},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()


def _explore_video_sort_key(video: VideoRecord) -> tuple[datetime, datetime, str]:
    return (_effective_video_timestamp(video), video.source_fetched_at, video.youtube_video_id)


def encode_explore_video_cursor(
    video: VideoRecord,
    *,
    snapshot_at: datetime,
    scope: str,
    filter_hash: str,
) -> str:
    payload = {
        "v": EXPLORE_VIDEO_CURSOR_VERSION,
        "kind": "explore-video",
        "effectiveAt": _effective_video_timestamp(video).isoformat(),
        "fetchedAt": video.source_fetched_at.isoformat(),
        "id": video.youtube_video_id,
        "snapshot": snapshot_at.isoformat(),
        "scope": scope,
        "filter": filter_hash,
        "sort": EXPLORE_VIDEO_SORT,
    }
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def decode_explore_video_cursor(
    cursor: str | None,
    *,
    scope: str,
    filter_hash: str,
) -> ExploreVideoCursor | None:
    if not cursor:
        return None
    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.b64decode(cursor + padding, altchars=b"-_", validate=True)
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("cursor payload must be an object")
        if payload.get("v") != EXPLORE_VIDEO_CURSOR_VERSION or payload.get("kind") != "explore-video":
            raise ValueError("unsupported cursor version")
        if payload.get("scope") != scope:
            raise ValueError("cursor scope does not match request")
        if payload.get("filter") != filter_hash:
            raise ValueError("cursor filter does not match request")
        if payload.get("sort") != EXPLORE_VIDEO_SORT:
            raise ValueError("cursor sort does not match request")
        effective_at = datetime.fromisoformat(str(payload["effectiveAt"]))
        fetched_at = datetime.fromisoformat(str(payload["fetchedAt"]))
        snapshot_at = datetime.fromisoformat(str(payload["snapshot"]))
        youtube_video_id = str(payload["id"])
        if (
            effective_at.tzinfo is None
            or fetched_at.tzinfo is None
            or snapshot_at.tzinfo is None
            or not youtube_video_id
        ):
            raise ValueError("cursor fields are invalid")
        return ExploreVideoCursor(
            effective_at=effective_at,
            fetched_at=fetched_at,
            youtube_video_id=youtube_video_id,
            snapshot_at=snapshot_at,
        )
    except (binascii.Error, UnicodeDecodeError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise InvalidCursorError("Invalid explore video cursor") from exc


def _comment_sort_key(comment: CommentRecord) -> tuple[datetime, str]:
    return (comment.published_at or comment.source_fetched_at, comment.youtube_comment_id)


def encode_comment_cursor(comment: CommentRecord) -> str:
    payload = json.dumps(
        {"at": _comment_sort_key(comment)[0].isoformat(), "id": comment.youtube_comment_id},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_comment_cursor(cursor: str | None) -> tuple[datetime, str] | None:
    if not cursor:
        return None
    try:
        padding = "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(cursor + padding).decode("utf-8"))
        return datetime.fromisoformat(str(payload["at"])), str(payload["id"])
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise RepositoryError("Invalid comment cursor") from exc


def _comment_thread_sort_key(
    comment: CommentRecord, sort: CommentThreadSort
) -> tuple[int, datetime, str] | tuple[datetime, str]:
    published_key, comment_id = _comment_sort_key(comment)
    if sort == "recommended":
        return (comment.like_count or 0, published_key, comment_id)
    return (published_key, comment_id)


def encode_comment_thread_cursor(comment: CommentRecord, sort: CommentThreadSort) -> str:
    published_key, comment_id = _comment_sort_key(comment)
    payload: dict[str, Any] = {
        "sort": sort,
        "at": published_key.isoformat(),
        "id": comment_id,
    }
    if sort == "recommended":
        payload["likes"] = comment.like_count or 0
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def decode_comment_thread_cursor(
    cursor: str | None, sort: CommentThreadSort
) -> tuple[int, datetime, str] | tuple[datetime, str] | None:
    if not cursor:
        return None
    try:
        padding = "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(cursor + padding).decode("utf-8"))
        cursor_sort = str(payload.get("sort", "newest"))
        if cursor_sort != sort:
            raise ValueError("cursor sort does not match request")
        published_key = datetime.fromisoformat(str(payload["at"]))
        comment_id = str(payload["id"])
        if sort == "recommended":
            return int(payload["likes"]), published_key, comment_id
        return published_key, comment_id
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise RepositoryError("Invalid comment thread cursor") from exc


_ALLOWED_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.QUEUED: frozenset({JobState.RUNNING, JobState.WAITING_RETRY, JobState.WAITING_QUOTA, JobState.CANCELLED}),
    JobState.RUNNING: frozenset(
        {
            JobState.WAITING_RETRY,
            JobState.WAITING_QUOTA,
            JobState.COMPLETED,
            JobState.COMPLETED_WITH_WARNINGS,
            JobState.FAILED,
            JobState.CANCELLED,
        }
    ),
    JobState.WAITING_RETRY: frozenset({JobState.QUEUED, JobState.RUNNING, JobState.CANCELLED, JobState.FAILED}),
    JobState.WAITING_QUOTA: frozenset({JobState.QUEUED, JobState.RUNNING, JobState.CANCELLED, JobState.FAILED}),
    JobState.FAILED: frozenset({JobState.QUEUED, JobState.CANCELLED}),
    JobState.CANCELLED: frozenset({JobState.QUEUED}),
    JobState.COMPLETED: frozenset(),
    JobState.COMPLETED_WITH_WARNINGS: frozenset(),
}

# Compatibility export while callers migrate to the infrastructure package.
from .infrastructure.memory_repository import InMemoryRepository
