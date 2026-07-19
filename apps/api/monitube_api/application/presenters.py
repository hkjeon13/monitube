"""Pure conversion from domain records to public API contracts."""

from ..contracts import (
    ChannelCollectionSource,
    CollectedComment,
    CollectedVideo,
    CollectionSource,
    CommentSummary,
    JobProgress,
    JobStatus,
    KeywordCollectionSource,
    PartialError,
    TargetPin,
    VideoCollectionSource,
    VideoStatistics,
    parse_source_config,
)
from ..domain import CommentRecord, JobRecord, SourceRecord, SourceType, VideoRecord


def job_contract(
    record: JobRecord,
    *,
    public_source_id: str | None = None,
) -> JobStatus:
    details = (
        record.checkpoint.get("phaseProgress")
        if isinstance(record.checkpoint, dict)
        else None
    )

    def phase(name: str, unit: str) -> JobProgress | None:
        item = details.get(name) if isinstance(details, dict) else None
        if not isinstance(item, dict):
            if record.progress_unit != unit:
                return None
            return JobProgress(
                completed=record.progress_completed,
                total=record.progress_total,
                unit=unit,
            )
        total = item.get("total")
        return JobProgress(
            completed=max(0, int(item.get("completed") or 0)),
            total=max(0, int(total)) if total is not None else None,
            unit=unit,
        )

    partial_errors = [
        PartialError.model_validate(item) for item in record.partial_errors
    ]
    if public_source_id is not None:
        partial_errors = [
            error.model_copy(update={"sourceId": public_source_id})
            for error in partial_errors
        ]

    return JobStatus(
        id=record.id,
        state=record.state,
        currentStage=record.current_stage,
        progress=JobProgress(
            completed=record.progress_completed,
            total=record.progress_total,
            unit=record.progress_unit,
        ),
        videoProgress=phase("videos", "videos"),
        commentProgress=phase("comments", "comments"),
        pauseReason=record.pause_reason,
        quotaBucket=record.quota_bucket,
        resumeAt=record.resume_at,
        resumeIsAutomatic=record.resume_is_automatic,
        partialErrors=partial_errors,
    )


def source_contract(record: SourceRecord) -> CollectionSource:
    config = parse_source_config(record.type, record.config)
    shared = {
        "id": record.id,
        "enabled": record.enabled,
        "nextRunAt": record.next_run_at,
        "targetId": record.target_id,
        "canonicalKey": record.canonical_key,
        "coverage": record.coverage,
        "lastCompletedAt": record.last_completed_at,
        "latestJob": job_contract(record.latest_job) if record.latest_job else None,
    }
    if record.type is SourceType.CHANNEL:
        return ChannelCollectionSource(
            type=SourceType.CHANNEL,
            config=config,
            **shared,
        )
    if record.type is SourceType.KEYWORD:
        return KeywordCollectionSource(
            type=SourceType.KEYWORD,
            config=config,
            **shared,
        )
    return VideoCollectionSource(
        type=SourceType.VIDEO,
        config=config,
        **shared,
    )


def video_contract(record: VideoRecord) -> CollectedVideo:
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


def comment_contract(record: CommentRecord) -> CollectedComment:
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


def comment_summary(summary: dict[str, object]) -> CommentSummary:
    return CommentSummary(
        total=int(summary.get("commentCount", 0)),
        latestPublishedAt=summary.get("latestCommentPublishedAt"),
        topWords=summary.get("topWords", []),
    )


def pin_contract(pin: dict[str, object]) -> TargetPin:
    return TargetPin(
        targetId=str(pin["target_id"]),
        enabled=bool(pin["enabled"]),
        intervalMinutes=int(pin["interval_minutes"]),
        nextRunAt=pin["next_run_at"],
        lastDispatchedAt=pin.get("last_dispatched_at"),
    )
