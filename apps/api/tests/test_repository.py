from datetime import UTC, datetime

import pytest

from monitube_api.domain import CommentRecord, JobState, SourceType
from monitube_api.repositories import InMemoryRepository, InvalidStateTransitionError


def test_repository_isolated_config_and_source_lifecycle() -> None:
    repository = InMemoryRepository()
    source = repository.create_source(
        source_type=SourceType.CHANNEL,
        config={"input": "@GoogleDevelopers", "nested": {"limit": 10}},
    )

    source.config["nested"]["limit"] = 999
    persisted = repository.get_source(source.id)

    assert persisted.config["nested"]["limit"] == 10
    assert repository.list_sources()[0].id == source.id


def test_job_state_machine_preserves_checkpoint_when_waiting_for_quota() -> None:
    repository = InMemoryRepository()
    source = repository.create_source(source_type=SourceType.KEYWORD, config={"query": "fastapi"})
    job = repository.create_job(
        source_id=source.id,
        include_comments=True,
        max_videos=None,
        max_comments_per_video=2,
    )

    waiting = repository.transition_job(
        job.id,
        JobState.WAITING_QUOTA,
        checkpoint={"pageToken": "next-page"},
        pause_reason="daily quota exhausted",
        resume_is_automatic=True,
    )

    assert waiting.state is JobState.WAITING_QUOTA
    assert waiting.checkpoint == {"pageToken": "next-page"}
    assert waiting.resume_is_automatic is True
    assert repository.transition_job(job.id, JobState.QUEUED).state is JobState.QUEUED


def test_terminal_job_cannot_be_started_again() -> None:
    repository = InMemoryRepository()
    source = repository.create_source(source_type=SourceType.CHANNEL, config={"input": "@channel"})
    job = repository.create_job(source_id=source.id, include_comments=False, max_videos=1, max_comments_per_video=1)
    repository.transition_job(job.id, JobState.RUNNING)
    repository.transition_job(job.id, JobState.COMPLETED)

    with pytest.raises(InvalidStateTransitionError):
        repository.transition_job(job.id, JobState.RUNNING)


def test_comment_page_persists_rows_and_checkpoint_together() -> None:
    repository = InMemoryRepository()
    source = repository.create_source(source_type=SourceType.VIDEO, config={"input": "pageVideo01"})
    job = repository.create_job(
        source_id=source.id,
        include_comments=True,
        max_videos=1,
        max_comments_per_video=2,
    )
    fetched_at = datetime(2025, 1, 1, tzinfo=UTC)
    comment = CommentRecord(
        id="comment-row",
        youtube_comment_id="comment-id",
        youtube_video_id="pageVideo01",
        youtube_parent_comment_id=None,
        youtube_thread_id="thread-id",
        text_display="persisted as one page",
        like_count=0,
        published_at=fetched_at,
        updated_at=None,
        source_fetched_at=fetched_at,
    )

    stored = repository.persist_comment_page(
        [comment],
        job_id=job.id,
        checkpoint={"commentPageToken": "next"},
    )

    assert [item.youtube_comment_id for item in stored] == ["comment-id"]
    assert repository.existing_comment_ids(["comment-id"]) == {"comment-id"}
    assert repository.get_job(job.id).checkpoint == {"commentPageToken": "next"}
