"""Application services that keep FastAPI routes independent of persistence."""

from __future__ import annotations

import hashlib
from typing import Literal

from .channel_resolution import resolve_channel_input
from .cache import DerivedCache
from .fuzzy_search import normalize_search_text
from .contracts import (
    ActiveParentJob,
    ActiveParentJobsResponse,
    ChannelLookup,
    ChannelCollectionSource,
    ChannelResolutionResponse,
    ChannelSubscriberSnapshot,
    AnalysisSummary,
    CollectedComment,
    CollectedVideo,
    AuthorCommentResult,
    CommentRepliesResponse,
    CommentDetailResponse,
    CommentThreadItem,
    CommentSummary,
    CollectionSource,
    CollectionSourceCreate,
    CollectionSourceUpdate,
    CollectionRequestCreate,
    CollectionRequestResponse,
    ExploreResponse,
    ExploreChannelsResponse,
    ExploreVideosPageResponse,
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
    RecentJobFailure,
    RecentJobFailuresResponse,
    SourceResultsResponse,
    SourceOverviewResponse,
    SourceOverviewSummary,
    SourceTopVideos,
    SourceVideosPageResponse,
    UnifiedSearchResponse,
    SourceConfig,
    VideoCommentsResponse,
    VideoCommentThreadsResponse,
    VideoStatistics,
    VideoCollectionSource,
    VideoSourceConfig,
    parse_source_config,
)
from .domain import CollectionSubmission, CommentRecord, JobRecord, SourceRecord, SourceType, VideoRecord
from .repositories import CollectionRepository
from .video_resolution import resolve_video_input


class InvalidSearchQueryError(ValueError):
    """A search query that becomes too short after normalization."""


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


def _job_contract(
    record: JobRecord,
    *,
    public_source_id: str | None = None,
) -> JobStatus:
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

    partial_errors = [PartialError.model_validate(item) for item in record.partial_errors]
    if public_source_id is not None:
        partial_errors = [
            error.model_copy(update={"sourceId": public_source_id})
            for error in partial_errors
        ]

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
        partialErrors=partial_errors,
    )


def _source_label(
    source_type: SourceType,
    config: dict[str, object],
    canonical_key: str | None,
) -> str:
    """Choose the stable user-entered identifier without exposing worker IDs."""

    key = "query" if source_type is SourceType.KEYWORD else "input"
    configured = config.get(key)
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    if canonical_key and canonical_key.strip():
        return canonical_key.strip()
    return source_type.value


def _safe_failure_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _structured_failure(
    errors: object,
) -> tuple[str | None, str | None, bool | None]:
    """Extract one structured error without inferring retry safety from text."""

    if not isinstance(errors, list):
        return None, None, None
    candidates = [item for item in errors if isinstance(item, dict)]
    representative = next(
        (
            item
            for item in candidates
            if _safe_failure_text(item.get("message"))
            or _safe_failure_text(item.get("code"))
        ),
        None,
    )
    if representative is None:
        return None, None, None
    message = _safe_failure_text(representative.get("message"))
    code = _safe_failure_text(representative.get("code"))
    retryable_value = representative.get("retryable")
    retryable = retryable_value if isinstance(retryable_value, bool) else None
    return message, code, retryable


