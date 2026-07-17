"""Pydantic request/response contracts exposed by the FastAPI application."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .domain import JobState, QuotaBucket, SourceType


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class HealthResponse(ApiModel):
    status: Literal["ok"] = "ok"
    service: Literal["monitube-api"] = "monitube-api"


class LoginRequest(ApiModel):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")
    password: str = Field(min_length=8, max_length=256)


class AuthUserResponse(ApiModel):
    username: str


class RuntimeKeyRegistration(ApiModel):
    apiKeys: list[str] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_keys(self) -> "RuntimeKeyRegistration":
        keys = tuple(dict.fromkeys(key.strip() for key in self.apiKeys if key.strip()))
        if not keys or any(len(key) < 20 or len(key) > 256 for key in keys):
            raise ValueError("Provide one to 32 valid API keys")
        self.apiKeys = list(keys)
        return self


class RuntimeKeyRegistrationResponse(ApiModel):
    accepted: int = Field(ge=1)


class ChannelResolutionRequest(ApiModel):
    input: str = Field(min_length=1, max_length=2_048)


class ChannelLookup(ApiModel):
    parameter: Literal["id", "forHandle", "forUsername", "search"]
    value: str


class ChannelResolutionResponse(ApiModel):
    kind: Literal["channel_id", "handle", "legacy_username", "ambiguous_name"]
    normalized: str
    lookup: ChannelLookup
    requires_search: bool


class VideoResolutionRequest(ApiModel):
    input: str = Field(min_length=1, max_length=2_048)


class VideoResolutionResponse(ApiModel):
    kind: Literal["video_id", "watch_url", "short_url"]
    normalized: str


class ChannelSourceConfig(ApiModel):
    input: str = Field(min_length=1, max_length=2_048)
    includeComments: bool = False
    # New channel requests use the all-content flags.  The numeric fields remain
    # accepted for older API clients and stored requests.
    collectAllVideos: bool = False
    collectAllComments: bool = False
    maxVideos: int = Field(default=50, ge=1, le=5_000)
    maxCommentPagesPerVideo: int = Field(default=1, ge=1, le=100)


class KeywordSourceConfig(ApiModel):
    query: str = Field(min_length=1, max_length=500)
    publishedAfter: datetime | None = None
    publishedBefore: datetime | None = None
    regionCode: str | None = Field(default=None, min_length=2, max_length=2)
    relevanceLanguage: str | None = Field(default=None, min_length=2, max_length=16)
    order: Literal["date", "relevance", "viewCount"] = "date"
    maxPagesPerRun: int = Field(default=1, ge=1, le=100)
    includeComments: bool = False
    collectAllComments: bool = False
    maxCommentPagesPerVideo: int = Field(default=1, ge=1, le=100)

    @model_validator(mode="after")
    def validate_window(self) -> "KeywordSourceConfig":
        if self.publishedAfter and self.publishedBefore and self.publishedAfter > self.publishedBefore:
            raise ValueError("publishedAfter must be earlier than publishedBefore")
        return self


class VideoSourceConfig(ApiModel):
    """One public YouTube video, identified without making a network request."""

    input: str = Field(min_length=1, max_length=2_048)
    includeComments: bool = False
    collectAllComments: bool = False
    maxCommentPagesPerVideo: int = Field(default=1, ge=1, le=100)


SourceConfig = ChannelSourceConfig | KeywordSourceConfig | VideoSourceConfig


def parse_source_config(source_type: SourceType, raw_config: dict[str, Any] | BaseModel) -> SourceConfig:
    """Validate a config using its source type instead of guessing from overlapping fields."""

    payload = raw_config.model_dump(mode="python") if isinstance(raw_config, BaseModel) else raw_config
    config_model = {
        SourceType.CHANNEL: ChannelSourceConfig,
        SourceType.KEYWORD: KeywordSourceConfig,
        SourceType.VIDEO: VideoSourceConfig,
    }[source_type]
    return config_model.model_validate(payload)


class ChannelCollectionSourceCreate(ApiModel):
    type: Literal[SourceType.CHANNEL] = SourceType.CHANNEL
    config: ChannelSourceConfig


class KeywordCollectionSourceCreate(ApiModel):
    type: Literal[SourceType.KEYWORD] = SourceType.KEYWORD
    config: KeywordSourceConfig


class VideoCollectionSourceCreate(ApiModel):
    type: Literal[SourceType.VIDEO] = SourceType.VIDEO
    config: VideoSourceConfig


CollectionSourceCreate: TypeAlias = Annotated[
    ChannelCollectionSourceCreate | KeywordCollectionSourceCreate | VideoCollectionSourceCreate,
    Field(discriminator="type"),
]


class CollectionSourceUpdate(ApiModel):
    enabled: bool | None = None
    config: dict[str, Any] | None = None
    nextRunAt: datetime | None = None


class JobProgress(ApiModel):
    completed: int = Field(ge=0)
    total: int | None = Field(default=None, ge=0)
    unit: Literal["sources", "pages", "videos", "comments"] = "sources"


class PartialError(ApiModel):
    scope: Literal["channel", "video", "comment", "source"]
    sourceId: str
    code: str
    retryable: bool
    message: str | None = None


class JobStatus(ApiModel):
    id: str
    state: JobState
    currentStage: str
    progress: JobProgress
    # These two values are retained independently in the job checkpoint, so a
    # completed card can state what happened to video details and comments.
    videoProgress: JobProgress | None = None
    commentProgress: JobProgress | None = None
    pauseReason: str | None = None
    quotaBucket: QuotaBucket | None = None
    resumeAt: datetime | None = None
    resumeIsAutomatic: bool = False
    partialErrors: list[PartialError] = Field(default_factory=list)


class CollectionSourceBase(ApiModel):
    """A user's visible subscription to a shared collection target.

    ``id`` is deliberately the subscription ID, not the worker-facing legacy
    source ID. ``targetId`` remains the stable ID for shared public data and
    target-level job coalescing.
    """

    id: str
    enabled: bool
    nextRunAt: datetime | None = None
    # These fields are optional while a database is being migrated from the legacy
    # source-only model.  They let clients render one shared collection card.
    targetId: str | None = None
    canonicalKey: str | None = None
    coverage: dict[str, Any] = Field(default_factory=dict)
    lastCompletedAt: datetime | None = None
    latestJob: JobStatus | None = None


class TargetPinUpdate(ApiModel):
    enabled: bool = True
    intervalMinutes: int = Field(default=360, ge=15, le=10_080)


class TargetPin(ApiModel):
    targetId: str
    enabled: bool
    intervalMinutes: int
    nextRunAt: datetime
    lastDispatchedAt: datetime | None = None


class ExploreChannel(ApiModel):
    youtubeChannelId: str
    handle: str | None = None
    title: str | None = None
    description: str | None = None
    thumbnailUrl: str | None = None
    subscriberCount: int | None = Field(default=None, ge=0)
    viewCount: int | None = Field(default=None, ge=0)
    youtubeVideoCount: int | None = Field(default=None, ge=0)
    hiddenSubscriberCount: bool | None = None
    videoCount: int = Field(ge=0)
    commentCount: int = Field(ge=0)
    youtubeCommentCount: int = Field(default=0, ge=0)
    videoCollectionRate: int = Field(default=0, ge=0, le=100)
    commentCollectionRate: int = Field(default=0, ge=0, le=100)
    lastFetchedAt: datetime | None = None
    targetId: str | None = None
    pin: TargetPin | None = None


class ExploreResponse(ApiModel):
    channels: list[ExploreChannel] = Field(default_factory=list)
    videos: list["CollectedVideo"] = Field(default_factory=list)


class ChannelSubscriberSnapshot(ApiModel):
    fetchedAt: datetime
    subscriberCount: int | None = Field(default=None, ge=0)
    hiddenSubscriberCount: bool | None = None


class SearchVideoResult(ApiModel):
    video: "CollectedVideo"
    score: float = Field(ge=0, le=1)
    matchedFields: list[str] = Field(default_factory=list)


class SearchCommentResult(ApiModel):
    comment: "CollectedComment"
    video: "CollectedVideo"
    channelTitle: str | None = None
    score: float = Field(ge=0, le=1)
    matchedFields: list[str] = Field(default_factory=list)


class UnifiedSearchResponse(ApiModel):
    query: str
    videos: list[SearchVideoResult] = Field(default_factory=list)
    comments: list[SearchCommentResult] = Field(default_factory=list)


class ChannelCollectionSource(CollectionSourceBase):
    type: Literal[SourceType.CHANNEL] = SourceType.CHANNEL
    config: ChannelSourceConfig


class KeywordCollectionSource(CollectionSourceBase):
    type: Literal[SourceType.KEYWORD] = SourceType.KEYWORD
    config: KeywordSourceConfig


class VideoCollectionSource(CollectionSourceBase):
    type: Literal[SourceType.VIDEO] = SourceType.VIDEO
    config: VideoSourceConfig


CollectionSource: TypeAlias = Annotated[
    ChannelCollectionSource | KeywordCollectionSource | VideoCollectionSource,
    Field(discriminator="type"),
]


class JobCreate(ApiModel):
    include_comments: bool = False
    max_videos: int | None = Field(default=None, ge=1, le=5_000)
    max_comments_per_video: int | None = Field(default=None, ge=1, le=100)


class ChannelCollectionRequestCreate(ChannelCollectionSourceCreate):
    """Submit a channel collection intent to the shared target coordinator."""

    forceRefresh: bool = False


class KeywordCollectionRequestCreate(KeywordCollectionSourceCreate):
    """Submit a keyword collection intent to the shared target coordinator."""

    forceRefresh: bool = False


class VideoCollectionRequestCreate(VideoCollectionSourceCreate):
    """Submit a direct-video collection intent to the shared target coordinator."""

    forceRefresh: bool = False


CollectionRequestCreate: TypeAlias = Annotated[
    ChannelCollectionRequestCreate | KeywordCollectionRequestCreate | VideoCollectionRequestCreate,
    Field(discriminator="type"),
]


class CollectionRequestResponse(ApiModel):
    """Outcome of an atomic, target-aware collection submission."""

    id: str
    disposition: Literal["cached", "joined", "queued", "successor_queued"]
    targetId: str
    source: CollectionSource
    job: JobStatus | None = None


class VideoStatistics(ApiModel):
    viewCount: int = Field(default=0, ge=0)
    likeCount: int = Field(default=0, ge=0)
    commentCount: int = Field(default=0, ge=0)


class CollectedVideo(ApiModel):
    """Public YouTube data persisted for a collection source."""

    id: str
    channelId: str | None = None
    title: str | None = None
    description: str | None = None
    publishedAt: datetime | None = None
    durationSeconds: int | None = Field(default=None, ge=0)
    privacyStatus: str | None = None
    madeForKids: bool | None = None
    statistics: VideoStatistics = Field(default_factory=VideoStatistics)
    fetchedAt: datetime


class CollectedComment(ApiModel):
    id: str
    videoId: str
    parentCommentId: str | None = None
    threadId: str | None = None
    text: str | None = None
    likeCount: int = Field(default=0, ge=0)
    publishedAt: datetime | None = None
    updatedAt: datetime | None = None
    fetchedAt: datetime
    authorChannelId: str | None = None
    authorName: str | None = None


class TopWord(ApiModel):
    word: str
    count: int = Field(ge=1)


class CommentSummary(ApiModel):
    total: int = Field(default=0, ge=0)
    latestPublishedAt: datetime | None = None
    topWords: list[TopWord] = Field(default_factory=list)


class AnalysisSummary(ApiModel):
    videoCount: int = Field(default=0, ge=0)
    commentCount: int = Field(default=0, ge=0)
    latestVideoPublishedAt: datetime | None = None
    latestCommentPublishedAt: datetime | None = None
    topWords: list[TopWord] = Field(default_factory=list)
    generatedAt: datetime


class SourceResultsResponse(ApiModel):
    source: CollectionSource
    latestJob: JobStatus | None = None
    videos: list[CollectedVideo] = Field(default_factory=list)
    commentSummary: CommentSummary = Field(default_factory=CommentSummary)
    analysis: AnalysisSummary


class VideoCommentsResponse(ApiModel):
    video: CollectedVideo
    comments: list[CollectedComment] = Field(default_factory=list)
    summary: CommentSummary = Field(default_factory=CommentSummary)


class AuthorCommentResult(ApiModel):
    comment: CollectedComment
    video: CollectedVideo
    channelTitle: str | None = None


class CommentDetailResponse(ApiModel):
    comment: CollectedComment
    video: CollectedVideo
    authorComments: list[AuthorCommentResult] = Field(default_factory=list)


class JobStateChange(ApiModel):
    state: JobState
    current_stage: str | None = Field(default=None, min_length=1, max_length=100)
    progress_completed: int | None = Field(default=None, ge=0)
    progress_total: int | None = Field(default=None, ge=0)
    progress_unit: Literal["sources", "pages", "videos", "comments"] | None = None
    pause_reason: str | None = Field(default=None, max_length=500)
    quota_bucket: QuotaBucket | None = None
    resume_at: datetime | None = None
    resume_is_automatic: bool | None = None
    checkpoint: dict[str, Any] | None = None
    partial_errors: list[PartialError] | None = None
