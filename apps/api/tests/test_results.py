from datetime import UTC, datetime

from fastapi.testclient import TestClient

from monitube_api.domain import CommentRecord, VideoRecord
from monitube_api.main import create_app
from monitube_api.repositories import InMemoryRepository


def _subscribed_video_source(repository: InMemoryRepository, video_id: str) -> tuple[TestClient, str, str]:
    """Create the public subscription plus its internal worker source for fixtures."""

    client = TestClient(create_app(repository=repository))
    response = client.post(
        "/v1/collection-requests",
        json={"type": "video", "config": {"input": video_id, "includeComments": True}},
    )
    assert response.status_code == 201
    body = response.json()
    worker_source_id = next(
        source.id for source in repository._sources.values() if source.target_id == body["targetId"]
    )
    return client, body["source"]["id"], worker_source_id


def test_source_results_and_video_comments_are_queryable() -> None:
    repository = InMemoryRepository()
    client, source_id, worker_source_id = _subscribed_video_source(repository, "dQw4w9WgXcQ")
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
    repository.link_source_video(worker_source_id, video.youtube_video_id)
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
    repository.save_analysis_summary(worker_source_id)

    results = client.get(f"/v1/sources/{source_id}/results")
    comments = client.get(f"/v1/videos/{video.youtube_video_id}/comments")

    assert results.status_code == 200
    assert results.json()["analysis"]["videoCount"] == 1
    assert results.json()["commentSummary"]["total"] == 1
    assert results.json()["videos"][0]["id"] == video.youtube_video_id
    assert comments.status_code == 200
    assert comments.json()["comments"][0]["text"] == "Great FastAPI video"


def test_comment_detail_includes_replies_and_other_comments_from_the_same_author() -> None:
    repository = InMemoryRepository()
    client, _, worker_source_id = _subscribed_video_source(repository, "dQw4w9WgXcQ")
    video = repository.upsert_video(
        VideoRecord(
            id="video-row", youtube_video_id="dQw4w9WgXcQ", youtube_channel_id="UCabcdefghijklmnopqrstuv",
            title="A video", description="Example", published_at=datetime(2025, 1, 2, tzinfo=UTC),
            duration_seconds=42, privacy_status="public", made_for_kids=False, statistics={},
            source_fetched_at=datetime(2025, 1, 3, tzinfo=UTC),
        )
    )
    repository.link_source_video(worker_source_id, video.youtube_video_id)
    for comment_id, text in (("comment-1", "첫 댓글"), ("comment-2", "다른 댓글")):
        repository.upsert_comment(
            CommentRecord(
                id=f"row-{comment_id}", youtube_comment_id=comment_id, youtube_video_id=video.youtube_video_id,
                youtube_parent_comment_id=None, youtube_thread_id=comment_id, text_display=text,
                author_channel_id="UCauthor", author_display_name="작성자", like_count=0,
                published_at=datetime(2025, 1, 4, tzinfo=UTC), updated_at=None,
                source_fetched_at=datetime(2025, 1, 4, tzinfo=UTC),
            )
        )
    repository.upsert_comment(
        CommentRecord(
            id="row-reply-2", youtube_comment_id="reply-2", youtube_video_id=video.youtube_video_id,
            youtube_parent_comment_id="comment-1", youtube_thread_id="comment-1", text_display="작성자가 아닌 답글",
            author_channel_id="UCreplier", author_display_name="답글 작성자", like_count=1,
            published_at=datetime(2025, 1, 5, tzinfo=UTC), updated_at=None,
            source_fetched_at=datetime(2025, 1, 5, tzinfo=UTC),
        )
    )
    repository.upsert_comment(
        CommentRecord(
            id="row-reply-1", youtube_comment_id="reply-1", youtube_video_id=video.youtube_video_id,
            youtube_parent_comment_id="comment-1", youtube_thread_id="comment-1", text_display="첫 번째 답글",
            author_channel_id="UCauthor", author_display_name="작성자", like_count=0,
            published_at=datetime(2025, 1, 4, 12, tzinfo=UTC), updated_at=None,
            source_fetched_at=datetime(2025, 1, 4, 12, tzinfo=UTC),
        )
    )
    repository.upsert_comment(
        CommentRecord(
            id="row-unrelated-reply", youtube_comment_id="unrelated-reply", youtube_video_id=video.youtube_video_id,
            youtube_parent_comment_id="comment-2", youtube_thread_id="comment-2", text_display="다른 댓글의 답글",
            author_channel_id="UCother", author_display_name="다른 답글 작성자", like_count=0,
            published_at=datetime(2025, 1, 6, tzinfo=UTC), updated_at=None,
            source_fetched_at=datetime(2025, 1, 6, tzinfo=UTC),
        )
    )
    detail = client.get("/v1/comments/comment-1")

    assert detail.status_code == 200
    assert detail.json()["comment"]["authorChannelId"] == "UCauthor"
    assert [reply["id"] for reply in detail.json()["replies"]] == ["reply-1", "reply-2"]
    assert detail.json()["replies"][0]["parentCommentId"] == "comment-1"
    # A self-reply is shown only in the dedicated reply thread, not duplicated
    # among the author's other comments.
    assert [item["comment"]["id"] for item in detail.json()["authorComments"]] == ["comment-2"]


