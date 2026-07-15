"""Worker-side state transitions for the durable polling collector."""

from __future__ import annotations

from datetime import timedelta
from typing import Protocol

from monitube_api.domain import JobRecord, JobState, QuotaBucket, utcnow
from monitube_api.repositories import JobRepository


class CollectionHandler(Protocol):
    """The YouTube collector implements this after a job has been claimed."""

    def collect(self, job: JobRecord) -> None: ...


class QuotaExhaustedError(RuntimeError):
    def __init__(self, message: str, *, bucket: QuotaBucket = QuotaBucket.CORE, resume_after_seconds: int = 3_600) -> None:
        super().__init__(message)
        self.bucket = bucket
        self.resume_after_seconds = resume_after_seconds


class RetryableCollectionError(RuntimeError):
    def __init__(self, message: str, *, retry_after_seconds: int = 60) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class LeaseLostError(RuntimeError):
    """Signals that another worker owns the job; this worker must not mutate it."""


class JobRunner:
    """Translate collector outcomes into resumable, policy-safe job states.

    The worker receives only jobs already bound to server-managed credentials. A quota
    wait leaves the job's existing checkpoint intact so a scheduler can later requeue
    it against that same binding.
    """

    def __init__(self, repository: JobRepository, handler: CollectionHandler) -> None:
        self.repository = repository
        self.handler = handler

    def run(self, job_id: str) -> JobRecord:
        job = self.repository.get_job(job_id)
        if job.state is JobState.QUEUED:
            running = self.repository.transition_job(job_id, JobState.RUNNING, current_stage="collecting")
        elif job.state is JobState.RUNNING:
            # A polling repository has already atomically claimed this job.
            running = job
        else:
            return job
        try:
            self.handler.collect(running)
        except QuotaExhaustedError as exc:
            return self.repository.transition_job(
                job_id,
                JobState.WAITING_QUOTA,
                current_stage="waiting_for_quota",
                pause_reason=str(exc),
                quota_bucket=exc.bucket,
                resume_at=utcnow() + timedelta(seconds=exc.resume_after_seconds),
                resume_is_automatic=True,
                lease_owner=None,
                lease_expires_at=None,
            )
        except RetryableCollectionError as exc:
            return self.repository.transition_job(
                job_id,
                JobState.WAITING_RETRY,
                current_stage="waiting_to_retry",
                pause_reason=str(exc),
                resume_at=utcnow() + timedelta(seconds=exc.retry_after_seconds),
                resume_is_automatic=True,
                lease_owner=None,
                lease_expires_at=None,
            )
        except LeaseLostError:
            # Do not turn another worker's claimed/reclaimed job into a failure.
            return self.repository.get_job(job_id)
        except Exception as exc:  # the real worker logs the traceback via its observability layer
            return self.repository.transition_job(
                job_id, JobState.FAILED, current_stage="failed", pause_reason=str(exc), lease_owner=None, lease_expires_at=None
            )
        latest = self.repository.get_job(job_id)
        terminal_state = JobState.COMPLETED_WITH_WARNINGS if latest.partial_errors else JobState.COMPLETED
        return self.repository.transition_job(
            job_id,
            terminal_state,
            current_stage="completed",
            pause_reason=None,
            quota_bucket=None,
            resume_at=None,
            resume_is_automatic=False,
            lease_owner=None,
            lease_expires_at=None,
        )
