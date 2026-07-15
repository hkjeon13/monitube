from datetime import UTC, datetime

from fastapi.testclient import TestClient

from monitube_api.domain import CommentRecord, SourceType, VideoRecord
from monitube_api.main import create_app
from monitube_api.repositories import InMemoryRepository


def test_source_results_and_video_comments_are_queryable() -> None:
    repository = InMemoryRepository()
    source = repository.create_source(
        source_type=SourceType.VIDEO,
        config={"input": "dQw4w9WgXcQ", "includeComments": True, "maxCommentPagesPerVideo": 1},
    )
    video = repository.upsert_video(
        VideoRecord(
            id="video-row",
            youtube_video_id="dQw4w9WgXcQ",
            youtube_channel_id="UCabcdefghijklmnopqrstuv",
            title="A video",
            description="Example",
            published_at=datetime(2025, 1, 2, tzinfo=UTC),
            duration_seconds=42,
            privacy_status="public",
            made_for_kids=False,
            statistics={"viewCount": 12, "likeCount": 3, "commentCount": 1},
            source_fetched_at=datetime(2025, 1, 3, tzinfo=UTC),
        )
    )
    repository.link_source_video(source.id, video.youtube_video_id)
    repository.upsert_comment(
        CommentRecord(
            id="comment-row",
            youtube_comment_id="comment-1",
            youtube_video_id=video.youtube_video_id,
            youtube_parent_comment_id=None,
            youtube_thread_id="thread-1",
            text_display="Great FastAPI video",
            like_count=2,
            published_at=datetime(2025, 1, 4, tzinfo=UTC),
            updated_at=None,
            source_fetched_at=datetime(2025, 1, 4, tzinfo=UTC),
        )
    )
    repository.save_analysis_summary(source.id)
    client = TestClient(create_app(repository=repository))

    results = client.get(f"/v1/sources/{source.id}/results")
    comments = client.get(f"/v1/videos/{video.youtube_video_id}/comments")

    assert results.status_code == 200
    assert results.json()["analysis"]["videoCount"] == 1
    assert results.json()["commentSummary"]["total"] == 1
    assert results.json()["videos"][0]["id"] == video.youtube_video_id
    assert comments.status_code == 200
    assert comments.json()["comments"][0]["text"] == "Great FastAPI video"
