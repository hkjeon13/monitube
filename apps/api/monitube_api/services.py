"""Application services that keep FastAPI routes independent of persistence."""

from __future__ import annotations

import hashlib

from .channel_resolution import resolve_channel_input
from .contracts import (
    ChannelLookup,
    ChannelCollectionSource,
    ChannelResolutionResponse,
    ChannelSubscriberSnapshot,
    AnalysisSummary,
    CollectedComment,
    CollectedVideo,
    AuthorCommentResult,
    CommentDetailResponse,
    CommentSummary,
    CollectionSource,
    CollectionSourceCreate,
    CollectionSourceUpdate,
    CollectionRequestCreate,
    CollectionRequestResponse,
    ExploreResponse,
    SearchCommentResult,
    SearchVideoResult,
    TargetPin,
    TargetPinUpdate,
    JobCreate,
    JobProgress,
    JobStateChange,
    JobStatus,
    KeywordCollectionSource,
    PartialError,
    SourceResultsResponse,
    UnifiedSearchResponse,
    SourceConfig,
    VideoCommentsResponse,
    VideoStatistics,
    VideoCollectionSource,
    VideoSourceConfig,
    parse_source_config,
)
from .domain import CollectionSubmission, CommentRecord, JobRecord, SourceRecord, SourceType, VideoRecord
from .repositories import CollectionRepository
from .video_resolution import resolve_video_input


def _source_contract(record: SourceRecord) -> CollectionSource:
    config = parse_source_config(record.type, record.config)
    shared = {
        "id": record.id,
        "enabled": record.enabled,
        "nextRunAt": record.next_run_at,
        "targetId": record.target_id,
        "canonicalKey": record.canonical_key,
        "coverage": record.coverage,
        "lastCompletedAt": record.last_completed_at,
        "latestJob": _job_contract(record.latest_job) if record.latest_job else None,
    }
    if record.type is SourceType.CHANNEL:
        return ChannelCollectionSource(type=SourceType.CHANNEL, config=config, **shared)
    if record.type is SourceType.KEYWORD:
        return KeywordCollectionSource(type=SourceType.KEYWORD, config=config, **shared)
    return VideoCollectionSource(type=SourceType.VIDEO, config=config, **shared)


