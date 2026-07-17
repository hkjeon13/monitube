from dataclasses import replace
from datetime import timedelta

from monitube_api.domain import CommentRecord, JobState, QuotaBucket, SourceType, VideoRecord, new_id, utcnow
from monitube_api.repositories import InMemoryRepository
from monitube_worker.collector import YouTubeCollector, quota_retry_delay_seconds
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
    assert waiting.checkpoint["quotaRetryAttempt"] == 1
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


def test_quota_retry_delay_is_bounded_between_one_and_three_hours() -> None:
    assert quota_retry_delay_seconds({}) == 3_600
    assert quota_retry_delay_seconds({"quotaRetryAttempt": 1}) == 7_200
    assert quota_retry_delay_seconds({"quotaRetryAttempt": 2}) == 10_800
    assert quota_retry_delay_seconds({"quotaRetryAttempt": 99}) == 10_800


class FullChannelClient:
    def __init__(self) -> None:
        self.playlist_calls = 0
        self.comment_calls = 0

    @staticmethod
    def bucket_for(_endpoint: str) -> QuotaBucket:
        return QuotaBucket.CORE

    def request(self, endpoint: str, _params: dict[str, object]):
        if endpoint == "channels":
            return {"items": [{"id": "UCabcdefghijklmnopqrstuv", "snippet": {"title": "Example"}, "contentDetails": {"relatedPlaylists": {"uploads": "UUexample"}}}]}
        if endpoint == "playlistItems":
            self.playlist_calls += 1
            video_id = "dQw4w9WgXcQ" if self.playlist_calls == 1 else "M7lc1UVf-VE"
            return {"items": [{"contentDetails": {"videoId": video_id}}], **({"nextPageToken": "second"} if self.playlist_calls == 1 else {})}
        if endpoint == "videos":
            return {"items": []}
        if endpoint == "commentThreads":
            self.comment_calls += 1
            return {"items": [], **({"nextPageToken": "second"} if self.comment_calls == 1 else {})}
        raise AssertionError(f"Unexpected endpoint: {endpoint}")


def test_channel_all_content_flags_continue_past_legacy_numeric_limits() -> None:
    repository = InMemoryRepository()
    source = repository.create_source(
        source_type=SourceType.CHANNEL,
        config={
            "input": "@example",
            "includeComments": True,
            "collectAllVideos": True,
            "collectAllComments": True,
            "maxVideos": 1,
            "maxCommentPagesPerVideo": 1,
        },
    )
    job = repository.create_job(source_id=source.id, include_comments=True, max_videos=1, max_comments_per_video=1)
    client = FullChannelClient()

    completed = JobRunner(repository, YouTubeCollector(repository, client)).run(job.id)

    assert completed.state is JobState.COMPLETED
    assert client.playlist_calls == 2
    assert completed.checkpoint["phaseProgress"]["videos"] == {"completed": 2, "total": 2}
    assert completed.checkpoint["phaseProgress"]["comments"] == {"completed": 0, "total": 0}


class IncrementalChannelClient:
    def __init__(self) -> None:
        self.playlist_calls = 0
        self.comment_requests: list[dict[str, object]] = []

    @staticmethod
    def bucket_for(_endpoint: str) -> QuotaBucket:
        return QuotaBucket.CORE

    def request(self, endpoint: str, params: dict[str, object]):
        if endpoint == "channels":
            return {"items": [{"id": "UCabcdefghijklmnopqrstuv", "snippet": {"title": "Example"}, "contentDetails": {"relatedPlaylists": {"uploads": "UUexample"}}}]}
        if endpoint == "playlistItems":
            self.playlist_calls += 1
            return {"items": [{"contentDetails": {"videoId": "dQw4w9WgXcQ"}}, {"contentDetails": {"videoId": "M7lc1UVf-VE"}}], "nextPageToken": "older"}
        if endpoint == "videos":
            return {"items": [
                {"id": "dQw4w9WgXcQ", "snippet": {"channelId": "UCabcdefghijklmnopqrstuv", "title": "Newest known", "publishedAt": "2025-01-02T03:04:05Z"}, "contentDetails": {"duration": "PT1M"}, "status": {}, "statistics": {"commentCount": "2"}},
                {"id": "M7lc1UVf-VE", "snippet": {"channelId": "UCabcdefghijklmnopqrstuv", "title": "Also known", "publishedAt": "2025-01-01T03:04:05Z"}, "contentDetails": {"duration": "PT1M"}, "status": {}, "statistics": {"commentCount": "0"}},
            ]}
        if endpoint == "commentThreads":
            self.comment_requests.append(params)
            return {"items": [{"id": "thread-known", "snippet": {"topLevelComment": {"id": "comment-known", "snippet": {"textDisplay": "Already stored", "publishedAt": "2025-01-02T03:04:05Z"}}}}], "nextPageToken": "older-comments"}
        raise AssertionError(f"Unexpected endpoint: {endpoint}")


