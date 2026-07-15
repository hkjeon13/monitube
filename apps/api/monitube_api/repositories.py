"""Persistence contracts and a fully usable in-memory implementation for local tests."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from threading import RLock
from typing import Any, Protocol

from .analysis import build_summary
from .domain import CommentRecord, JobRecord, JobState, QuotaBucket, SourceRecord, SourceType, VideoRecord, new_id, utcnow


class RepositoryError(RuntimeError):
    pass


class NotFoundError(RepositoryError):
    pass


class InvalidStateTransitionError(RepositoryError):
    pass


class SourceRepository(Protocol):
    def create_source(self, *, source_type: SourceType, config: dict[str, Any]) -> SourceRecord: ...

    def get_source(self, source_id: str) -> SourceRecord: ...

    def list_sources(self) -> list[SourceRecord]: ...

    def update_source(self, source_id: str, **changes: Any) -> SourceRecord: ...

    def delete_source(self, source_id: str) -> None: ...


class JobRepository(Protocol):
    def create_job(
        self,
        *,
        source_id: str,
        include_comments: bool,
        max_videos: int | None,
        max_comments_per_video: int | None,
        runtime_config_id: str | None = None,
    ) -> JobRecord: ...

    def get_job(self, job_id: str) -> JobRecord: ...

    def transition_job(self, job_id: str, state: JobState, **changes: Any) -> JobRecord: ...


class CollectionRepository(SourceRepository, JobRepository, Protocol):
    """Methods used by the API service and the polling collection worker."""

    def bootstrap_runtime_config(
        self, *, environment: str, google_project_number: str, secret_ref: str, key_fingerprint: str | None
    ) -> str: ...

    def claim_next_job(self, *, worker_id: str, lease_seconds: int = 120) -> JobRecord | None: ...

    def renew_job_lease(self, *, job_id: str, worker_id: str, lease_seconds: int = 120) -> bool: ...

    def checkpoint_job(self, job_id: str, checkpoint: dict[str, Any]) -> JobRecord: ...

    def update_job_progress(
        self, job_id: str, *, completed: int, total: int | None, unit: str, current_stage: str | None = None
    ) -> JobRecord: ...

    def upsert_channel(self, channel: dict[str, Any]) -> dict[str, Any]: ...

    def upsert_video(self, video: VideoRecord) -> VideoRecord: ...

    def link_source_video(self, source_id: str, youtube_video_id: str) -> None: ...

    def upsert_comment(self, comment: CommentRecord) -> CommentRecord: ...

    def record_api_request(self, *, job_id: str, bucket: QuotaBucket, endpoint: str, status_code: int, error_reason: str | None = None) -> None: ...

    def save_analysis_summary(self, source_id: str) -> dict[str, Any]: ...

    def get_source_results(self, source_id: str) -> dict[str, Any]: ...

    def get_video_comments(self, video_id: str) -> dict[str, Any]: ...


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


class InMemoryRepository(CollectionRepository):
    """Thread-safe test/local repository with the same durable boundary as PostgreSQL.

    It is deliberately useful enough to run the API and fake collector tests without a
    database. Runtime config records contain only a reference and fingerprint; a raw
    API key is never accepted or retained by this repository.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._sources: dict[str, SourceRecord] = {}
        self._jobs: dict[str, JobRecord] = {}
        self._runtime_configs: dict[str, dict[str, Any]] = {}
        self._channels: dict[str, dict[str, Any]] = {}
        self._videos: dict[str, VideoRecord] = {}
        self._comments: dict[str, CommentRecord] = {}
        self._source_videos: dict[str, set[str]] = {}
        self._analysis: dict[str, dict[str, Any]] = {}
        self._request_logs: list[dict[str, Any]] = []

    @staticmethod
    def _clone_source(record: SourceRecord) -> SourceRecord:
        return replace(record, config=deepcopy(record.config))

    @staticmethod
    def _clone_job(record: JobRecord) -> JobRecord:
        return replace(record, checkpoint=deepcopy(record.checkpoint), partial_errors=deepcopy(record.partial_errors))

    def bootstrap_runtime_config(
        self, *, environment: str, google_project_number: str, secret_ref: str, key_fingerprint: str | None
    ) -> str:
        with self._lock:
            for identifier, config in self._runtime_configs.items():
                if config["environment"] == environment and config["google_project_number"] == google_project_number:
                    config.update(secret_ref=secret_ref, key_fingerprint=key_fingerprint, status="active")
                    return identifier
            identifier = new_id()
            self._runtime_configs[identifier] = {
                "environment": environment,
                "google_project_number": google_project_number,
                "secret_ref": secret_ref,
                "key_fingerprint": key_fingerprint,
                "status": "active",
            }
            return identifier

    def create_source(self, *, source_type: SourceType, config: dict[str, Any]) -> SourceRecord:
        with self._lock:
            now = utcnow()
            record = SourceRecord(
                id=new_id(), type=source_type, config=deepcopy(config), enabled=True, created_at=now, updated_at=now
            )
            self._sources[record.id] = record
            self._source_videos[record.id] = set()
            return self._clone_source(record)

    def get_source(self, source_id: str) -> SourceRecord:
        with self._lock:
            try:
                return self._clone_source(self._sources[source_id])
            except KeyError as exc:
                raise NotFoundError(f"Source '{source_id}' was not found") from exc

    def list_sources(self) -> list[SourceRecord]:
        with self._lock:
            return [self._clone_source(record) for record in sorted(self._sources.values(), key=lambda item: item.created_at)]

    def update_source(self, source_id: str, **changes: Any) -> SourceRecord:
        allowed = {"enabled", "config", "next_run_at"}
        unknown = changes.keys() - allowed
        if unknown:
            raise RepositoryError(f"Unsupported source changes: {', '.join(sorted(unknown))}")
        with self._lock:
            record = self.get_source(source_id)
            values = dict(changes)
            if "config" in values:
                values["config"] = deepcopy(values["config"])
            values["updated_at"] = utcnow()
            updated = replace(record, **values)
            self._sources[source_id] = updated
            return self._clone_source(updated)

    def delete_source(self, source_id: str) -> None:
        with self._lock:
            if source_id not in self._sources:
                raise NotFoundError(f"Source '{source_id}' was not found")
            del self._sources[source_id]
            self._source_videos.pop(source_id, None)
            self._analysis.pop(source_id, None)
            for job_id in [job.id for job in self._jobs.values() if job.source_id == source_id]:
                del self._jobs[job_id]

    def create_job(
        self,
        *,
        source_id: str,
        include_comments: bool,
        max_videos: int | None,
        max_comments_per_video: int | None,
        runtime_config_id: str | None = None,
    ) -> JobRecord:
        with self._lock:
            self.get_source(source_id)
            now = utcnow()
            record = JobRecord(
                id=new_id(),
                source_id=source_id,
                state=JobState.QUEUED,
                current_stage="queued",
                progress_completed=0,
                progress_total=None,
                progress_unit="sources",
                include_comments=include_comments,
                max_videos=max_videos,
                max_comments_per_video=max_comments_per_video,
                runtime_config_id=runtime_config_id,
                created_at=now,
                updated_at=now,
            )
            self._jobs[record.id] = record
            return self._clone_job(record)

    def get_job(self, job_id: str) -> JobRecord:
        with self._lock:
            try:
                return self._clone_job(self._jobs[job_id])
            except KeyError as exc:
                raise NotFoundError(f"Job '{job_id}' was not found") from exc

    def transition_job(self, job_id: str, state: JobState, **changes: Any) -> JobRecord:
        allowed = {
            "current_stage",
            "progress_completed",
            "progress_total",
            "progress_unit",
            "pause_reason",
            "quota_bucket",
            "resume_at",
            "resume_is_automatic",
            "checkpoint",
            "partial_errors",
            "lease_owner",
            "lease_expires_at",
        }
        unknown = changes.keys() - allowed
        if unknown:
            raise RepositoryError(f"Unsupported job changes: {', '.join(sorted(unknown))}")
        with self._lock:
            record = self.get_job(job_id)
            if state != record.state and state not in _ALLOWED_TRANSITIONS[record.state]:
                raise InvalidStateTransitionError(f"Cannot transition job '{job_id}' from {record.state.value} to {state.value}")
            values = dict(changes)
            for key in ("checkpoint", "partial_errors"):
                if key in values:
                    values[key] = deepcopy(values[key])
            values.update(state=state, updated_at=utcnow())
            updated = replace(record, **values)
            self._jobs[job_id] = updated
            return self._clone_job(updated)

    def claim_next_job(self, *, worker_id: str, lease_seconds: int = 120) -> JobRecord | None:
        with self._lock:
            now = utcnow()
            candidates = sorted(self._jobs.values(), key=lambda item: item.created_at)
            for record in candidates:
                due_wait = record.state in {JobState.WAITING_RETRY, JobState.WAITING_QUOTA} and record.resume_at is not None and record.resume_at <= now
                recoverable_running = record.state is JobState.RUNNING and record.lease_expires_at is not None and record.lease_expires_at <= now
                available_lease = record.lease_expires_at is None or record.lease_expires_at <= now
                if (record.state is JobState.QUEUED or due_wait or recoverable_running) and available_lease:
                    updated = replace(
                        record,
                        state=JobState.RUNNING,
                        current_stage="reclaimed" if recoverable_running else "claimed",
                        pause_reason=None,
                        quota_bucket=None,
                        resume_at=None,
                        resume_is_automatic=False,
                        lease_owner=worker_id,
                        lease_expires_at=now + timedelta(seconds=lease_seconds),
                        updated_at=now,
                    )
                    self._jobs[record.id] = updated
                    return self._clone_job(updated)
            return None

    def renew_job_lease(self, *, job_id: str, worker_id: str, lease_seconds: int = 120) -> bool:
        with self._lock:
            record = self._jobs.get(job_id)
            if not record or record.state is not JobState.RUNNING or record.lease_owner != worker_id:
                return False
            self._jobs[job_id] = replace(record, lease_expires_at=utcnow() + timedelta(seconds=lease_seconds), updated_at=utcnow())
            return True

    def checkpoint_job(self, job_id: str, checkpoint: dict[str, Any]) -> JobRecord:
        record = self.get_job(job_id)
        return self.transition_job(job_id, record.state, checkpoint=checkpoint)

    def update_job_progress(
        self, job_id: str, *, completed: int, total: int | None, unit: str, current_stage: str | None = None
    ) -> JobRecord:
        record = self.get_job(job_id)
        changes: dict[str, Any] = {"progress_completed": completed, "progress_total": total, "progress_unit": unit}
        if current_stage:
            changes["current_stage"] = current_stage
        return self.transition_job(job_id, record.state, **changes)

    def upsert_channel(self, channel: dict[str, Any]) -> dict[str, Any]:
        youtube_channel_id = str(channel["youtube_channel_id"])
        with self._lock:
            current = deepcopy(self._channels.get(youtube_channel_id, {}))
            current.update({key: value for key, value in deepcopy(channel).items() if value is not None or key not in current})
            self._channels[youtube_channel_id] = current
            return deepcopy(current)

    def upsert_video(self, video: VideoRecord) -> VideoRecord:
        with self._lock:
            current = self._videos.get(video.youtube_video_id)
            stored = replace(video, id=current.id) if current else video
            self._videos[video.youtube_video_id] = stored
            return stored

    def link_source_video(self, source_id: str, youtube_video_id: str) -> None:
        with self._lock:
            self.get_source(source_id)
            if youtube_video_id not in self._videos:
                raise NotFoundError(f"Video '{youtube_video_id}' was not found")
            self._source_videos.setdefault(source_id, set()).add(youtube_video_id)

    def upsert_comment(self, comment: CommentRecord) -> CommentRecord:
        with self._lock:
            current = self._comments.get(comment.youtube_comment_id)
            stored = replace(comment, id=current.id) if current else comment
            self._comments[comment.youtube_comment_id] = stored
            return stored

    def record_api_request(
        self, *, job_id: str, bucket: QuotaBucket, endpoint: str, status_code: int, error_reason: str | None = None
    ) -> None:
        with self._lock:
            self._request_logs.append(
                {
                    "job_id": job_id,
                    "bucket": bucket.value,
                    "endpoint": endpoint,
                    "status_code": status_code,
                    "error_reason": error_reason,
                    "occurred_at": utcnow(),
                }
            )

    def _source_video_records(self, source_id: str) -> list[VideoRecord]:
        ids = self._source_videos.get(source_id, set())
        return sorted((self._videos[item] for item in ids if item in self._videos), key=lambda item: item.source_fetched_at, reverse=True)

    def _comments_for_video_ids(self, video_ids: set[str]) -> list[CommentRecord]:
        return sorted(
            (comment for comment in self._comments.values() if comment.youtube_video_id in video_ids),
            key=lambda item: item.published_at or item.source_fetched_at,
            reverse=True,
        )

    def save_analysis_summary(self, source_id: str) -> dict[str, Any]:
        with self._lock:
            videos = self._source_video_records(source_id)
            comments = self._comments_for_video_ids({video.youtube_video_id for video in videos})
            summary = build_summary(videos, comments)
            self._analysis[source_id] = deepcopy(summary)
            return deepcopy(summary)

    def get_source_results(self, source_id: str) -> dict[str, Any]:
        with self._lock:
            source = self.get_source(source_id)
            videos = self._source_video_records(source_id)
            comments = self._comments_for_video_ids({video.youtube_video_id for video in videos})
            latest_job = next(
                iter(sorted((job for job in self._jobs.values() if job.source_id == source_id), key=lambda item: item.created_at, reverse=True)),
                None,
            )
            summary = deepcopy(self._analysis.get(source_id) or build_summary(videos, comments))
            return {
                "source": source,
                "latest_job": self._clone_job(latest_job) if latest_job else None,
                "videos": list(videos),
                "comments": list(comments),
                "analysis": summary,
            }

    def get_video_comments(self, video_id: str) -> dict[str, Any]:
        with self._lock:
            video = self._videos.get(video_id)
            if not video:
                raise NotFoundError(f"Video '{video_id}' was not found")
            comments = self._comments_for_video_ids({video_id})
            return {"video": video, "comments": comments, "summary": build_summary([video], comments)}
