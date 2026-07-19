"""Source-specific YouTube collection and persistence for the polling worker."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Iterable, Mapping

from monitube_api.channel_resolution import resolve_channel_input
from monitube_api.domain import CommentRecord, JobRecord, JobState, SourceType, VideoRecord, new_id, utcnow
from monitube_api.quota import YoutubeErrorCategory, classify_youtube_error
from monitube_api.repositories import CollectionRepository

from .runner import LeaseLostError, QuotaExhaustedError, RetryableCollectionError
from .youtube_data import YouTubeApiError, YouTubeDataClient
from .collection.comments import CommentCollectionMixin
from .collection.discovery import DiscoveryCollectionMixin
from .collection.checkpoints import (
    checkpoint_payload,
    resume_cursor,
    with_phase_progress,
)
from .collection.error_policy import decide_collection_error
from .collection.parsing import (
    as_int,
    parse_duration_seconds,
    parse_rfc3339,
    quota_retry_delay_seconds,
)


class YouTubeCollector(DiscoveryCollectionMixin, CommentCollectionMixin):
    """Collect one source with a single configured API key; it never rotates keys."""

    def __init__(self, repository: CollectionRepository, client: YouTubeDataClient, *, lease_seconds: int = 120) -> None:
        self.repository = repository
        self.client = client
        self.lease_seconds = lease_seconds
        self._active_checkpoint: dict[str, Any] = {}

    def _checkpoint(self, job: JobRecord, *, stage: str, scope_key: str, page_token: str | None, batch_cursor: int = 0) -> None:
        checkpoint = self._checkpoint_payload(
            stage=stage,
            scope_key=scope_key,
            page_token=page_token,
            batch_cursor=batch_cursor,
        )
        self.repository.checkpoint_job(job.id, checkpoint)
        self._active_checkpoint = checkpoint

    def _checkpoint_payload(
        self, *, stage: str, scope_key: str, page_token: str | None, batch_cursor: int = 0
    ) -> dict[str, Any]:
        """Build a candidate checkpoint without advancing the committed cursor."""
        return checkpoint_payload(
            self._active_checkpoint,
            stage=stage,
            scope_key=scope_key,
            page_token=page_token,
            batch_cursor=batch_cursor,
        )

    def _set_phase_progress(
        self,
        job: JobRecord,
        *,
        phase: str,
        completed: int,
        total: int | None,
        current_stage: str,
    ) -> None:
        """Persist independently renderable video/comment progress with the job."""

        self._active_checkpoint = with_phase_progress(
            self._active_checkpoint,
            phase=phase,
            completed=completed,
            total=total,
        )
        self.repository.update_job_progress(
            job.id,
            completed=max(0, completed),
            total=max(0, total) if total is not None else None,
            unit="videos" if phase == "videos" else "comments",
            current_stage=current_stage,
        )
        # Keep the current cursor untouched.  In particular, replacing a
        # comment-page checkpoint here would prevent a quota-paused job from
        # resuming that video's page cursor.
        self.repository.checkpoint_job(job.id, self._active_checkpoint)

    @staticmethod
    def _resume_cursor(job: JobRecord, *, stage: str, scope_key: str) -> tuple[str | None, int]:
        return resume_cursor(job.checkpoint, stage=stage, scope_key=scope_key)

    def _call(self, job: JobRecord, endpoint: str, **params: Any) -> Mapping[str, Any]:
        if job.lease_owner:
            # Renew immediately before every potentially slow upstream call. A failed
            # renewal means another worker reclaimed the job, so do not continue it.
            if not self.repository.renew_job_lease(job_id=job.id, worker_id=job.lease_owner, lease_seconds=self.lease_seconds):
                raise LeaseLostError("Collection job lease is no longer owned by this worker")
        attempts = max(1, int(getattr(self.client, "key_count", 1)))
        for attempt in range(attempts):
            fingerprint = getattr(self.client, "key_fingerprint", None)
            try:
                payload = self.client.request(endpoint, params)
                if fingerprint and hasattr(self.repository, "record_runtime_key_state"):
                    self.repository.record_runtime_key_state(runtime_config_id=job.runtime_config_id, key_fingerprint=fingerprint)
                break
            except YouTubeApiError as exc:
                if fingerprint and hasattr(self.repository, "record_runtime_key_state"):
                    self.repository.record_runtime_key_state(runtime_config_id=job.runtime_config_id, key_fingerprint=fingerprint, error_reason=exc.reasons[0] if exc.reasons else "upstream_error")
                if attempt + 1 < attempts and getattr(self.client, "should_failover", lambda _error: False)(exc):
                    self.client.rotate()
                    continue
                self.repository.record_api_request(
                    job_id=job.id, bucket=exc.bucket, endpoint=endpoint, status_code=exc.status_code,
                    error_reason=exc.reasons[0] if exc.reasons else None,
                )
                raise
        else:  # pragma: no cover - loop always breaks or raises
            raise RuntimeError("YouTube key pool exhausted")
        self.repository.record_api_request(
            job_id=job.id,
            bucket=self.client.bucket_for(endpoint),
            endpoint=endpoint,
            status_code=200,
        )
        return payload

    def _raise_classified(self, job: JobRecord, exc: YouTubeApiError) -> None:
        if self._active_checkpoint:
            self.repository.checkpoint_job(job.id, self._active_checkpoint)
        decision = decide_collection_error(exc, job.checkpoint)
        if decision.action == "quota":
            checkpoint = dict(self._active_checkpoint or job.checkpoint)
            checkpoint["quotaRetryAttempt"] = as_int(checkpoint.get("quotaRetryAttempt")) + 1
            self._active_checkpoint = checkpoint
            self.repository.checkpoint_job(job.id, checkpoint)
            raise QuotaExhaustedError(
                str(exc),
                bucket=decision.quota_bucket or exc.bucket,
                resume_after_seconds=decision.retry_after_seconds or 3_600,
            ) from exc
        if decision.action == "retry":
            raise RetryableCollectionError(
                str(exc),
                retry_after_seconds=decision.retry_after_seconds or 60,
            ) from exc
        raise exc

    def _add_partial_error(self, job: JobRecord, *, scope: str, code: str, message: str, retryable: bool) -> None:
        current = self.repository.get_job(job.id)
        errors = list(current.partial_errors)
        errors.append({"scope": scope, "sourceId": current.source_id, "code": code, "retryable": retryable, "message": message})
        self.repository.transition_job(job.id, current.state, partial_errors=errors)

    def collect(self, job: JobRecord) -> None:
        """Collect and persist a single claimed job, raising runner-recognized errors."""

        source = self.repository.get_source(job.source_id)
        self._active_checkpoint = dict(job.checkpoint)
        try:
            if job.checkpoint.get("jobKind") == "video":
                self._collect_video_job(job, source)
                return
            if job.checkpoint.get("fanoutDiscovered"):
                self._finalize_fanout_job(job, source)
                return
            if source.type is SourceType.VIDEO:
                # A direct video request is already the smallest schedulable unit.
                self._collect_video_job(job, source, video_id=str(source.config["input"]))
                return
            if source.type is SourceType.CHANNEL:
                incremental_refresh = bool(source.coverage.get("complete") and source.coverage.get("collectAllVideos"))
                video_ids, known_videos, backfill_required = self._channel_video_ids(
                    job, source.config, incremental_refresh=incremental_refresh
                )
            elif source.type is SourceType.KEYWORD:
                video_ids = self._keyword_video_ids(job, source.config)
                known_videos = {}
                incremental_refresh = False
                backfill_required = False
            if job.target_id is None:
                self._collect_video_ids_inline(
                    job, source, video_ids, incremental_refresh=incremental_refresh, backfill_required=backfill_required
                )
                return
            # A discovery job performs only the cheap list/search phase, then fans
            # out independently retryable video jobs. This stops a large channel
            # from monopolising the worker ahead of other channels or keywords.
            self.repository.enqueue_video_jobs(parent_job=job, youtube_video_ids=video_ids)
            checkpoint = dict(self._active_checkpoint)
            checkpoint["fanoutDiscovered"] = True
            checkpoint["fanoutVideoCount"] = len(video_ids)
            self._active_checkpoint = checkpoint
            self.repository.checkpoint_job(job.id, checkpoint)
            self._set_phase_progress(
                job, phase="videos", completed=0, total=len(video_ids),
                current_stage="waiting_for_video_jobs",
            )
            raise RetryableCollectionError("Waiting for video collection jobs", retry_after_seconds=5)
        except YouTubeApiError as exc:
            self._raise_classified(job, exc)

    def _finalize_fanout_job(self, job: JobRecord, source: Any) -> None:
        total, terminal, failed = self.repository.child_job_summary(parent_job_id=job.id)
        self._set_phase_progress(job, phase="videos", completed=terminal, total=total, current_stage="waiting_for_video_jobs")
        if terminal < total:
            raise RetryableCollectionError("Waiting for video collection jobs", retry_after_seconds=5)
        if failed:
            raise RuntimeError(f"{failed} video collection job(s) failed")
        self._checkpoint(job, stage="completed", scope_key=source.id, page_token=None, batch_cursor=total)

    def _collect_video_ids_inline(
        self, job: JobRecord, source: Any, video_ids: list[str], *, incremental_refresh: bool, backfill_required: bool
    ) -> None:
        stage = "backfilling_oldest_videos" if backfill_required else "fetching_videos"
        self._set_phase_progress(job, phase="videos", completed=0, total=len(video_ids), current_stage=stage)
        videos = self._video_records(job, video_ids)
        for video in videos:
            self.repository.link_source_video(source.id, video.youtube_video_id)
        self._set_phase_progress(job, phase="videos", completed=len(video_ids), total=len(video_ids), current_stage="videos_persisted")
        if job.include_comments or source.config.get("includeComments"):
            max_pages = None if source.config.get("collectAllComments") else (
                job.max_comments_per_video or as_int(source.config.get("maxCommentPagesPerVideo")) or 1
            )
            persisted = self.repository.comment_counts_by_video(video.youtube_video_id for video in videos)
            pending = self._prioritize_comment_collection(
                [video for video in videos if persisted.get(video.youtube_video_id, 0) < video.statistics.get("commentCount", 0)], persisted
            )
            done = len(videos) - len(pending)
            self._set_phase_progress(job, phase="comments", completed=done, total=len(videos), current_stage="collecting_comments")
            for index, video in enumerate(pending, start=1):
                self._collect_comments(job, video, max_pages, incremental_refresh=incremental_refresh)
                self._set_phase_progress(job, phase="comments", completed=done + index, total=len(videos), current_stage="collecting_comments")
        self._checkpoint(job, stage="completed", scope_key=source.id, page_token=None, batch_cursor=len(videos))

    def _collect_video_job(self, job: JobRecord, source: Any, *, video_id: str | None = None) -> None:
        video_id = video_id or str(job.checkpoint.get("youtubeVideoId") or "")
        if not video_id:
            raise RuntimeError("Video job is missing youtubeVideoId")
        # Direct-video parent jobs do not start with the child ``jobKind``
        # checkpoint. Persist the video identity before the first detail cursor
        # so retry routing and cross-target terminal invalidation remain exact.
        self._active_checkpoint["youtubeVideoId"] = video_id
        videos = self._video_records(job, [video_id])
        for video in videos:
            self.repository.link_source_video(source.id, video.youtube_video_id)
        self._set_phase_progress(job, phase="videos", completed=len(videos), total=1, current_stage="video_persisted")
        if not videos:
            return
        include_comments = bool(job.include_comments or source.config.get("includeComments"))
        if include_comments:
            max_pages = None if source.config.get("collectAllComments") else (
                job.max_comments_per_video or as_int(source.config.get("maxCommentPagesPerVideo")) or 1
            )
            video = videos[0]
            persisted_count = self.repository.comment_counts_by_video([video.youtube_video_id]).get(video.youtube_video_id, 0)
            if persisted_count < video.statistics.get("commentCount", 0):
                self._set_phase_progress(job, phase="comments", completed=0, total=1, current_stage="collecting_comments")
                self._collect_comments(job, video, max_pages, incremental_refresh=False)
            self._set_phase_progress(job, phase="comments", completed=1, total=1, current_stage="comments_persisted")
        self._checkpoint(job, stage="completed", scope_key=video_id, page_token=None, batch_cursor=1)