def test_channel_refresh_stops_at_known_upload_page_and_comment_page() -> None:
    repository = InMemoryRepository()
    for video_id, comments in (("dQw4w9WgXcQ", 1), ("M7lc1UVf-VE", 0)):
        repository.upsert_video(VideoRecord(
            id=new_id(), youtube_video_id=video_id, youtube_channel_id="UCabcdefghijklmnopqrstuv",
            title="Stored", description=None, published_at=None, duration_seconds=None,
            privacy_status="public", made_for_kids=False,
            statistics={"viewCount": 0, "likeCount": 0, "commentCount": comments}, source_fetched_at=utcnow(),
        ))
    repository.upsert_comment(CommentRecord(
        id=new_id(), youtube_comment_id="comment-known", youtube_video_id="dQw4w9WgXcQ",
        youtube_parent_comment_id=None, youtube_thread_id="thread-known", text_display="Already stored",
        like_count=0, published_at=None, updated_at=None, source_fetched_at=utcnow(),
    ))
    source = repository.create_source(
        source_type=SourceType.CHANNEL,
        config={"input": "@example", "includeComments": True, "collectAllVideos": True, "collectAllComments": True},
    )
    repository._sources[source.id] = replace(source, coverage={"complete": True, "collectAllVideos": True})
    job = repository.create_job(source_id=source.id, include_comments=True, max_videos=None, max_comments_per_video=None)
    client = IncrementalChannelClient()

    completed = JobRunner(repository, YouTubeCollector(repository, client)).run(job.id)

    assert completed.state is JobState.COMPLETED
    assert client.playlist_calls == 1
    assert len(client.comment_requests) == 1
    assert client.comment_requests[0]["videoId"] == "dQw4w9WgXcQ"
    assert client.comment_requests[0]["order"] == "time"


def test_incomplete_comment_collection_prioritizes_lowest_coverage_then_oldest_video() -> None:
    repository = InMemoryRepository()
    collector = YouTubeCollector(repository, DirectVideoClient())
    now = utcnow()
    videos = [
        VideoRecord(
            id=new_id(), youtube_video_id="partially-collected", youtube_channel_id=None,
            title=None, description=None, published_at=now - timedelta(days=30), duration_seconds=None,
            privacy_status=None, made_for_kids=None, statistics={"commentCount": 10}, source_fetched_at=now,
        ),
        VideoRecord(
            id=new_id(), youtube_video_id="newer-uncollected", youtube_channel_id=None,
            title=None, description=None, published_at=now - timedelta(days=5), duration_seconds=None,
            privacy_status=None, made_for_kids=None, statistics={"commentCount": 10}, source_fetched_at=now,
        ),
        VideoRecord(
            id=new_id(), youtube_video_id="older-uncollected", youtube_channel_id=None,
            title=None, description=None, published_at=now - timedelta(days=20), duration_seconds=None,
            privacy_status=None, made_for_kids=None, statistics={"commentCount": 10}, source_fetched_at=now,
        ),
    ]

    prioritized = collector._prioritize_comment_collection(
        videos, {"partially-collected": 1, "newer-uncollected": 0, "older-uncollected": 0}
    )

    assert [video.youtube_video_id for video in prioritized] == [
        "older-uncollected",
        "newer-uncollected",
        "partially-collected",
    ]


class ResumedChannelClient:
    def __init__(self) -> None:
        self.comment_calls = 0

    @staticmethod
    def bucket_for(_endpoint: str) -> QuotaBucket:
        return QuotaBucket.CORE

    def request(self, endpoint: str, _params: dict[str, object]):
        if endpoint == "channels":
            return {"items": [{"id": "UCabcdefghijklmnopqrstuv", "snippet": {"title": "Example"}, "contentDetails": {"relatedPlaylists": {"uploads": "UUexample"}}}]}
        if endpoint == "playlistItems":
            return {"items": [{"contentDetails": {"videoId": "dQw4w9WgXcQ"}}]}
        if endpoint == "videos":
            return {"items": [{
                "id": "dQw4w9WgXcQ", "snippet": {"channelId": "UCabcdefghijklmnopqrstuv", "title": "Known", "publishedAt": "2025-01-02T03:04:05Z"},
                "contentDetails": {"duration": "PT1M"}, "status": {}, "statistics": {"commentCount": "1"},
            }]}
        if endpoint == "commentThreads":
            self.comment_calls += 1
            return {"items": []}
        raise AssertionError(f"Unexpected endpoint: {endpoint}")


