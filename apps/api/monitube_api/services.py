"""Application services that keep FastAPI routes independent of persistence."""

from __future__ import annotations

from .channel_resolution import resolve_channel_input
from .contracts import (
    ChannelLookup,
    ChannelCollectionSource,
    ChannelResolutionResponse,
    AnalysisSummary,
    CollectedComment,
    CollectedVideo,
    CommentSummary,
    CollectionSource,
    CollectionSourceCreate,
    CollectionSourceUpdate,
    JobCreate,
    JobProgress,
    JobStateChange,
    JobStatus,
    KeywordCollectionSource,
    PartialError,
    SourceResultsResponse,
    SourceConfig,
    VideoCommentsResponse,
    VideoStatistics,
    VideoCollectionSource,
    VideoSourceConfig,
    parse_source_config,
)
from .domain import CommentRecord, JobRecord, SourceRecord, SourceType, VideoRecord
from .repositories import CollectionRepository
from .video_resolution import resolve_video_input


def _source_contract(record: SourceRecord) -> CollectionSource:
    config = parse_source_config(record.type, record.config)
    shared = {"id": record.id, "enabled": record.enabled, "nextRunAt": record.next_run_at}
    if record.type is SourceType.CHANNEL:
        return ChannelCollectionSource(type=SourceType.CHANNEL, config=config, **shared)
    if record.type is SourceType.KEYWORD:
        return KeywordCollectionSource(type=SourceType.KEYWORD, config=config, **shared)
    return VideoCollectionSource(type=SourceType.VIDEO, config=config, **shared)


def _job_contract(record: JobRecord) -> JobStatus:
    return JobStatus(
        id=record.id,
        state=record.state,
        currentStage=record.current_stage,
        progress=JobProgress(completed=record.progress_completed, total=record.progress_total, unit=record.progress_unit),
        pauseReason=record.pause_reason,
        quotaBucket=record.quota_bucket,
        resumeAt=record.resume_at,
        resumeIsAutomatic=record.resume_is_automatic,
        partialErrors=[PartialError.model_validate(item) for item in record.partial_errors],
    )


def _video_contract(record: VideoRecord) -> CollectedVideo:
    return CollectedVideo(
        id=record.youtube_video_id,
        channelId=record.youtube_channel_id,
        title=record.title,
        description=record.description,
        publishedAt=record.published_at,
        durationSeconds=record.duration_seconds,
        privacyStatus=record.privacy_status,
        madeForKids=record.made_for_kids,
        statistics=VideoStatistics(
            viewCount=record.statistics.get("viewCount", 0),
            likeCount=record.statistics.get("likeCount", 0),
            commentCount=record.statistics.get("commentCount", 0),
        ),
        fetchedAt=record.source_fetched_at,
    )


def _comment_contract(record: CommentRecord) -> CollectedComment:
    return CollectedComment(
        id=record.youtube_comment_id,
        videoId=record.youtube_video_id,
        parentCommentId=record.youtube_parent_comment_id,
        threadId=record.youtube_thread_id,
        text=record.text_display,
        likeCount=record.like_count,
        publishedAt=record.published_at,
        updatedAt=record.updated_at,
        fetchedAt=record.source_fetched_at,
    )


def _comment_summary(summary: dict[str, object]) -> CommentSummary:
    return CommentSummary(
        total=int(summary.get("commentCount", 0)),
        latestPublishedAt=summary.get("latestCommentPublishedAt"),
        topWords=summary.get("topWords", []),
    )


class CollectionService:
    """Source and job operations using only server-managed upstream credentials.

    Request contracts contain collection targets and limits only. The service owns
    upstream credential configuration and never accepts caller-selected credentials.
    """

    def __init__(self, repository: CollectionRepository, *, runtime_config_id: str | None = None) -> None:
        self.repository = repository
        self.runtime_config_id = runtime_config_id

    def resolve_channel(self, input_value: str) -> ChannelResolutionResponse:
        resolution = resolve_channel_input(input_value)
        return ChannelResolutionResponse(
            kind=resolution.kind.value,
            normalized=resolution.normalized,
            lookup=ChannelLookup(parameter=resolution.lookup_parameter, value=resolution.normalized),
            requires_search=resolution.requires_search,
        )

    @staticmethod
    def _canonical_config(source_type: SourceType, raw_config: SourceConfig | dict[str, object]) -> SourceConfig:
        config = parse_source_config(source_type, raw_config)
        if isinstance(config, VideoSourceConfig):
            return config.model_copy(update={"input": resolve_video_input(config.input).normalized})
        return config

    def create_source(self, request: CollectionSourceCreate) -> CollectionSource:
        config = self._canonical_config(request.type, request.config)
        return _source_contract(
            self.repository.create_source(
                source_type=request.type,
                config=config.model_dump(mode="json", exclude_none=True),
            )
        )

    def list_sources(self) -> list[CollectionSource]:
        return [_source_contract(record) for record in self.repository.list_sources()]

    def get_source(self, source_id: str) -> CollectionSource:
        return _source_contract(self.repository.get_source(source_id))

    def update_source(self, source_id: str, request: CollectionSourceUpdate) -> CollectionSource:
        existing = self.repository.get_source(source_id)
        changes: dict[str, object] = {}
        if request.enabled is not None:
            changes["enabled"] = request.enabled
        if request.config is not None:
            config = self._canonical_config(existing.type, request.config)
            changes["config"] = config.model_dump(mode="json", exclude_none=True)
        if request.nextRunAt is not None:
            changes["next_run_at"] = request.nextRunAt
        if not changes:
            return _source_contract(existing)
        return _source_contract(self.repository.update_source(source_id, **changes))

    def delete_source(self, source_id: str) -> None:
        self.repository.delete_source(source_id)

    def create_job(self, source_id: str, request: JobCreate) -> JobStatus:
        record = self.repository.create_job(
            source_id=source_id,
            include_comments=request.include_comments,
            max_videos=request.max_videos,
            max_comments_per_video=request.max_comments_per_video,
            runtime_config_id=self.runtime_config_id,
        )
        return _job_contract(record)

    def get_job(self, job_id: str) -> JobStatus:
        return _job_contract(self.repository.get_job(job_id))

    def get_source_results(self, source_id: str) -> SourceResultsResponse:
        result = self.repository.get_source_results(source_id)
        source = _source_contract(result["source"])
        latest_job = _job_contract(result["latest_job"]) if result.get("latest_job") else None
        summary = result["analysis"]
        return SourceResultsResponse(
            source=source,
            latestJob=latest_job,
            videos=[_video_contract(video) for video in result["videos"]],
            commentSummary=_comment_summary(summary),
            analysis=AnalysisSummary.model_validate(summary),
        )

    def get_video_comments(self, video_id: str) -> VideoCommentsResponse:
        result = self.repository.get_video_comments(video_id)
        return VideoCommentsResponse(
            video=_video_contract(result["video"]),
            comments=[_comment_contract(comment) for comment in result["comments"]],
            summary=_comment_summary(result["summary"]),
        )

    def change_job_state(self, job_id: str, request: JobStateChange) -> JobStatus:
        changes = request.model_dump(exclude={"state"}, exclude_none=True)
        if "partial_errors" in changes:
            changes["partial_errors"] = [error.model_dump(exclude_none=True) for error in request.partial_errors or []]
        return _job_contract(self.repository.transition_job(job_id, request.state, **changes))
