from datetime import UTC, datetime
from inspect import getsource
from pathlib import Path

import pytest

from monitube_api.domain import CommentRecord, JobState, SourceType
from monitube_api.postgres_repository import PostgresRepository
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


def test_recent_failed_jobs_include_only_owned_parent_sources() -> None:
    repository = InMemoryRepository()
    owned = repository.create_source(
        source_type=SourceType.KEYWORD,
        config={"query": "owned failure"},
        owner_id="user-a",
    )
    foreign = repository.create_source(
        source_type=SourceType.KEYWORD,
        config={"query": "foreign failure"},
        owner_id="user-b",
    )
    owned_job = repository.create_job(
        source_id=owned.id,
        include_comments=False,
        max_videos=None,
        max_comments_per_video=None,
        owner_id="user-a",
    )
    foreign_job = repository.create_job(
        source_id=foreign.id,
        include_comments=False,
        max_videos=None,
        max_comments_per_video=None,
        owner_id="user-b",
    )
    repository.transition_job(owned_job.id, JobState.RUNNING)
    failed = repository.transition_job(owned_job.id, JobState.FAILED, pause_reason="owned reason")
    repository.transition_job(foreign_job.id, JobState.RUNNING)
    repository.transition_job(foreign_job.id, JobState.FAILED, pause_reason="foreign reason")

    failures = repository.list_recent_failed_parent_jobs(owner_id="user-a", limit=10)

    assert len(failures) == 1
    assert failures[0]["source_id"] == owned.id
    assert failures[0]["target_id"] is None
    assert failures[0]["source_type"] is SourceType.KEYWORD
    assert failures[0]["source_config"] == {"query": "owned failure"}
    assert failures[0]["failed_at"] == failed.updated_at
    assert failures[0]["job"].id == owned_job.id
    assert failures[0]["failed_child_count"] == 0
    assert failures[0]["representative_child_pause_reason"] is None
    assert failures[0]["representative_child_partial_errors"] == []


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


def test_summary_reads_and_cutover_gate_reject_legacy_analysis_runs() -> None:
    overview_sql = getsource(PostgresRepository.get_source_overview)
    assert overview_sql.count("run.pipeline_version = 'deterministic-v2'") == 4

    deploy_script = (
        Path(__file__).resolve().parents[3] / "scripts" / "deploy_remote.sh"
    ).read_text()
    assert "run.pipeline_version = 'deterministic-v2'" in deploy_script.replace(
        "'\"'\"'", "'"
    )
    assert "result.result_kind = 'basic_summary'" in deploy_script.replace(
        "'\"'\"'", "'"
    )


def test_readiness_requires_latest_search_statistics_migration() -> None:
    readiness_sql = getsource(PostgresRepository.check_readiness)

    assert "016_search_planner_statistics.sql" in readiness_sql
    assert "015_database_performance_foundation.sql" not in readiness_sql


def test_postgres_recent_failures_query_enforces_owner_parent_and_failed_scope() -> None:
    query_source = getsource(PostgresRepository.list_recent_failed_parent_jobs)

    assert "subscription.user_id = %s" in query_source
    assert "source.owner_id = %s" in query_source
    assert "job.updated_at >= subscription.created_at" in query_source
    assert query_source.count("job.parent_job_id IS NULL") == 2
    assert query_source.count("job.state = 'failed'") == 2
    assert "visible_parent_failures AS MATERIALIZED" in query_source
    assert "ranked_failed_children AS" in query_source
    assert "JOIN visible_parent_failures parent ON parent.id = child.parent_job_id" in query_source
    assert "count(*) OVER (PARTITION BY child.parent_job_id)" in query_source
    assert "child.failure_rank = 1" in query_source
    assert "source.target_id IS NULL" in query_source
    assert "job.target_id IS NULL" in query_source
    assert "ORDER BY failed_at DESC, id DESC" in query_source
    assert "LIMIT %s" in query_source


def test_indexed_search_materializes_candidates_before_acl_and_limit() -> None:
    class RecordingCursor:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple[object, ...]]] = []

        def __enter__(self) -> "RecordingCursor":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, statement: str, params: tuple[object, ...]) -> None:
            self.calls.append((statement, params))

        @staticmethod
        def fetchall() -> list[dict[str, object]]:
            return []

    class RecordingConnection:
        def __init__(self) -> None:
            self.db_cursor = RecordingCursor()

        def cursor(self) -> RecordingCursor:
            return self.db_cursor

        @staticmethod
        def commit() -> None:
            return None

        @staticmethod
        def rollback() -> None:
            return None

        @staticmethod
        def close() -> None:
            return None

    connection = RecordingConnection()
    repository = PostgresRepository(
        "postgresql://unused",
        connect=lambda: connection,
        enable_search_trigram=True,
    )
    owner_id = "00000000-0000-0000-0000-000000000001"

    result = repository.search_collected(
        query="video platform",
        owner_id=owner_id,
        limit=20,
        scope="all",
    )

    assert result == {"videos": [], "comments": []}
    assert len(connection.db_cursor.calls) == 2
    (video_sql, video_params), (comment_sql, comment_params) = connection.db_cursor.calls

    assert "matched_videos AS MATERIALIZED" in video_sql
    assert "matched_comments AS MATERIALIZED" in comment_sql
    assert "ILIKE ALL" not in video_sql
    assert "ILIKE ALL" not in comment_sql
    assert video_sql.count("search_document.document ILIKE %s") == 2
    assert comment_sql.count("lower(COALESCE(cm.text_display, '')) ILIKE %s") == 2

    # Candidate construction must precede ACL, while the parameterized LIMIT
    # remains after ACL. Limiting the raw match CTE can hide authorized rows
    # when higher-ranked matches belong to another user.
    video_candidate_end = video_sql.index("authorized_videos AS MATERIALIZED")
    video_detail_start = video_sql.index("SELECT v.id::text")
    comment_candidate_end = comment_sql.index("authorized_comments AS MATERIALIZED")
    comment_detail_start = comment_sql.index("SELECT cm.id::text AS comment_id")
    assert "LIMIT %s" not in video_sql[:video_candidate_end]
    assert "LIMIT %s" not in comment_sql[:comment_candidate_end]
    assert (
        video_candidate_end
        < video_sql.index("subscription.user_id")
        < video_sql.index("LIMIT %s")
        < video_detail_start
    )
    assert (
        comment_candidate_end
        < comment_sql.index("subscription.user_id")
        < comment_sql.index("LIMIT %s")
        < comment_detail_start
    )
    assert "JOIN authorized_videos search_candidate" in video_sql
    assert "JOIN comments cm ON cm.id = search_candidate.comment_id" in comment_sql

    # The potentially large first materialization contains only narrow lookup
    # and ranking columns; full comment data is fetched after ACL and LIMIT.
    matched_comment_projection = comment_sql[
        comment_sql.index("WITH matched_comments AS MATERIALIZED"):
        comment_sql.index("FROM comments cm")
    ]
    assert "cm.youtube_comment_id" not in matched_comment_projection
    assert "cm.author_display_name" not in matched_comment_projection

    expected_params = (
        "videoplatform",
        "%video%",
        "%platform%",
        "videoplatform",
        owner_id,
        owner_id,
        200,
    )
    assert video_params == expected_params
    assert comment_params == expected_params
    assert video_sql.count("%s") == len(video_params)
    assert comment_sql.count("%s") == len(comment_params)