def test_resumed_incomplete_channel_skips_video_with_all_comments_already_persisted() -> None:
    repository = InMemoryRepository()
    repository.upsert_video(VideoRecord(
        id=new_id(), youtube_video_id="dQw4w9WgXcQ", youtube_channel_id="UCabcdefghijklmnopqrstuv",
        title="Known", description=None, published_at=None, duration_seconds=None,
        privacy_status="public", made_for_kids=False, statistics={"commentCount": 1}, source_fetched_at=utcnow(),
    ))
    repository.upsert_comment(CommentRecord(
        id=new_id(), youtube_comment_id="comment-known", youtube_video_id="dQw4w9WgXcQ",
        youtube_parent_comment_id=None, youtube_thread_id="thread-known", text_display="Already stored",
        like_count=0, published_at=None, updated_at=None, source_fetched_at=utcnow(),
    ))
    source = repository.create_source(
        source_type=SourceType.CHANNEL,
        config={"input": "@example", "includeComments": True, "collectAllVideos": True, "collectAllComments": True},
    )
    job = repository.create_job(source_id=source.id, include_comments=True, max_videos=None, max_comments_per_video=None)
    client = ResumedChannelClient()

    completed = JobRunner(repository, YouTubeCollector(repository, client)).run(job.id)

    assert completed.state is JobState.COMPLETED
    assert client.comment_calls == 0
    assert completed.checkpoint["phaseProgress"]["comments"] == {"completed": 1, "total": 1}


class HistoricalBackfillClient:
    def __init__(self) -> None:
        self.playlist_calls = 0

    @staticmethod
    def bucket_for(_endpoint: str) -> QuotaBucket:
        return QuotaBucket.CORE

    def request(self, endpoint: str, _params: dict[str, object]):
        if endpoint == "channels":
            return {
                "items": [{
                    "id": "UCabcdefghijklmnopqrstuv", "snippet": {"title": "Example"},
                    "contentDetails": {"relatedPlaylists": {"uploads": "UUexample"}},
                    "statistics": {"videoCount": "4"},
                }]
            }
        if endpoint == "playlistItems":
            self.playlist_calls += 1
            if self.playlist_calls == 1:
                return {
                    "items": [
                        {"contentDetails": {"videoId": "newest-1"}},
                        {"contentDetails": {"videoId": "newest-2"}},
                    ],
                    "nextPageToken": "older-page",
                }
            return {"items": [
                {"contentDetails": {"videoId": "oldest-1"}},
                {"contentDetails": {"videoId": "oldest-2"}},
            ]}
        raise AssertionError(f"Unexpected endpoint: {endpoint}")


def test_channel_count_deficit_continues_to_oldest_playlist_pages() -> None:
    repository = InMemoryRepository()
    channel_id = "UCabcdefghijklmnopqrstuv"
    for video_id in ("newest-1", "newest-2"):
        repository.upsert_video(VideoRecord(
            id=new_id(), youtube_video_id=video_id, youtube_channel_id=channel_id,
            title="Stored", description=None, published_at=None, duration_seconds=None,
            privacy_status="public", made_for_kids=False, statistics={}, source_fetched_at=utcnow(),
        ))
    source = repository.create_source(
        source_type=SourceType.CHANNEL,
        config={"input": "@example", "includeComments": False, "collectAllVideos": True},
    )
    job = repository.create_job(source_id=source.id, include_comments=False, max_videos=None, max_comments_per_video=None)
    client = HistoricalBackfillClient()

    ids, known_videos, backfill_required = YouTubeCollector(repository, client)._channel_video_ids(
        job, source.config, incremental_refresh=False
    )

    assert backfill_required is True
    assert client.playlist_calls == 2
    assert ids == ["oldest-2", "oldest-1", "newest-2", "newest-1"]
    assert set(known_videos) == {"newest-1", "newest-2"}


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