def _failure_details(item: dict[str, object]) -> tuple[str, str | None, bool | None]:
    """Prefer a failed fanout child, then the parent, with an explicit fallback."""

    child_pause = _safe_failure_text(item.get("representative_child_pause_reason"))
    if child_pause:
        # Fatal runner exceptions write pause_reason while partial_errors may
        # still contain unrelated warnings from an earlier collection phase.
        return child_pause, None, None
    child_message, child_code, child_retryable = _structured_failure(
        item.get("representative_child_partial_errors")
    )
    if child_message or child_code:
        return (
            child_message or child_code or "Collection child failed.",
            child_code,
            child_retryable,
        )

    job = item["job"]
    if not isinstance(job, JobRecord):
        return "Collection failed without a recorded reason.", None, None
    parent_pause = _safe_failure_text(job.pause_reason)
    if parent_pause:
        return parent_pause, None, None
    parent_message, parent_code, parent_retryable = _structured_failure(job.partial_errors)
    return (
        parent_message
        or parent_code
        or "Collection failed without a recorded reason.",
        parent_code,
        parent_retryable,
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

    def __init__(
        self,
        repository: CollectionRepository,
        *,
        runtime_config_id: str | None = None,
        derived_cache: DerivedCache | None = None,
    ) -> None:
        self.repository = repository
        self.runtime_config_id = runtime_config_id
        self.derived_cache = derived_cache

    def _explore_cache_generation(self, owner_id: str) -> int | str:
        reader = getattr(self.repository, "get_owner_explore_generation", None)
        if callable(reader):
            return reader(owner_id=owner_id)
        if self.derived_cache:
            return self.derived_cache.owner_generation(owner_id)
        return 0

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
        """Compatibility endpoint that creates a subscription, never a worker source.

        Older clients still use ``POST /sources``.  Route that intent through the
        same canonical target coordinator as ``POST /collection-requests`` so it
        cannot create an owner-bound physical source or bypass shared coverage.
        """
        config = self._canonical_config(request.type, request.config)
        canonical_key, aliases = self._canonical_target(request.type, config)
        submission = self.repository.submit_collection_request(
            source_type=request.type,
            config=config.model_dump(mode="json", exclude_none=True),
            canonical_key=canonical_key,
            aliases=aliases,
            force_refresh=False,
            idempotency_key=None,
            owner_id=owner_id,
            runtime_config_id=self.runtime_config_id,
        )
        return _source_contract(submission.source)

    def submit_collection_request(
        self,
        request: CollectionRequestCreate,
        *,
        owner_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> CollectionRequestResponse:
        """Submit a user subscription intent against a shared canonical target.

        ``owner_id`` identifies the caller's subscription only.  The target, its
        worker source, jobs, and public YouTube data remain shared across users.
        The repository performs target lookup plus subscription creation in one
        transaction, which makes duplicate requests safe without a post-hoc
        ownership assignment.
        """
        config = self._canonical_config(request.type, request.config)
        canonical_key, aliases = self._canonical_target(request.type, config)
        submission = self.repository.submit_collection_request(
            source_type=request.type,
            config=config.model_dump(mode="json", exclude_none=True),
            canonical_key=canonical_key,
            aliases=aliases,
            force_refresh=request.forceRefresh,
            idempotency_key=idempotency_key.strip() if idempotency_key else None,
            owner_id=owner_id,
            runtime_config_id=self.runtime_config_id,
        )
        return self._submission_contract(submission)

    def list_sources(self, *, owner_id: str | None = None) -> list[CollectionSource]:
        return [_source_contract(record) for record in self.repository.list_sources(owner_id=owner_id)]

    def get_source(self, source_id: str, *, owner_id: str | None = None) -> CollectionSource:
        return _source_contract(self.repository.get_source(source_id, owner_id=owner_id))

    def update_source(
        self, source_id: str, request: CollectionSourceUpdate, *, owner_id: str | None = None
    ) -> CollectionSource:
        existing = self.repository.get_source(source_id, owner_id=owner_id)
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
        return _source_contract(self.repository.update_source(source_id, owner_id=owner_id, **changes))

    def delete_source(self, source_id: str, *, owner_id: str | None = None) -> None:
        self.repository.delete_source(source_id, owner_id=owner_id)

    def create_job(self, source_id: str, request: JobCreate, *, owner_id: str | None = None) -> JobStatus:
        record = self.repository.create_job(
            source_id=source_id,
            include_comments=request.include_comments,
            max_videos=request.max_videos,
            max_comments_per_video=request.max_comments_per_video,
            owner_id=owner_id,
            runtime_config_id=self.runtime_config_id,
        )
        return _job_contract(record)

    def get_job(self, job_id: str, *, owner_id: str | None = None) -> JobStatus:
        return _job_contract(self.repository.get_job(job_id, owner_id=owner_id))

    def list_source_jobs(
        self, source_id: str, *, owner_id: str | None = None, limit: int = 20
    ) -> list[JobStatus]:
        return [
            _job_contract(record)
            for record in self.repository.list_jobs_for_source(source_id, limit=limit, owner_id=owner_id)
        ]

    def list_active_parent_jobs(self, *, owner_id: str) -> ActiveParentJobsResponse:
        return ActiveParentJobsResponse(
            jobs=[
                ActiveParentJob(
                    sourceId=item["source_id"],
                    targetId=item.get("target_id"),
                    job=_job_contract(item["job"]),
                )
                for item in self.repository.list_active_parent_jobs(owner_id=owner_id)
            ]
        )

    def list_recent_failed_parent_jobs(
        self, *, owner_id: str, limit: int = 10
    ) -> RecentJobFailuresResponse:
        failures: list[RecentJobFailure] = []
        for item in self.repository.list_recent_failed_parent_jobs(
            owner_id=owner_id,
            limit=limit,
        ):
            reason, error_code, retryable = _failure_details(item)
            public_source_id = item["source_id"]
            failures.append(
                RecentJobFailure(
                    sourceId=public_source_id,
                    targetId=item.get("target_id"),
                    sourceType=item["source_type"],
                    sourceLabel=_source_label(
                        item["source_type"],
                        item.get("source_config") or {},
                        item.get("canonical_key"),
                    ),
                    failedAt=item["failed_at"],
                    reason=reason,
                    errorCode=error_code,
                    retryable=retryable,
                    failedChildCount=int(item.get("failed_child_count") or 0),
                    job=_job_contract(
                        item["job"],
                        public_source_id=public_source_id,
                    ),
                )
            )
        return RecentJobFailuresResponse(failures=failures)

    def get_source_results(self, source_id: str, *, owner_id: str | None = None) -> SourceResultsResponse:
        result = self.repository.get_source_results(source_id, owner_id=owner_id)
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

    def get_source_overview(
        self, source_id: str, *, owner_id: str | None = None
    ) -> SourceOverviewResponse:
        if self.derived_cache and self.derived_cache.enabled:
            # Resolve current ACL and the non-cached subscription DTO first.
            source_record = self.repository.get_source(source_id, owner_id=owner_id)
            version_reader = getattr(self.repository, "get_scope_data_version", None)
            if callable(version_reader):
                data_version = version_reader(
                    target_id=source_record.target_id,
                    source_id=source_record.id,
                )
                scope_id = source_record.target_id or f"source-{source_record.id}"
                cache_key = self.derived_cache.target_summary_key(scope_id, data_version)

                def load_derived() -> dict[str, object]:
                    loaded = self.repository.get_source_overview(source_id, owner_id=owner_id)
                    top = loaded.get("top_videos", {})
                    return {
                        "summary": SourceOverviewSummary.model_validate(
                            loaded["summary"]
                        ).model_dump(mode="json"),
                        "topVideos": SourceTopVideos(
                            views=[_video_contract(video) for video in top.get("views", [])],
                            likes=[_video_contract(video) for video in top.get("likes", [])],
                            comments=[_video_contract(video) for video in top.get("comments", [])],
                        ).model_dump(mode="json"),
                    }

                cached = self.derived_cache.get_or_load(
                    cache_key, load_derived, ttl_seconds=45
                )
                return SourceOverviewResponse(
                    source=_source_contract(source_record),
                    latestJob=(
                        _job_contract(source_record.latest_job)
                        if source_record.latest_job else None
                    ),
                    summary=SourceOverviewSummary.model_validate(cached["summary"]),
                    topVideos=SourceTopVideos.model_validate(cached["topVideos"]),
                )

        result = self.repository.get_source_overview(source_id, owner_id=owner_id)
        top_videos = result.get("top_videos", {})
        return SourceOverviewResponse(
            source=_source_contract(result["source"]),
            latestJob=_job_contract(result["latest_job"]) if result.get("latest_job") else None,
            summary=SourceOverviewSummary.model_validate(result["summary"]),
            topVideos=SourceTopVideos(
                views=[_video_contract(video) for video in top_videos.get("views", [])],
                likes=[_video_contract(video) for video in top_videos.get("likes", [])],
                comments=[_video_contract(video) for video in top_videos.get("comments", [])],
            ),
        )

    def get_source_videos_page(
        self,
        source_id: str,
        *,
        owner_id: str | None = None,
        cursor: str | None = None,
        limit: int = 60,
    ) -> SourceVideosPageResponse:
        result = self.repository.get_source_videos_page(
            source_id,
            owner_id=owner_id,
            cursor=cursor,
            limit=limit,
        )
        return SourceVideosPageResponse(
            videos=[_video_contract(video) for video in result["videos"]],
            nextCursor=result.get("next_cursor"),
            snapshotAt=result["snapshot_at"],
            total=result["total"],
        )

    def get_video_comments(self, video_id: str, *, owner_id: str | None = None) -> VideoCommentsResponse:
        result = self.repository.get_video_comments(video_id, owner_id=owner_id)
        return VideoCommentsResponse(
            video=_video_contract(result["video"]),
            comments=[_comment_contract(comment) for comment in result["comments"]],
            summary=_comment_summary(result["summary"]),
        )

    def get_video_comment_threads(
        self, video_id: str, *, owner_id: str | None = None, cursor: str | None = None,
        limit: int = 20, sort: Literal["newest", "oldest", "recommended"] = "newest"
    ) -> VideoCommentThreadsResponse:
        result = self.repository.get_video_comment_threads(
            video_id, owner_id=owner_id, cursor=cursor, limit=limit, sort=sort
        )
        return VideoCommentThreadsResponse(
            video=_video_contract(result["video"]),
            sort=sort,
            items=[
                CommentThreadItem(
                    comment=_comment_contract(item["comment"]),
                    repliesPreview=[_comment_contract(reply) for reply in item["replies_preview"]],
                    storedReplyCount=item["stored_reply_count"],
                )
                for item in result["items"]
            ],
            nextCursor=result.get("next_cursor"),
        )

    def get_comment_replies(
        self, comment_id: str, *, owner_id: str | None = None, cursor: str | None = None, limit: int = 20
    ) -> CommentRepliesResponse:
        result = self.repository.get_comment_replies(
            comment_id, owner_id=owner_id, cursor=cursor, limit=limit
        )
        return CommentRepliesResponse(
            comments=[_comment_contract(comment) for comment in result["comments"]],
            nextCursor=result.get("next_cursor"),
        )

    def get_comment_detail(self, comment_id: str, *, owner_id: str | None = None) -> CommentDetailResponse:
        result = self.repository.get_comment_detail(comment_id, owner_id=owner_id)
        return CommentDetailResponse(
            comment=_comment_contract(result["comment"]), video=_video_contract(result["video"]),
            parentComment=_comment_contract(result["parent_comment"]) if result.get("parent_comment") else None,
            storedReplyCount=result.get("stored_reply_count", len(result.get("replies", []))),
            replies=[_comment_contract(reply) for reply in result.get("replies", [])],
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

    def explore(
        self, *, owner_id: str | None = None, channel_id: str | None = None, offset: int = 0, limit: int = 60
    ) -> ExploreResponse:
        result = self.repository.list_explore(channel_id=channel_id, owner_id=owner_id, offset=offset, limit=limit)
        channels = []
        for channel in result["channels"]:
            pin = channel.pop("pin", None)
            channels.append({**channel, "pin": self._pin_contract(pin) if pin else None})
        return ExploreResponse(
            channels=channels,
            videos=[_video_contract(video) for video in result["videos"]],
            nextOffset=result.get("next_offset"),
        )

    def explore_channels(self, *, owner_id: str | None = None) -> ExploreChannelsResponse:
        def load() -> dict[str, object]:
            channels = []
            for item in self.repository.list_explore_channels(owner_id=owner_id):
                channel = dict(item)
                pin = channel.pop("pin", None)
                channels.append({**channel, "pin": self._pin_contract(pin) if pin else None})
            return ExploreChannelsResponse(channels=channels).model_dump(mode="json")

        if self.derived_cache and self.derived_cache.enabled and owner_id is not None:
            filter_hash = self.derived_cache.filter_hash({"kind": "channels"})
            key = self.derived_cache.owner_explore_key(
                owner_id,
                filter_hash,
                self._explore_cache_generation(owner_id),
            )
            return ExploreChannelsResponse.model_validate(
                self.derived_cache.get_or_load(key, load, ttl_seconds=45)
            )
        return ExploreChannelsResponse.model_validate(load())

    def explore_videos_page(
        self,
        *,
        owner_id: str | None = None,
        channel_id: str | None = None,
        cursor: str | None = None,
        limit: int = 60,
    ) -> ExploreVideosPageResponse:
        def load() -> dict[str, object]:
            result = self.repository.list_explore_videos_page(
                owner_id=owner_id,
                channel_id=channel_id,
                cursor=cursor,
                limit=limit,
            )
            return ExploreVideosPageResponse(
                videos=[_video_contract(video) for video in result["videos"]],
                nextCursor=result.get("next_cursor"),
                snapshotAt=result["snapshot_at"],
                total=result["total"],
            ).model_dump(mode="json")

        if self.derived_cache and self.derived_cache.enabled and owner_id is not None:
            filter_hash = self.derived_cache.filter_hash(
                {
                    "kind": "videos",
                    "channelId": channel_id,
                    "cursor": cursor,
                    "limit": limit,
                }
            )
            key = self.derived_cache.owner_explore_key(
                owner_id,
                filter_hash,
                self._explore_cache_generation(owner_id),
            )
            return ExploreVideosPageResponse.model_validate(
                self.derived_cache.get_or_load(key, load, ttl_seconds=45)
            )
        return ExploreVideosPageResponse.model_validate(load())

    def channel_subscriber_history(
        self, youtube_channel_id: str, *, owner_id: str | None = None
    ) -> list[ChannelSubscriberSnapshot]:
        return [
            ChannelSubscriberSnapshot.model_validate(item)
            for item in self.repository.list_channel_subscriber_history(
                youtube_channel_id=youtube_channel_id,
                owner_id=owner_id,
            )
        ]

    def search_collected(
        self, query: str, *, owner_id: str | None = None, limit: int = 20, scope: str = "all"
    ) -> UnifiedSearchResponse:
        normalized_query = normalize_search_text(query)
        if len(normalized_query) <= 1:
            raise InvalidSearchQueryError("Search query must contain at least two normalized characters")
        result = self.repository.search_collected(query=query, limit=limit, owner_id=owner_id, scope=scope)
        if len(normalized_query) == 2:
            # Two-character searches are deliberately prefix-only for bounded
            # video metadata fields; comment contains-search starts at 3 chars.
            result = {**result, "comments": []}
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
