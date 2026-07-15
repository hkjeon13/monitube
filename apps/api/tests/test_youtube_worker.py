from datetime import timedelta

from monitube_api.domain import JobState, QuotaBucket, SourceType, utcnow
from monitube_api.repositories import InMemoryRepository
from monitube_worker.collector import YouTubeCollector
from monitube_worker.runner import JobRunner
from monitube_worker.youtube_data import YouTubeApiError


class QuotaClient:
    @staticmethod
    def bucket_for(endpoint: str) -> QuotaBucket:
        return QuotaBucket.SEARCH_QUERIES if endpoint == "search" else QuotaBucket.CORE

    def request(self, endpoint: str, _params: object):
        raise YouTubeApiError(
            endpoint=endpoint,
            bucket=self.bucket_for(endpoint),
            status_code=403,
            payload={"error": {"errors": [{"reason": "quotaExceeded"}]}},
        )


class ResumeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    @staticmethod
    def bucket_for(endpoint: str) -> QuotaBucket:
        return QuotaBucket.SEARCH_QUERIES if endpoint == "search" else QuotaBucket.CORE

    def request(self, endpoint: str, params: dict[str, object]):
        self.calls.append((endpoint, params))
        if endpoint == "search":
            return {"items": []}
        raise AssertionError(f"Unexpected endpoint: {endpoint}")


class DirectVideoClient:
    @staticmethod
    def bucket_for(_endpoint: str) -> QuotaBucket:
        return QuotaBucket.CORE

    def request(self, endpoint: str, _params: dict[str, object]):
        if endpoint == "videos":
            return {
                "items": [
                    {
                        "id": "dQw4w9WgXcQ",
                        "snippet": {"channelId": "UCabcdefghijklmnopqrstuv", "channelTitle": "Example", "title": "Demo", "publishedAt": "2025-01-02T03:04:05Z"},
                        "contentDetails": {"duration": "PT1M2S"},
                        "status": {"privacyStatus": "public", "madeForKids": False},
                        "statistics": {"viewCount": "12", "likeCount": "3", "commentCount": "1"},
                    }
                ]
            }
        if endpoint == "commentThreads":
            return {
                "items": [
                    {
                        "id": "thread-1",
                        "snippet": {
                            "topLevelComment": {
                                "id": "comment-1",
                                "snippet": {"textDisplay": "Great demo video", "likeCount": 2, "publishedAt": "2025-01-03T00:00:00Z"},
                            }
                        },
                    }
                ]
            }
        raise AssertionError(f"Unexpected endpoint: {endpoint}")


def test_quota_error_logs_checkpoint_and_due_job_resumes_from_same_cursor() -> None:
    repository = InMemoryRepository()
    source = repository.create_source(
        source_type=SourceType.KEYWORD,
        config={"query": "FastAPI", "maxPagesPerRun": 2, "includeComments": False, "maxCommentPagesPerVideo": 1},
    )
    job = repository.create_job(source_id=source.id, include_comments=False, max_videos=None, max_comments_per_video=None)
    repository.transition_job(
        job.id,
        JobState.RUNNING,
        checkpoint={"stage": "keyword_search", "scopeKey": "FastAPI", "pageToken": "resume-page", "batchCursor": 1},
    )

    waiting = JobRunner(repository, YouTubeCollector(repository, QuotaClient())).run(job.id)

    assert waiting.state is JobState.WAITING_QUOTA
    assert waiting.checkpoint["pageToken"] == "resume-page"
    assert waiting.resume_is_automatic is True
    assert repository._request_logs[-1]["endpoint"] == "search"  # verifies durable-boundary logging in the fallback repository
    assert repository._request_logs[-1]["bucket"] == "search_queries"

    repository.transition_job(waiting.id, JobState.WAITING_QUOTA, resume_at=utcnow() - timedelta(seconds=1))
    claimed = repository.claim_next_job(worker_id="test-worker")
    assert claimed is not None and claimed.state is JobState.RUNNING
    assert claimed.checkpoint["pageToken"] == "resume-page"

    resume_client = ResumeClient()
    completed = JobRunner(repository, YouTubeCollector(repository, resume_client)).run(claimed.id)

    assert completed.state is JobState.COMPLETED
    # Discovery restarts from the frozen query window rather than using a bare page
    # token that would omit IDs from earlier, not-yet-linked pages.
    assert resume_client.calls[0] == ("search", {
        "part": "snippet",
        "type": "video",
        "q": "FastAPI",
        "order": "date",
        "publishedAfter": None,
        "publishedBefore": None,
        "regionCode": None,
        "relevanceLanguage": None,
        "maxResults": 50,
        "pageToken": None,
    })


def test_expired_running_lease_is_reclaimed_and_active_owner_can_renew() -> None:
    repository = InMemoryRepository()
    source = repository.create_source(
        source_type=SourceType.VIDEO,
        config={"input": "dQw4w9WgXcQ", "includeComments": False, "maxCommentPagesPerVideo": 1},
    )
    job = repository.create_job(source_id=source.id, include_comments=False, max_videos=None, max_comments_per_video=None)
    repository.transition_job(
        job.id,
        JobState.RUNNING,
        lease_owner="dead-worker",
        lease_expires_at=utcnow() - timedelta(seconds=1),
        checkpoint={"stage": "comments", "scopeKey": "dQw4w9WgXcQ", "pageToken": "next"},
    )

    reclaimed = repository.claim_next_job(worker_id="live-worker", lease_seconds=30)

    assert reclaimed is not None
    assert reclaimed.state is JobState.RUNNING
    assert reclaimed.lease_owner == "live-worker"
    assert reclaimed.current_stage == "reclaimed"
    assert reclaimed.checkpoint["pageToken"] == "next"
    assert repository.renew_job_lease(job_id=reclaimed.id, worker_id="live-worker", lease_seconds=60) is True
    assert repository.renew_job_lease(job_id=reclaimed.id, worker_id="dead-worker", lease_seconds=60) is False


def test_direct_video_collection_persists_video_comments_and_summary() -> None:
    repository = InMemoryRepository()
    source = repository.create_source(
        source_type=SourceType.VIDEO,
        config={"input": "dQw4w9WgXcQ", "includeComments": True, "maxCommentPagesPerVideo": 1},
    )
    job = repository.create_job(source_id=source.id, include_comments=False, max_videos=None, max_comments_per_video=None)

    completed = JobRunner(repository, YouTubeCollector(repository, DirectVideoClient())).run(job.id)
    result = repository.get_source_results(source.id)

    assert completed.state is JobState.COMPLETED
    assert result["videos"][0].title == "Demo"
    assert result["comments"][0].youtube_comment_id == "comment-1"
    assert result["analysis"]["topWords"][0]["word"] == "demo"