def test_unified_search_finds_titles_and_tolerates_a_comment_typo() -> None:
    repository = InMemoryRepository()
    client, _, worker_source_id = _subscribed_video_source(repository, "dQw4w9WgXcQ")
    channel_id = "UCabcdefghijklmnopqrstuv"
    repository.upsert_channel({
        "id": "channel-row", "youtube_channel_id": channel_id, "handle": "@fastapi",
        "title": "FastAPI Korea", "description": "API engineering", "source_fetched_at": datetime(2025, 1, 3, tzinfo=UTC),
    })
    video = repository.upsert_video(
        VideoRecord(
            id="video-row", youtube_video_id="dQw4w9WgXcQ", youtube_channel_id=channel_id,
            title="FastAPI 배포 가이드", description="서비스를 안전하게 배포합니다.",
            published_at=datetime(2025, 1, 2, tzinfo=UTC), duration_seconds=42,
            privacy_status="public", made_for_kids=False,
            statistics={"viewCount": 12, "likeCount": 3, "commentCount": 1}, source_fetched_at=datetime(2025, 1, 3, tzinfo=UTC),
        )
    )
    repository.link_source_video(worker_source_id, video.youtube_video_id)
    repository.upsert_comment(
        CommentRecord(
            id="comment-row", youtube_comment_id="comment-1", youtube_video_id=video.youtube_video_id,
            youtube_parent_comment_id=None, youtube_thread_id="thread-1", text_display="배포 설명이 정말 좋아요",
            like_count=2, published_at=datetime(2025, 1, 4, tzinfo=UTC), updated_at=None,
            source_fetched_at=datetime(2025, 1, 4, tzinfo=UTC),
        )
    )
    title = client.get("/v1/search", params={"q": "배포"})
    videos_only = client.get("/v1/search", params={"q": "배포", "scope": "videos"})
    comments_only = client.get("/v1/search", params={"q": "배포", "scope": "comments"})
    typo = client.get("/v1/search", params={"q": "설먕이"})
    title_only_comment = client.get("/v1/search", params={"q": "FastAPI"})

    assert title.status_code == 200
    assert title.json()["videos"][0]["video"]["title"] == "FastAPI 배포 가이드"
    assert len(videos_only.json()["videos"]) == 1
    assert videos_only.json()["comments"] == []
    assert comments_only.json()["videos"] == []
    assert len(comments_only.json()["comments"]) == 1
    assert typo.status_code == 200
    assert typo.json()["comments"][0]["comment"]["id"] == "comment-1"
    assert title_only_comment.status_code == 200
    assert title_only_comment.json()["comments"] == []
