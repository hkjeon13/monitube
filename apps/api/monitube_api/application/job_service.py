"""Collection job command and status use cases."""

from ..contracts import (
    ActiveParentJob,
    ActiveParentJobsResponse,
    JobCreate,
    JobStateChange,
    JobStatus,
    RecentJobFailure,
    RecentJobFailuresResponse,
)
from ..domain import JobRecord, SourceType
from .base import ApplicationService
from .presenters import job_contract


def _source_label(
    source_type: SourceType,
    config: dict[str, object],
    canonical_key: str | None,
) -> str:
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


def _failure_details(
    item: dict[str, object],
) -> tuple[str, str | None, bool | None]:
    child_pause = _safe_failure_text(
        item.get("representative_child_pause_reason")
    )
    if child_pause:
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
    parent_message, parent_code, parent_retryable = _structured_failure(
        job.partial_errors
    )
    return (
        parent_message
        or parent_code
        or "Collection failed without a recorded reason.",
        parent_code,
        parent_retryable,
    )


class JobService(ApplicationService):
    def create_job(
        self,
        source_id: str,
        request: JobCreate,
        *,
        owner_id: str | None = None,
    ) -> JobStatus:
        record = self.repository.create_job(
            source_id=source_id,
            include_comments=request.include_comments,
            max_videos=request.max_videos,
            max_comments_per_video=request.max_comments_per_video,
            owner_id=owner_id,
            runtime_config_id=self.runtime_config_id,
        )
        return job_contract(record)

    def get_job(
        self,
        job_id: str,
        *,
        owner_id: str | None = None,
    ) -> JobStatus:
        return job_contract(self.repository.get_job(job_id, owner_id=owner_id))

    def list_source_jobs(
        self,
        source_id: str,
        *,
        owner_id: str | None = None,
        limit: int = 20,
    ) -> list[JobStatus]:
        return [
            job_contract(record)
            for record in self.repository.list_jobs_for_source(
                source_id,
                limit=limit,
                owner_id=owner_id,
            )
        ]

    def list_active_parent_jobs(
        self,
        *,
        owner_id: str,
    ) -> ActiveParentJobsResponse:
        return ActiveParentJobsResponse(
            jobs=[
                ActiveParentJob(
                    sourceId=item["source_id"],
                    targetId=item.get("target_id"),
                    job=job_contract(item["job"]),
                )
                for item in self.repository.list_active_parent_jobs(
                    owner_id=owner_id
                )
            ]
        )

    def list_recent_failed_parent_jobs(
        self,
        *,
        owner_id: str,
        limit: int = 10,
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
                    job=job_contract(
                        item["job"],
                        public_source_id=public_source_id,
                    ),
                )
            )
        return RecentJobFailuresResponse(failures=failures)

    def change_job_state(
        self,
        job_id: str,
        request: JobStateChange,
    ) -> JobStatus:
        changes = request.model_dump(exclude={"state"}, exclude_none=True)
        if "partial_errors" in changes:
            changes["partial_errors"] = [
                error.model_dump(exclude_none=True)
                for error in request.partial_errors or []
            ]
        return job_contract(
            self.repository.transition_job(job_id, request.state, **changes)
        )
