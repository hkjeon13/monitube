"""Job command, status, lease, and checkpoint persistence ports."""

from typing import Any, Iterable, Protocol

from ..domain import JobRecord, JobState


class JobRepository(Protocol):
    def create_job(
        self,
        *,
        source_id: str,
        include_comments: bool,
        max_videos: int | None,
        max_comments_per_video: int | None,
        owner_id: str | None = None,
        runtime_config_id: str | None = None,
    ) -> JobRecord: ...

    def get_job(
        self,
        job_id: str,
        *,
        owner_id: str | None = None,
    ) -> JobRecord: ...

    def list_jobs_for_source(
        self,
        source_id: str,
        *,
        limit: int = 20,
        owner_id: str | None = None,
    ) -> list[JobRecord]: ...

    def list_active_parent_jobs(
        self,
        *,
        owner_id: str,
    ) -> list[dict[str, Any]]: ...

    def list_recent_failed_parent_jobs(
        self,
        *,
        owner_id: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]: ...

    def transition_job(
        self,
        job_id: str,
        state: JobState,
        **changes: Any,
    ) -> JobRecord: ...


class JobLeaseRepository(Protocol):
    def claim_next_job(
        self,
        *,
        worker_id: str,
        lease_seconds: int = 120,
    ) -> JobRecord | None: ...

    def renew_job_lease(
        self,
        *,
        job_id: str,
        worker_id: str,
        lease_seconds: int = 120,
    ) -> bool: ...

    def checkpoint_job(
        self,
        job_id: str,
        checkpoint: dict[str, Any],
    ) -> JobRecord: ...

    def update_job_progress(
        self,
        job_id: str,
        *,
        completed: int,
        total: int | None,
        unit: str,
        current_stage: str | None = None,
    ) -> JobRecord: ...

    def enqueue_video_jobs(
        self,
        *,
        parent_job: JobRecord,
        youtube_video_ids: Iterable[str],
    ) -> int: ...

    def child_job_summary(
        self,
        *,
        parent_job_id: str,
    ) -> tuple[int, int, int]: ...