def _job_contract(record: JobRecord) -> JobStatus:
    details = record.checkpoint.get("phaseProgress") if isinstance(record.checkpoint, dict) else None

    def phase(name: str, unit: str) -> JobProgress | None:
        item = details.get(name) if isinstance(details, dict) else None
        if not isinstance(item, dict):
            # Older jobs did not retain separate phases. Preserve the one
            # durable aggregate instead of inventing a misleading total.
            if record.progress_unit != unit:
                return None
            return JobProgress(completed=record.progress_completed, total=record.progress_total, unit=unit)
        total = item.get("total")
        return JobProgress(
            completed=max(0, int(item.get("completed") or 0)),
            total=max(0, int(total)) if total is not None else None,
            unit=unit,
        )

    return JobStatus(
        id=record.id,
        state=record.state,
        currentStage=record.current_stage,
        progress=JobProgress(completed=record.progress_completed, total=record.progress_total, unit=record.progress_unit),
        videoProgress=phase("videos", "videos"),
        commentProgress=phase("comments", "comments"),
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
        authorChannelId=record.author_channel_id,
        authorName=record.author_display_name,
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

    _DEFAULT_CHANNEL_REFRESH_MINUTES = 360

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
        if source_type is SourceType.CHANNEL:
            return config.model_copy(update={"input": resolve_channel_input(config.input).normalized})
        return config

    @staticmethod
    def _canonical_target(source_type: SourceType, config: SourceConfig) -> tuple[str, list[tuple[str, str]]]:
        """Build a stable target key while excluding requested collection breadth.

        Handles and custom URLs are aliases rather than permanent identities.  Their
        provisional key is intentionally replaced with ``channel:<UC…>`` once the
        worker resolves the public channel ID.
        """

        serialized = config.model_dump(mode="json", exclude_none=True)
        if source_type is SourceType.CHANNEL:
            resolution = resolve_channel_input(str(serialized["input"]))
            normalized = resolution.normalized
            lowered = normalized.casefold()
            if resolution.kind.value == "channel_id":
                return f"channel:{normalized}", [("channel_id", normalized), ("input", normalized)]
            return (
                f"channel:{resolution.kind.value}:{lowered}",
                [(resolution.kind.value, lowered), ("input", lowered)],
            )
        if source_type is SourceType.VIDEO:
            video_id = str(serialized["input"])
            return f"video:{video_id}", [("video_id", video_id), ("input", video_id)]

        # A keyword target identity includes only search semantics.  Limits and
        # comment depth are coverage, so different user requests still share it.
        # Keep this material deliberately rendering-independent: the migration uses
        # the exact same unit-separator contract when it backfills legacy targets.
        fingerprint_material = "\x1f".join(
            (
                " ".join(str(serialized.get("query") or "").split()).lower(),
                str(serialized.get("publishedAfter") or ""),
                str(serialized.get("publishedBefore") or ""),
                str(serialized.get("regionCode") or "").upper(),
                str(serialized.get("relevanceLanguage") or "").lower(),
                str(serialized.get("order") or "date"),
            )
        )
        fingerprint = hashlib.sha256(fingerprint_material.encode("utf-8")).hexdigest()
        return f"keyword:{fingerprint}", [("keyword", fingerprint)]

    @staticmethod
    def _submission_contract(submission: CollectionSubmission) -> CollectionRequestResponse:
        return CollectionRequestResponse(
            id=submission.request.id,
            disposition=submission.disposition,
            targetId=submission.target.id,
            source=_source_contract(submission.source),
            job=_job_contract(submission.job) if submission.job else None,
        )

    def create_source(self, request: CollectionSourceCreate, *, owner_id: str | None = None) -> CollectionSource:
        config = self._canonical_config(request.type, request.config)
        return _source_contract(
            self.repository.create_source(
                source_type=request.type,
                config=config.model_dump(mode="json", exclude_none=True),
                owner_id=owner_id,
            )
        )

    def submit_collection_request(
        self, request: CollectionRequestCreate, *, idempotency_key: str | None = None
    ) -> CollectionRequestResponse:
        config = self._canonical_config(request.type, request.config)
        canonical_key, aliases = self._canonical_target(request.type, config)
        submission = self.repository.submit_collection_request(
            source_type=request.type,
            config=config.model_dump(mode="json", exclude_none=True),
            canonical_key=canonical_key,
            aliases=aliases,
            force_refresh=request.forceRefresh,
            idempotency_key=idempotency_key.strip() if idempotency_key else None,
            runtime_config_id=self.runtime_config_id,
        )
        # Channels are subscriptions by default.  The initial request still starts
        # immediately; this durable target-level pin schedules later refreshes of
        # the same canonical channel without creating duplicate user requests.
        if request.type is SourceType.CHANNEL:
            self.repository.set_target_pin(
                target_id=submission.target.id,
                enabled=True,
                interval_minutes=self._DEFAULT_CHANNEL_REFRESH_MINUTES,
            )
        return self._submission_contract(submission)

    def list_sources(self, *, owner_id: str | None = None) -> list[CollectionSource]:
        return [_source_contract(record) for record in self.repository.list_sources(owner_id=owner_id)]

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

    def list_source_jobs(self, source_id: str, *, limit: int = 20) -> list[JobStatus]:
        return [_job_contract(record) for record in self.repository.list_jobs_for_source(source_id, limit=limit)]

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

    def get_comment_detail(self, comment_id: str) -> CommentDetailResponse:
        result = self.repository.get_comment_detail(comment_id)
        return CommentDetailResponse(
            comment=_comment_contract(result["comment"]), video=_video_contract(result["video"]),
            authorComments=[AuthorCommentResult(
                comment=_comment_contract(item["comment"]), video=_video_contract(item["video"]),
                channelTitle=item.get("channel_title"),
            ) for item in result["author_comments"]],
        )

    @staticmethod
    def _pin_contract(pin: dict[str, object]) -> TargetPin:
        return TargetPin(
            targetId=str(pin["target_id"]), enabled=bool(pin["enabled"]),
            intervalMinutes=int(pin["interval_minutes"]), nextRunAt=pin["next_run_at"],
            lastDispatchedAt=pin.get("last_dispatched_at"),
        )

    def set_target_pin(self, target_id: str, request: TargetPinUpdate) -> TargetPin:
        return self._pin_contract(self.repository.set_target_pin(
            target_id=target_id, enabled=request.enabled, interval_minutes=request.intervalMinutes,
        ))

    def get_target_pin(self, target_id: str) -> TargetPin | None:
        pin = self.repository.get_target_pin(target_id=target_id)
        return self._pin_contract(pin) if pin else None

    def explore(self, *, channel_id: str | None = None) -> ExploreResponse:
        result = self.repository.list_explore(channel_id=channel_id)
        channels = []
        for channel in result["channels"]:
            pin = channel.pop("pin", None)
            channels.append({**channel, "pin": self._pin_contract(pin) if pin else None})
        return ExploreResponse(channels=channels, videos=[_video_contract(video) for video in result["videos"]])

    def channel_subscriber_history(self, youtube_channel_id: str) -> list[ChannelSubscriberSnapshot]:
        return [ChannelSubscriberSnapshot.model_validate(item) for item in self.repository.list_channel_subscriber_history(youtube_channel_id=youtube_channel_id)]

    def search_collected(self, query: str, *, limit: int = 20) -> UnifiedSearchResponse:
        result = self.repository.search_collected(query=query, limit=limit)
        return UnifiedSearchResponse(
            query=query,
            videos=[
                SearchVideoResult(
                    video=_video_contract(item["video"]), score=item["score"],
                    matchedFields=item["matched_fields"],
                )
                for item in result["videos"]
            ],
            comments=[
                SearchCommentResult(
                    comment=_comment_contract(item["comment"]), video=_video_contract(item["video"]),
                    channelTitle=item.get("channel_title"), score=item["score"],
                    matchedFields=item["matched_fields"],
                )
                for item in result["comments"]
            ],
        )

    def change_job_state(self, job_id: str, request: JobStateChange) -> JobStatus:
        changes = request.model_dump(exclude={"state"}, exclude_none=True)
        if "partial_errors" in changes:
            changes["partial_errors"] = [error.model_dump(exclude_none=True) for error in request.partial_errors or []]
        return _job_contract(self.repository.transition_job(job_id, request.state, **changes))
