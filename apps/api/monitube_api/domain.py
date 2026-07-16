"""Framework-independent records and state machine used by API and workers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import uuid4


def utcnow() -> datetime:
    """Return a timezone-aware UTC timestamp suitable for persisted records."""

    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid4())


class SourceType(str, Enum):
    CHANNEL = "channel"
    KEYWORD = "keyword"
    VIDEO = "video"


class QuotaBucket(str, Enum):
    SEARCH_QUERIES = "search_queries"
    CORE = "core"


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_RETRY = "waiting_retry"
    WAITING_QUOTA = "waiting_quota"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {
            JobState.COMPLETED,
            JobState.COMPLETED_WITH_WARNINGS,
            JobState.FAILED,
            JobState.CANCELLED,
        }


@dataclass(frozen=True, slots=True)
class SourceRecord:
    id: str
    type: SourceType
    config: dict[str, Any]
    enabled: bool
    created_at: datetime
    updated_at: datetime
    next_run_at: datetime | None = None
    # ``collection_sources`` remains a backwards-compatible worker-facing table.
    # New collection flows attach it to one canonical target instead of creating a
    # new physical source for every browser submission.
    target_id: str | None = None
    canonical_key: str | None = None
    coverage: dict[str, Any] = field(default_factory=dict)
    last_completed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class JobRecord:
    id: str
    source_id: str
    state: JobState
    current_stage: str
    progress_completed: int
    progress_total: int | None
    progress_unit: str
    include_comments: bool
    max_videos: int | None
    max_comments_per_video: int | None
    checkpoint: dict[str, Any] = field(default_factory=dict)
    pause_reason: str | None = None
    quota_bucket: QuotaBucket | None = None
    resume_at: datetime | None = None
    resume_is_automatic: bool = False
    partial_errors: list[dict[str, Any]] = field(default_factory=list)
    runtime_config_id: str | None = None
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    target_id: str | None = None


@dataclass(frozen=True, slots=True)
class CollectionTargetRecord:
    """A canonical physical collection target shared by all user requests."""

    id: str
    type: SourceType
    canonical_key: str
    config: dict[str, Any]
    coverage: dict[str, Any]
    resolved_channel_id: str | None
    last_completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class CollectionRequestRecord:
    """A browser request attached to a target and, when needed, a shared job."""

    id: str
    target_id: str
    source_id: str | None
    request_config: dict[str, Any]
    idempotency_key: str | None
    job_id: str | None
    status: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class CollectionSubmission:
    """Atomic result of accepting a collection request."""

    request: CollectionRequestRecord
    target: CollectionTargetRecord
    source: SourceRecord
    job: JobRecord | None
    disposition: str


@dataclass(frozen=True, slots=True)
class VideoRecord:
    """Normalized public YouTube video data retained by a collection source."""

    id: str
    youtube_video_id: str
    youtube_channel_id: str | None
    title: str | None
    description: str | None
    published_at: datetime | None
    duration_seconds: int | None
    privacy_status: str | None
    made_for_kids: bool | None
    statistics: dict[str, int]
    source_fetched_at: datetime


@dataclass(frozen=True, slots=True)
class CommentRecord:
    """A public comment body and minimal metadata, keyed by YouTube comment ID."""

    id: str
    youtube_comment_id: str
    youtube_video_id: str
    youtube_parent_comment_id: str | None
    youtube_thread_id: str | None
    text_display: str | None
    like_count: int
    published_at: datetime | None
    updated_at: datetime | None
    source_fetched_at: datetime
