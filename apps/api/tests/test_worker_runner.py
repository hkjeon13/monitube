from monitube_api.domain import JobState, QuotaBucket, SourceType
from monitube_api.repositories import InMemoryRepository
from monitube_worker.runner import JobRunner, LeaseLostError, QuotaExhaustedError


class QuotaLimitedCollector:
    def collect(self, _job: object) -> None:
        raise QuotaExhaustedError(
            "search query quota exhausted",
            bucket=QuotaBucket.SEARCH_QUERIES,
            resume_after_seconds=90,
        )


def test_worker_preserves_checkpoint_when_quota_is_exhausted() -> None:
    repository = InMemoryRepository()
    source = repository.create_source(
        source_type=SourceType.KEYWORD,
        config={"query": "FastAPI"},
    )
    job = repository.create_job(
        source_id=source.id,
        include_comments=True,
        max_videos=None,
        max_comments_per_video=1,
    )
    repository.transition_job(job.id, JobState.QUEUED, checkpoint={"pageToken": "next-page"})

    waiting = JobRunner(repository, QuotaLimitedCollector()).run(job.id)

    assert waiting.state is JobState.WAITING_QUOTA
    assert waiting.quota_bucket is QuotaBucket.SEARCH_QUERIES
    assert waiting.resume_is_automatic is True
    assert waiting.resume_at is not None
    assert waiting.checkpoint == {"pageToken": "next-page"}
    assert JobRunner(repository, QuotaLimitedCollector()).run(job.id) == waiting


class LeaseLostCollector:
    def collect(self, _job: object) -> None:
        raise LeaseLostError("job was reclaimed")


def test_worker_does_not_fail_a_job_when_it_loses_the_lease() -> None:
    repository = InMemoryRepository()
    source = repository.create_source(source_type=SourceType.VIDEO, config={"input": "dQw4w9WgXcQ"})
    job = repository.create_job(source_id=source.id, include_comments=False, max_videos=None, max_comments_per_video=None)
    repository.transition_job(job.id, JobState.RUNNING, lease_owner="new-owner")

    result = JobRunner(repository, LeaseLostCollector()).run(job.id)

    assert result.state is JobState.RUNNING
    assert result.lease_owner == "new-owner"
