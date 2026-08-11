"""Source-specific YouTube collection and persistence for the polling worker."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Iterable, Mapping

from monitube_api.channel_resolution import resolve_channel_input
from monitube_api.domain import CommentRecord, JobRecord, JobState, SourceType, VideoRecord, new_id, utcnow
from monitube_api.quota import YoutubeErrorCategory, classify_youtube_error
from monitube_api.repositories import CollectionRepository

from .runner import LeaseLostError, QuotaExhaustedError, RetryableCollectionError
from .searchapi import SearchApiError
from .youtube_data import YouTubeApiError, YouTubeDataClient
from .collection.comments import CommentCollectionMixin
from .collection.discovery import DiscoveryCollectionMixin
from .collection.transcripts import TranscriptCollectionMixin
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


class YouTubeCollector(DiscoveryCollectionMixin, CommentCollectionMixin, TranscriptCollectionMixin):
    """Collect one source with a single configured API key; it never rotates keys."""

    def __init__(
        self,
        repository: CollectionRepository,
        client: YouTubeDataClient,
        *,
        discovery_provider: str = "youtube",
        discovery_client: Any | None = None,
        transcript_client: Any | None = None,
        transcript_collection_enabled: bool = False,
        transcript_primary_language: str = "ko",
        transcript_fallback_language: str = "en",
        transcript_type_preference: str = "manual",
        transcript_max_segments: int = 100_000,
        lease_seconds: int = 120,
    ) -> None:
        self.repository = repository
        self.client = client
        self.discovery_provider = discovery_provider
        self.discovery_client = discovery_client
        self.transcript_client = transcript_client
        self.transcript_collection_enabled = transcript_collection_enabled
        self.transcript_primary_language = transcript_primary_language
        self.transcript_fallback_language = transcript_fallback_language
        self.transcript_type_preference = transcript_type_preference
        self.transcript_max_segments = transcript_max_segments
        self.lease_seconds = lease_seconds
        self._active_checkpoint: dict[str, Any] = {}

    def _searchapi_call(
        self,
        job: JobRecord,
        operation: str,
        method: Any,
        **params: Any,
    ) -> Mapping[str, Any]:
        if job.lease_owner and not self.repository.renew_job_lease(
            job_id=job.id,
            worker_id=job.lease_owner,
            lease_seconds=self.lease_seconds,
        ):
            raise LeaseLostError("Collection job lease is no longer owned by this worker")
        try:
            payload = method(**params)
        except SearchApiError as exc:
            self.repository.record_provider_request(
                job_id=job.id,
                provider="searchapi",
                operation=operation,
                status_code=exc.status_code,
                error_code=exc.error_code,
                requested_language=params.get("language"),
            )
            if exc.status_code == 429 or exc.status_code >= 500:
                raise RetryableCollectionError(
                    str(exc), retry_after_seconds=60
                ) from exc
            raise
        items = payload.get("videos") or payload.get("transcripts") or []
        self.repository.record_provider_request(
            job_id=job.id,
            provider="searchapi",
            operation=operation,
            status_code=200,
            item_count=len(items) if isinstance(items, list) else None,
            requested_language=params.get("language"),
            resolved_language=(payload.get("search_parameters") or {}).get("lang"),
        )
        return payload

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
        failed: int = 0,
        waiting_quota: int = 0,
    ) -> None:
        """Persist independently renderable video/comment progress with the job."""

        self._active_checkpoint = with_phase_progress(
            self._active_checkpoint,
            phase=phase,
            completed=completed,
            total=total,
            failed=failed,
            waiting_quota=waiting_quota,
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
            if job.checkpoint.get("jobKind") == "video_batch":
                self._collect_video_batch_job(job, source)
                return
            if job.checkpoint.get("jobKind") == "comment":
                self._collect_comment_job(job, source)
                return
            needs_keyword_history = bool(
                source.type is SourceType.KEYWORD
                and not source.coverage.get("historicalBackfillComplete")
                and not job.checkpoint.get("keywordHistoricalBackfillComplete")
            )
            if job.checkpoint.get("fanoutDiscovered") and not needs_keyword_history:
                self._finalize_fanout_job(job, source)
                return
            if job.checkpoint.get("fanoutDiscovered") and needs_keyword_history:
                # Upgrade a keyword parent that was already in its legacy fanout
                # phase when this historical-backfill behavior was deployed. Keep
                # its existing children and continue discovery on the same parent,
                # whose per-video idempotency keys suppress duplicate child work.
                self._active_checkpoint.pop("fanoutDiscovered", None)
                self._active_checkpoint.pop("fanoutVideoCount", None)
                self.repository.checkpoint_job(job.id, self._active_checkpoint)
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
                video_ids = self._keyword_video_ids(
                    job,
                    source.config,
                    historical_backfill=not bool(
                        source.coverage.get("historicalBackfillComplete")
                    ),
                )
                incremental_refresh = False
                backfill_required = False
            if job.target_id is None:
                self._collect_video_ids_inline(
                    job,
                    source,
                    video_ids,
                    incremental_refresh=incremental_refresh,
                    backfill_required=backfill_required,
                )
                return
            # A discovery job performs only the cheap list/search phase, then fans
            # out independently retryable video jobs. This stops a large channel
            # from monopolising the worker ahead of other channels or keywords.
            self.repository.enqueue_video_jobs(
                parent_job=job, youtube_video_ids=video_ids
            )
            summary = self.repository.child_phase_summary(parent_job_id=job.id)
            checkpoint = dict(self._active_checkpoint)
            checkpoint["fanoutDiscovered"] = True
            checkpoint["fanoutVideoCount"] = summary["video_total"]
            self._active_checkpoint = checkpoint
            self.repository.checkpoint_job(job.id, checkpoint)
            self._set_parent_phase_progress(job, summary)
            raise RetryableCollectionError(
                "Waiting for video collection jobs", retry_after_seconds=5
            )
        except YouTubeApiError as exc:
            self._raise_classified(job, exc)

    def _finalize_fanout_job(self, job: JobRecord, source: Any) -> None:
        summary = self.repository.child_phase_summary(parent_job_id=job.id)
        self._set_parent_phase_progress(job, summary)
        if summary["job_terminal"] < summary["job_total"]:
            raise RetryableCollectionError(
                "Waiting for video collection jobs", retry_after_seconds=5
            )
        if summary["job_failed"]:
            raise RuntimeError(
                f"{summary['job_failed']} video collection job(s) failed"
            )
        if summary.get("video_failed", 0):
            current = self.repository.get_job(job.id)
            if not any(
                error.get("code") == "video_metadata_unavailable"
                for error in current.partial_errors
            ):
                self._add_partial_error(
                    job,
                    scope="video",
                    code="video_metadata_unavailable",
                    message=f"{summary['video_failed']} discovered video(s) did not return canonical metadata",
                    retryable=False,
                )
        self._checkpoint(
            job,
            stage="completed",
            scope_key=source.id,
            page_token=None,
            batch_cursor=summary["video_total"],
        )

    def _set_parent_phase_progress(
        self, job: JobRecord, summary: Mapping[str, int]
    ) -> None:
        """Expose persisted video/comment work without conflating it with terminal children."""

        waiting = summary.get("waiting_quota", 0)
        stage = "waiting_for_quota" if waiting else "waiting_for_video_jobs"
        self._set_phase_progress(
            job,
            phase="videos",
            completed=summary.get("video_completed", 0),
            total=summary.get("video_total", 0),
            current_stage=stage,
            failed=summary.get("video_failed", 0),
            waiting_quota=summary.get("video_waiting_quota", 0),
        )
        if summary.get("transcript_total", 0):
            self._set_phase_progress(
                job,
                phase="transcripts",
                completed=summary.get("transcript_completed", 0),
                total=summary.get("transcript_total", 0),
                current_stage=stage,
                failed=summary.get("transcript_failed", 0),
            )
        if summary.get("comment_total", 0):
            self._set_phase_progress(
                job,
                phase="comments",
                completed=summary.get("comment_completed", 0),
                total=summary.get("comment_total", 0),
                current_stage=stage,
                failed=summary.get("comment_failed", 0),
                waiting_quota=summary.get("comment_waiting_quota", 0),
            )
        # Keep the generic progress compatible with existing clients while the
        # dedicated phase fields carry the precise semantics.
        self.repository.update_job_progress(
            job.id,
            completed=summary.get("video_completed", 0),
            total=summary.get("video_total", 0),
            unit="videos",
            current_stage=stage,
        )

    def _collect_video_ids_inline(
        self,
        job: JobRecord,
        source: Any,
        video_ids: list[str],
        *,
        incremental_refresh: bool,
        backfill_required: bool,
    ) -> None:
        stage = "backfilling_oldest_videos" if backfill_required else "fetching_videos"
        self._set_phase_progress(
            job, phase="videos", completed=0, total=len(video_ids), current_stage=stage
        )
        known = self.repository.get_videos_by_youtube_ids(video_ids)
        videos = [
            video
            for video in self._video_records(job, video_ids)
            if self._video_matches_source_window(source, video)
        ]
        for video in videos:
            self.repository.link_source_video(source.id, video.youtube_video_id)
            if source.type in {SourceType.CHANNEL, SourceType.KEYWORD}:
                self._maybe_collect_transcript(
                    job,
                    video,
                    newly_discovered=video.youtube_video_id not in known,
                )
        self._set_phase_progress(
            job,
            phase="videos",
            completed=len(videos),
            total=len(video_ids),
            current_stage="videos_persisted",
            failed=max(0, len(video_ids) - len(videos)),
        )
        if job.include_comments or source.config.get("includeComments"):
            max_pages = (
                None
                if source.config.get("collectAllComments")
                else (
                    job.max_comments_per_video
                    or as_int(source.config.get("maxCommentPagesPerVideo"))
                    or 1
                )
            )
            persisted = self.repository.comment_counts_by_video(
                video.youtube_video_id for video in videos
            )
            pending = self._prioritize_comment_collection(
                [
                    video
                    for video in videos
                    if persisted.get(video.youtube_video_id, 0)
                    < video.statistics.get("commentCount", 0)
                ],
                persisted,
            )
            done = len(videos) - len(pending)
            self._set_phase_progress(
                job,
                phase="comments",
                completed=done,
                total=len(videos),
                current_stage="collecting_comments",
            )
            for index, video in enumerate(pending, start=1):
                self._collect_comments(
                    job, video, max_pages, incremental_refresh=incremental_refresh
                )
                self._set_phase_progress(
                    job,
                    phase="comments",
                    completed=done + index,
                    total=len(videos),
                    current_stage="collecting_comments",
                )
        self._checkpoint(
            job,
            stage="completed",
            scope_key=source.id,
            page_token=None,
            batch_cursor=len(videos),
        )

    def _collect_video_job(
        self, job: JobRecord, source: Any, *, video_id: str | None = None
    ) -> None:
        video_id = video_id or str(job.checkpoint.get("youtubeVideoId") or "")
        if not video_id:
            raise RuntimeError("Video job is missing youtubeVideoId")
        # Direct-video parent jobs do not start with the child ``jobKind``
        # checkpoint. Persist the video identity before the first detail cursor
        # so retry routing and cross-target terminal invalidation remain exact.
        self._active_checkpoint["youtubeVideoId"] = video_id
        newly_discovered = video_id not in self.repository.get_videos_by_youtube_ids([video_id])
        videos = [
            video for video in self._video_records(job, [video_id])
            if self._video_matches_source_window(source, video)
        ]
        for video in videos:
            self.repository.link_source_video(source.id, video.youtube_video_id)
        self._set_phase_progress(
            job,
            phase="videos",
            completed=len(videos),
            total=1,
            current_stage="video_persisted",
            failed=0 if videos else 1,
        )
        for video in videos:
            if source.type in {SourceType.CHANNEL, SourceType.KEYWORD}:
                self._maybe_collect_transcript(
                    job, video, newly_discovered=newly_discovered
                )
        if not videos:
            return
        include_comments = bool(
            job.include_comments or source.config.get("includeComments")
        )
        if include_comments:
            max_pages = (
                None
                if source.config.get("collectAllComments")
                else (
                    job.max_comments_per_video
                    or as_int(source.config.get("maxCommentPagesPerVideo"))
                    or 1
                )
            )
            video = videos[0]
            persisted_count = self.repository.comment_counts_by_video(
                [video.youtube_video_id]
            ).get(video.youtube_video_id, 0)
            if persisted_count < video.statistics.get("commentCount", 0):
                self._set_phase_progress(
                    job,
                    phase="comments",
                    completed=0,
                    total=1,
                    current_stage="collecting_comments",
                )
                self._collect_comments(job, video, max_pages, incremental_refresh=False)
            self._set_phase_progress(
                job,
                phase="comments",
                completed=1,
                total=1,
                current_stage="comments_persisted",
            )
        self._checkpoint(
            job, stage="completed", scope_key=video_id, page_token=None, batch_cursor=1
        )

    def _collect_video_batch_job(self, job: JobRecord, source: Any) -> None:
        raw_ids = job.checkpoint.get("youtubeVideoIds")
        video_ids = (
            list(dict.fromkeys(str(value) for value in raw_ids if str(value)))
            if isinstance(raw_ids, list)
            else []
        )
        if not video_ids:
            raise RuntimeError("Video batch job is missing youtubeVideoIds")

        known = self.repository.get_videos_by_youtube_ids(video_ids)
        videos = [
            video
            for video in self._video_records(job, video_ids)
            if self._video_matches_source_window(source, video)
        ]
        for video in videos:
            self.repository.link_source_video(source.id, video.youtube_video_id)
        self._set_phase_progress(
            job,
            phase="videos",
            completed=len(videos),
            total=len(video_ids),
            current_stage="videos_persisted",
            failed=max(0, len(video_ids) - len(videos)),
        )

        transcript_candidates = (
            videos if source.type in {SourceType.CHANNEL, SourceType.KEYWORD} else []
        )
        transcript_done = 0
        if (
            transcript_candidates
            and self.transcript_collection_enabled
            and self.transcript_client is not None
        ):
            self._set_phase_progress(
                job,
                phase="transcripts",
                completed=0,
                total=len(transcript_candidates),
                current_stage="collecting_transcripts",
            )
            for video in transcript_candidates:
                self._maybe_collect_transcript(
                    job,
                    video,
                    newly_discovered=video.youtube_video_id not in known,
                )
                transcript_done += 1
                self._set_phase_progress(
                    job,
                    phase="transcripts",
                    completed=transcript_done,
                    total=len(transcript_candidates),
                    current_stage="collecting_transcripts",
                )

        if job.include_comments or source.config.get("includeComments"):
            self.repository.enqueue_comment_jobs(
                video_batch_job=job,
                youtube_video_ids=[video.youtube_video_id for video in videos],
            )

        self._checkpoint(
            job,
            stage="completed",
            scope_key=source.id,
            page_token=None,
            batch_cursor=len(video_ids),
        )

    def _collect_comment_job(self, job: JobRecord, source: Any) -> None:
        video_id = str(job.checkpoint.get("youtubeVideoId") or "")
        if not video_id:
            raise RuntimeError("Comment job is missing youtubeVideoId")
        video = self.repository.get_videos_by_youtube_ids([video_id]).get(video_id)
        if video is None:
            raise RuntimeError(f"Comment job video '{video_id}' was not persisted")

        max_pages = (
            None
            if source.config.get("collectAllComments")
            else (
                job.max_comments_per_video
                or as_int(source.config.get("maxCommentPagesPerVideo"))
                or 1
            )
        )
        persisted_count = self.repository.comment_counts_by_video([video_id]).get(
            video_id, 0
        )
        self._set_phase_progress(
            job,
            phase="comments",
            completed=0,
            total=1,
            current_stage="collecting_comments",
        )
        if persisted_count < video.statistics.get("commentCount", 0):
            self._collect_comments(job, video, max_pages, incremental_refresh=False)
        self._set_phase_progress(
            job,
            phase="comments",
            completed=1,
            total=1,
            current_stage="comments_persisted",
        )
        self._checkpoint(
            job,
            stage="completed",
            scope_key=video_id,
            page_token=None,
            batch_cursor=1,
        )

    @staticmethod
    def _video_matches_source_window(source: Any, video: VideoRecord) -> bool:
        """Enforce exact keyword timestamps after canonical videos.list hydration."""

        if source.type is not SourceType.KEYWORD or video.published_at is None:
            return True
        published_after = parse_rfc3339(source.config.get("publishedAfter"))
        published_before = parse_rfc3339(source.config.get("publishedBefore"))
        if published_after and video.published_at < published_after:
            return False
        if published_before and video.published_at > published_before:
            return False
        return True
