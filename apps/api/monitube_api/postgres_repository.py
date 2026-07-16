"""psycopg-backed repository for server-managed collection jobs and public data."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
import hashlib
from typing import Any, Iterator

try:  # Keep the in-memory API usable when optional runtime dependencies are absent.
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Json
except ImportError:  # pragma: no cover - exercised only in minimal local installs
    psycopg = None  # type: ignore[assignment]
    dict_row = None  # type: ignore[assignment]

    class Json:  # type: ignore[no-redef]
        def __init__(self, value: Any) -> None:
            self.value = value

from .analysis import build_summary
from .domain import (
    CollectionRequestRecord,
    CollectionSubmission,
    CollectionTargetRecord,
    CommentRecord,
    JobRecord,
    JobState,
    QuotaBucket,
    SourceRecord,
    SourceType,
    VideoRecord,
    new_id,
    utcnow,
)
from .repositories import CollectionRepository, InvalidStateTransitionError, NotFoundError, RepositoryError, _ALLOWED_TRANSITIONS


class PostgresRepository(CollectionRepository):
    """Synchronous PostgreSQL repository used by FastAPI and the polling worker.

    The class persists an opaque secret reference and a fingerprint in
    ``youtube_runtime_configs``. It intentionally has no parameter or column for a
    raw API key.
    """

    def __init__(self, database_url: str, *, connect: Any | None = None) -> None:
        if not database_url:
            raise ValueError("database_url is required")
        self.database_url = database_url
        self._connect_override = connect

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        if self._connect_override is not None:
            connection = self._connect_override()
        else:
            if psycopg is None:
                raise RuntimeError("psycopg is required when DATABASE_URL is configured")
            connection = psycopg.connect(self.database_url, row_factory=dict_row)
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _source(row: dict[str, Any]) -> SourceRecord:
        return SourceRecord(
            id=str(row["id"]),
            type=SourceType(row["type"]),
            config=dict(row["config"] or {}),
            enabled=bool(row["enabled"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            next_run_at=row.get("next_run_at"),
            target_id=str(row["target_id"]) if row.get("target_id") else None,
            canonical_key=row.get("canonical_key"),
            coverage=dict(row.get("coverage") or {}),
            last_completed_at=row.get("last_completed_at"),
        )

    @staticmethod
    def _target(row: dict[str, Any]) -> CollectionTargetRecord:
        return CollectionTargetRecord(
            id=str(row["id"]),
            type=SourceType(row["type"]),
            canonical_key=str(row["canonical_key"]),
            config=dict(row.get("config") or {}),
            coverage=dict(row.get("coverage") or {}),
            resolved_channel_id=str(row["resolved_channel_id"]) if row.get("resolved_channel_id") else None,
            last_completed_at=row.get("last_completed_at"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _request(row: dict[str, Any]) -> CollectionRequestRecord:
        return CollectionRequestRecord(
            id=str(row["id"]),
            target_id=str(row["target_id"]),
            source_id=str(row["source_id"]) if row.get("source_id") else None,
            request_config=dict(row.get("request_config") or {}),
            idempotency_key=row.get("idempotency_key"),
            job_id=str(row["job_id"]) if row.get("job_id") else None,
            status=str(row["status"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _json_safe(value: Any) -> Any:
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, dict):
            return {key: PostgresRepository._json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [PostgresRepository._json_safe(item) for item in value]
        return value

    @staticmethod
    def _job(row: dict[str, Any]) -> JobRecord:
        return JobRecord(
            id=str(row["id"]),
            source_id=str(row["source_id"]),
            state=JobState(row["state"]),
            current_stage=row["current_stage"],
            progress_completed=int(row.get("progress_completed") or 0),
            progress_total=row.get("progress_total"),
            progress_unit=row.get("progress_unit") or "sources",
            include_comments=bool(row.get("include_comments")),
            max_videos=row.get("max_videos"),
            max_comments_per_video=row.get("max_comments_per_video"),
            checkpoint=dict(row.get("checkpoint") or {}),
            pause_reason=row.get("pause_reason"),
            quota_bucket=QuotaBucket(row["quota_bucket"]) if row.get("quota_bucket") else None,
            resume_at=row.get("resume_at"),
            resume_is_automatic=bool(row.get("resume_is_automatic")),
            partial_errors=list(row.get("partial_errors") or []),
            runtime_config_id=str(row["runtime_config_id"]) if row.get("runtime_config_id") else None,
            lease_owner=row.get("lease_owner"),
            lease_expires_at=row.get("lease_expires_at"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            target_id=str(row["target_id"]) if row.get("target_id") else None,
        )

    @staticmethod
    def _video(row: dict[str, Any]) -> VideoRecord:
        stats = row.get("statistics") or {}
        return VideoRecord(
            id=str(row["id"]),
            youtube_video_id=row["youtube_video_id"],
            youtube_channel_id=row.get("youtube_channel_id"),
            title=row.get("title"),
            description=row.get("description"),
            published_at=row.get("published_at"),
            duration_seconds=row.get("duration_seconds"),
            privacy_status=row.get("privacy_status"),
            made_for_kids=row.get("made_for_kids"),
            statistics={key: int(value or 0) for key, value in stats.items() if key in {"viewCount", "likeCount", "commentCount"}},
            source_fetched_at=row.get("source_fetched_at") or utcnow(),
        )

    @staticmethod
    def _comment(row: dict[str, Any]) -> CommentRecord:
        return CommentRecord(
            id=str(row["id"]),
            youtube_comment_id=row["youtube_comment_id"],
            youtube_video_id=row["youtube_video_id"],
            youtube_parent_comment_id=row.get("youtube_parent_comment_id"),
            youtube_thread_id=row.get("youtube_thread_id"),
            text_display=row.get("text_display"),
            like_count=int(row.get("like_count") or 0),
            published_at=row.get("published_at"),
            updated_at=row.get("updated_at"),
            source_fetched_at=row.get("source_fetched_at") or utcnow(),
        )

    def bootstrap_runtime_config(
        self, *, environment: str, google_project_number: str, secret_ref: str, key_fingerprint: str | None
    ) -> str:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO youtube_runtime_configs (environment, google_project_number, secret_ref, key_fingerprint, status)
                VALUES (%s, %s, %s, %s, 'active')
                ON CONFLICT (environment, google_project_number) DO UPDATE
                SET secret_ref = EXCLUDED.secret_ref,
                    key_fingerprint = EXCLUDED.key_fingerprint,
                    status = 'active',
                    retired_at = NULL
                RETURNING id::text
                """,
                (environment, google_project_number, secret_ref, key_fingerprint),
            )
            return str(cursor.fetchone()["id"])

    @staticmethod
    def _select_source(cursor: Any, source_id: str) -> dict[str, Any] | None:
        cursor.execute(
            """
            SELECT cs.id::text, cs.type::text, cs.config, cs.enabled, cs.created_at, cs.updated_at, cs.next_run_at,
                   cs.target_id::text, ct.canonical_key, ct.coverage, ct.last_completed_at
            FROM collection_sources cs
            LEFT JOIN collection_targets ct ON ct.id = cs.target_id
            WHERE cs.id = %s
            """,
            (source_id,),
        )
        return cursor.fetchone()

    def create_source(self, *, source_type: SourceType, config: dict[str, Any]) -> SourceRecord:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO collection_sources (type, config)
                VALUES (%s, %s)
                RETURNING id::text
                """,
                (source_type.value, Json(config)),
            )
            row = self._select_source(cursor, str(cursor.fetchone()["id"]))
            assert row is not None
            return self._source(row)

    def get_source(self, source_id: str) -> SourceRecord:
        with self._connection() as connection, connection.cursor() as cursor:
            row = self._select_source(cursor, source_id)
            if not row:
                raise NotFoundError(f"Source '{source_id}' was not found")
            return self._source(row)

    def list_sources(self) -> list[SourceRecord]:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT cs.id::text, cs.type::text, cs.config, cs.enabled, cs.created_at, cs.updated_at, cs.next_run_at,
                       cs.target_id::text, ct.canonical_key, ct.coverage, ct.last_completed_at
                FROM collection_sources cs
                LEFT JOIN collection_targets ct ON ct.id = cs.target_id
                WHERE cs.target_id IS NULL
                   OR cs.id = (
                     SELECT cr.source_id
                     FROM collection_requests cr
                     JOIN collection_sources primary_source ON primary_source.id = cr.source_id
                     WHERE cr.target_id = cs.target_id AND cr.source_id IS NOT NULL
                     ORDER BY (COALESCE(primary_source.config ->> 'includeComments', 'false') = 'true') DESC,
                              COALESCE((primary_source.config ->> 'maxVideos')::integer, 0) DESC,
                              COALESCE((primary_source.config ->> 'maxPagesPerRun')::integer, 0) DESC,
                              COALESCE((primary_source.config ->> 'maxCommentPagesPerVideo')::integer, 0) DESC,
                              cr.created_at, cr.id
                     LIMIT 1
                   )
                ORDER BY cs.created_at
                """
            )
            return [self._source(row) for row in cursor.fetchall()]

    def update_source(self, source_id: str, **changes: Any) -> SourceRecord:
        allowed = {"enabled", "config", "next_run_at"}
        unknown = changes.keys() - allowed
        if unknown:
            raise RepositoryError(f"Unsupported source changes: {', '.join(sorted(unknown))}")
        if not changes:
            return self.get_source(source_id)
        assignments: list[str] = []
        values: list[Any] = []
        for key, value in changes.items():
            assignments.append(f"{key} = %s")
            values.append(Json(value) if key == "config" else value)
        values.append(source_id)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE collection_sources SET {', '.join(assignments)}, updated_at = now() WHERE id = %s RETURNING id::text",
                values,
            )
            row = cursor.fetchone()
            if not row:
                raise NotFoundError(f"Source '{source_id}' was not found")
            selected = self._select_source(cursor, str(row["id"]))
            assert selected is not None
            return self._source(selected)

    def delete_source(self, source_id: str) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM collection_sources WHERE id = %s", (source_id,))
            if cursor.rowcount != 1:
                raise NotFoundError(f"Source '{source_id}' was not found")

    def _active_runtime_config(self, cursor: Any) -> str:
        cursor.execute("SELECT id::text FROM youtube_runtime_configs WHERE status = 'active' ORDER BY activated_at DESC LIMIT 1")
        row = cursor.fetchone()
        if not row:
            raise RepositoryError("No server-managed YouTube runtime configuration is active")
        return str(row["id"])

    def create_job(
        self,
        *,
        source_id: str,
        include_comments: bool,
        max_videos: int | None,
        max_comments_per_video: int | None,
        runtime_config_id: str | None = None,
    ) -> JobRecord:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT target_id::text FROM collection_sources WHERE id = %s", (source_id,))
            source = cursor.fetchone()
            if not source:
                raise NotFoundError(f"Source '{source_id}' was not found")
            if source.get("target_id"):
                cursor.execute("SELECT id FROM collection_targets WHERE id = %s FOR UPDATE", (source["target_id"],))
                cursor.execute(
                    """
                    SELECT * FROM sync_jobs
                    WHERE target_id = %s AND state IN ('queued', 'running', 'waiting_retry', 'waiting_quota')
                    ORDER BY created_at
                    LIMIT 1
                    FOR UPDATE
                    """,
                    (source["target_id"],),
                )
                active = cursor.fetchone()
                if active:
                    return self._job(active)
            config_id = runtime_config_id or self._active_runtime_config(cursor)
            cursor.execute(
                """
                INSERT INTO sync_jobs (
                    source_id, target_id, runtime_config_id, state, current_stage, idempotency_key,
                    include_comments, max_videos, max_comments_per_video
                )
                VALUES (%s, %s, %s, 'queued', 'queued', %s, %s, %s, %s)
                RETURNING *
                """,
                (source_id, source.get("target_id"), config_id, new_id(), include_comments, max_videos, max_comments_per_video),
            )
            return self._job(cursor.fetchone())

    @staticmethod
    def _desired_coverage(source_type: SourceType, config: dict[str, Any]) -> dict[str, Any]:
        desired: dict[str, Any] = {
            "complete": False,
            "includeComments": bool(config.get("includeComments", False)),
            "maxCommentPagesPerVideo": int(config.get("maxCommentPagesPerVideo") or 1),
        }
        if source_type is SourceType.CHANNEL:
            desired["maxVideos"] = int(config.get("maxVideos") or 50)
        elif source_type is SourceType.KEYWORD:
            desired["maxPagesPerRun"] = int(config.get("maxPagesPerRun") or 1)
        return desired

    @staticmethod
    def _merge_config(source_type: SourceType, current: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
        merged = dict(current)
        for key, value in incoming.items():
            merged.setdefault(key, value)
        merged["includeComments"] = bool(current.get("includeComments", False) or incoming.get("includeComments", False))
        merged["maxCommentPagesPerVideo"] = max(
            int(current.get("maxCommentPagesPerVideo") or 1), int(incoming.get("maxCommentPagesPerVideo") or 1)
        )
        if source_type is SourceType.CHANNEL:
            merged["maxVideos"] = max(int(current.get("maxVideos") or 1), int(incoming.get("maxVideos") or 1))
        elif source_type is SourceType.KEYWORD:
            merged["maxPagesPerRun"] = max(int(current.get("maxPagesPerRun") or 1), int(incoming.get("maxPagesPerRun") or 1))
        return merged

    @staticmethod
    def _coverage_satisfies(coverage: dict[str, Any], desired: dict[str, Any]) -> bool:
        if not coverage.get("complete"):
            return False
        if desired.get("includeComments") and not coverage.get("includeComments"):
            return False
        for key in ("maxVideos", "maxPagesPerRun"):
            if key in desired and int(coverage.get(key) or 0) < int(desired[key]):
                return False
        return not desired.get("includeComments") or int(coverage.get("maxCommentPagesPerVideo") or 0) >= int(
            desired.get("maxCommentPagesPerVideo") or 1
        )

    @staticmethod
    def _job_coverage(job: JobRecord, source_type: SourceType, source_config: dict[str, Any]) -> dict[str, Any]:
        coverage = {
            "complete": False,
            "includeComments": bool(job.include_comments),
            "maxCommentPagesPerVideo": int(job.max_comments_per_video or source_config.get("maxCommentPagesPerVideo") or 1),
        }
        if source_type is SourceType.CHANNEL:
            coverage["maxVideos"] = int(job.max_videos or source_config.get("maxVideos") or 50)
        elif source_type is SourceType.KEYWORD:
            # The legacy job schema has no keyword-page breadth field.  A running
            # keyword job is treated conservatively unless it was queued and can be
            # safely widened before claim.
            coverage["maxPagesPerRun"] = int(source_config.get("maxPagesPerRun") or 1)
        return coverage

    def _target_source(self, cursor: Any, target_id: str) -> SourceRecord | None:
        cursor.execute(
            """
            SELECT cs.id::text
            FROM collection_requests cr
            JOIN collection_sources cs ON cs.id = cr.source_id
            WHERE cr.target_id = %s AND cr.source_id IS NOT NULL
            ORDER BY (COALESCE(cs.config ->> 'includeComments', 'false') = 'true') DESC,
                     COALESCE((cs.config ->> 'maxVideos')::integer, 0) DESC,
                     COALESCE((cs.config ->> 'maxPagesPerRun')::integer, 0) DESC,
                     COALESCE((cs.config ->> 'maxCommentPagesPerVideo')::integer, 0) DESC,
                     cr.created_at, cr.id
            LIMIT 1
            FOR UPDATE
            """,
            (target_id,),
        )
        row = cursor.fetchone()
        if not row:
            cursor.execute(
                """
                SELECT id::text FROM collection_sources WHERE target_id = %s
                ORDER BY (COALESCE(config ->> 'includeComments', 'false') = 'true') DESC,
                         COALESCE((config ->> 'maxVideos')::integer, 0) DESC,
                         COALESCE((config ->> 'maxPagesPerRun')::integer, 0) DESC,
                         COALESCE((config ->> 'maxCommentPagesPerVideo')::integer, 0) DESC,
                         created_at, id
                LIMIT 1 FOR UPDATE
                """,
                (target_id,),
            )
            row = cursor.fetchone()
        if not row:
            return None
        selected = self._select_source(cursor, str(row["id"]))
        return self._source(selected) if selected else None

    def _create_target_job(
        self,
        cursor: Any,
        *,
        target_id: str,
        source: SourceRecord,
        runtime_config_id: str | None,
    ) -> JobRecord:
        desired = self._desired_coverage(source.type, source.config)
        config_id = runtime_config_id or self._active_runtime_config(cursor)
        cursor.execute(
            """
            INSERT INTO sync_jobs (
              source_id, target_id, runtime_config_id, state, current_stage, idempotency_key,
              include_comments, max_videos, max_comments_per_video
            ) VALUES (%s, %s, %s, 'queued', 'queued', %s, %s, %s, %s)
            RETURNING *
            """,
            (
                source.id,
                target_id,
                config_id,
                new_id(),
                bool(desired["includeComments"]),
                desired.get("maxVideos"),
                desired.get("maxCommentPagesPerVideo"),
            ),
        )
        return self._job(cursor.fetchone())

    def _submission(self, cursor: Any, request: CollectionRequestRecord) -> CollectionSubmission:
        cursor.execute("SELECT * FROM collection_targets WHERE id = %s", (request.target_id,))
        target_row = cursor.fetchone()
        if not target_row:
            raise NotFoundError(f"Target '{request.target_id}' was not found")
        target = self._target(target_row)
        source_id = request.source_id
        if source_id is None:
            source = self._target_source(cursor, target.id)
        else:
            source_row = self._select_source(cursor, source_id)
            source = self._source(source_row) if source_row else None
        if not source:
            raise RepositoryError(f"Target '{target.id}' has no worker source")
        job: JobRecord | None = None
        if request.job_id:
            cursor.execute("SELECT * FROM sync_jobs WHERE id = %s", (request.job_id,))
            job_row = cursor.fetchone()
            job = self._job(job_row) if job_row else None
        if request.job_id is None and request.status == "queued":
            disposition = "successor_queued"
        elif request.status == "completed":
            disposition = "cached"
        elif request.status == "joined":
            disposition = "joined"
        else:
            disposition = "queued"
        return CollectionSubmission(request=request, target=target, source=source, job=job, disposition=disposition)

    def submit_collection_request(
        self,
        *,
        source_type: SourceType,
        config: dict[str, Any],
        canonical_key: str,
        aliases: list[tuple[str, str]],
        force_refresh: bool,
        idempotency_key: str | None,
        runtime_config_id: str | None = None,
    ) -> CollectionSubmission:
        with self._connection() as connection, connection.cursor() as cursor:
            if idempotency_key:
                cursor.execute("SELECT * FROM collection_requests WHERE idempotency_key = %s ORDER BY created_at LIMIT 1 FOR UPDATE", (idempotency_key,))
                replay = cursor.fetchone()
                if replay:
                    return self._submission(cursor, self._request(replay))

            target: CollectionTargetRecord | None = None
            for alias_kind, alias_value in aliases:
                cursor.execute(
                    """
                    SELECT ct.*
                    FROM collection_target_aliases cta
                    JOIN collection_targets ct ON ct.id = cta.target_id
                    WHERE cta.target_type = %s AND cta.alias_kind = %s AND cta.alias_value = %s
                    FOR UPDATE OF ct
                    """,
                    (source_type.value, alias_kind, alias_value),
                )
                row = cursor.fetchone()
                if row:
                    target = self._target(row)
                    break

            # A cached channel handle can immediately resolve to its immutable ID,
            # even if the request arrived as a handle rather than a UC identifier.
            if target is None and source_type is SourceType.CHANNEL:
                handle = next((value for kind, value in aliases if kind == "handle"), None)
                if handle:
                    cursor.execute(
                        "SELECT youtube_channel_id FROM channels WHERE lower(handle) = lower(%s) ORDER BY source_fetched_at DESC NULLS LAST LIMIT 1",
                        (handle,),
                    )
                    channel = cursor.fetchone()
                    if channel:
                        canonical_key = f"channel:{channel['youtube_channel_id']}"

            if target is None:
                cursor.execute(
                    """
                    INSERT INTO collection_targets (type, canonical_key, config)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (type, canonical_key) DO UPDATE SET updated_at = now()
                    RETURNING *
                    """,
                    (source_type.value, canonical_key, Json(config)),
                )
                target = self._target(cursor.fetchone())

            # Target upsert/alias lookup locks the shared target row.  Recheck here
            # so two concurrent browser retries with the same key serialize instead
            # of racing the partial unique request index.
            if idempotency_key:
                cursor.execute(
                    "SELECT * FROM collection_requests WHERE target_id = %s AND idempotency_key = %s FOR UPDATE",
                    (target.id, idempotency_key),
                )
                replay = cursor.fetchone()
                if replay:
                    return self._submission(cursor, self._request(replay))

            for alias_kind, alias_value in aliases:
                cursor.execute(
                    """
                    INSERT INTO collection_target_aliases (target_id, target_type, alias_kind, alias_value)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (target_type, alias_kind, alias_value) DO NOTHING
                    """,
                    (target.id, source_type.value, alias_kind, alias_value),
                )

            source = self._target_source(cursor, target.id)
            source_is_new = source is None
            if source is None:
                cursor.execute(
                    "INSERT INTO collection_sources (type, config, target_id) VALUES (%s, %s, %s) RETURNING id::text",
                    (source_type.value, Json(config), target.id),
                )
                row = self._select_source(cursor, str(cursor.fetchone()["id"]))
                assert row is not None
                source = self._source(row)

            prior_config = dict(source.config)
            merged_config = self._merge_config(source_type, prior_config, target.config)
            merged_config = self._merge_config(source_type, merged_config, config)
            cursor.execute("UPDATE collection_sources SET config = %s, updated_at = now() WHERE id = %s", (Json(merged_config), source.id))
            cursor.execute("UPDATE collection_targets SET config = %s, updated_at = now() WHERE id = %s RETURNING *", (Json(merged_config), target.id))
            target = self._target(cursor.fetchone())
            source_row = self._select_source(cursor, source.id)
            assert source_row is not None
            source = self._source(source_row)

            cursor.execute(
                """
                SELECT * FROM sync_jobs
                WHERE target_id = %s AND state IN ('queued', 'running', 'waiting_retry', 'waiting_quota')
                ORDER BY created_at
                LIMIT 1
                FOR UPDATE
                """,
                (target.id,),
            )
            active_row = cursor.fetchone()
            active = self._job(active_row) if active_row else None
            desired = self._desired_coverage(source_type, config)
            request_status = "queued"
            request_job_id: str | None = None

            if not force_refresh and self._coverage_satisfies(target.coverage, desired):
                request_status = "completed"
            elif active and self._coverage_satisfies(self._job_coverage(active, source_type, prior_config), desired):
                request_status = "joined"
                request_job_id = active.id
            elif active and active.state is JobState.QUEUED:
                active_desired = self._desired_coverage(source_type, merged_config)
                cursor.execute(
                    """
                    UPDATE sync_jobs
                    SET include_comments = %s, max_videos = %s, max_comments_per_video = %s, updated_at = now()
                    WHERE id = %s
                    """,
                    (
                        bool(active_desired["includeComments"]),
                        active_desired.get("maxVideos"),
                        active_desired.get("maxCommentPagesPerVideo"),
                        active.id,
                    ),
                )
                request_job_id = active.id
            elif not active:
                active = self._create_target_job(cursor, target_id=target.id, source=source, runtime_config_id=runtime_config_id)
                request_job_id = active.id

            cursor.execute(
                """
                INSERT INTO collection_requests (target_id, source_id, request_config, idempotency_key, job_id, status)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    target.id,
                    source.id if source_is_new else None,
                    Json(config),
                    idempotency_key,
                    request_job_id,
                    request_status,
                ),
            )
            return self._submission(cursor, self._request(cursor.fetchone()))

    def promote_channel_target(
        self, *, source_id: str, youtube_channel_id: str, handle: str | None = None
    ) -> CollectionTargetRecord | None:
        """Promote a provisional handle target after the worker resolves its UC ID."""

        with self._connection() as connection, connection.cursor() as cursor:
            source_row = self._select_source(cursor, source_id)
            if not source_row or not source_row.get("target_id"):
                return None
            current = self._source(source_row)
            if current.type is not SourceType.CHANNEL:
                return None
            cursor.execute("SELECT * FROM collection_targets WHERE id = %s FOR UPDATE", (current.target_id,))
            current_target_row = cursor.fetchone()
            if not current_target_row:
                return None
            current_target = self._target(current_target_row)
            canonical_key = f"channel:{youtube_channel_id}"
            cursor.execute(
                "SELECT * FROM collection_targets WHERE type = 'channel' AND canonical_key = %s FOR UPDATE",
                (canonical_key,),
            )
            existing_row = cursor.fetchone()
            if existing_row and str(existing_row["id"]) != current_target.id:
                target = self._target(existing_row)
                # The partial active-target index permits only one live job after a
                # merge.  Retain the already-canonical target's live work and leave
                # redundant provisional jobs auditable but detached from the target.
                cursor.execute(
                    """
                    SELECT id::text, target_id::text FROM sync_jobs
                    WHERE target_id IN (%s, %s)
                      AND state IN ('queued', 'running', 'waiting_retry', 'waiting_quota')
                    ORDER BY created_at
                    FOR UPDATE
                    """,
                    (current_target.id, target.id),
                )
                active_jobs = cursor.fetchall()
                if any(str(job["target_id"]) == target.id for job in active_jobs):
                    cursor.execute(
                        """
                        UPDATE sync_jobs SET target_id = NULL, updated_at = now()
                        WHERE target_id = %s
                          AND state IN ('queued', 'running', 'waiting_retry', 'waiting_quota')
                        """,
                        (current_target.id,),
                    )
                # Preserve every reference before retiring the provisional target.
                cursor.execute("UPDATE collection_sources SET target_id = %s WHERE target_id = %s", (target.id, current_target.id))
                cursor.execute("UPDATE collection_requests SET target_id = %s WHERE target_id = %s", (target.id, current_target.id))
                cursor.execute("UPDATE sync_jobs SET target_id = %s WHERE target_id = %s", (target.id, current_target.id))
                cursor.execute(
                    """
                    INSERT INTO collection_target_videos (target_id, video_id, first_seen_at, last_seen_at)
                    SELECT %s, video_id, first_seen_at, last_seen_at
                    FROM collection_target_videos WHERE target_id = %s
                    ON CONFLICT (target_id, video_id) DO UPDATE
                    SET first_seen_at = LEAST(collection_target_videos.first_seen_at, EXCLUDED.first_seen_at),
                        last_seen_at = GREATEST(collection_target_videos.last_seen_at, EXCLUDED.last_seen_at)
                    """,
                    (target.id, current_target.id),
                )
                cursor.execute(
                    """
                    INSERT INTO collection_target_aliases (target_id, target_type, alias_kind, alias_value)
                    SELECT %s, target_type, alias_kind, alias_value
                    FROM collection_target_aliases WHERE target_id = %s
                    ON CONFLICT (target_type, alias_kind, alias_value) DO NOTHING
                    """,
                    (target.id, current_target.id),
                )
                cursor.execute("DELETE FROM collection_target_aliases WHERE target_id = %s", (current_target.id,))
                cursor.execute("DELETE FROM collection_targets WHERE id = %s", (current_target.id,))
            else:
                cursor.execute(
                    """
                    UPDATE collection_targets ct
                    SET canonical_key = %s,
                        resolved_channel_id = channels.id,
                        updated_at = now()
                    FROM channels
                    WHERE ct.id = %s AND channels.youtube_channel_id = %s
                    RETURNING ct.*
                    """,
                    (canonical_key, current_target.id, youtube_channel_id),
                )
                promoted = cursor.fetchone()
                if not promoted:
                    cursor.execute(
                        "UPDATE collection_targets SET canonical_key = %s, updated_at = now() WHERE id = %s RETURNING *",
                        (canonical_key, current_target.id),
                    )
                    promoted = cursor.fetchone()
                target = self._target(promoted)
            aliases = [("channel_id", youtube_channel_id)]
            if handle:
                aliases.append(("handle", handle.casefold()))
            for alias_kind, alias_value in aliases:
                cursor.execute(
                    """
                    INSERT INTO collection_target_aliases (target_id, target_type, alias_kind, alias_value)
                    VALUES (%s, 'channel', %s, %s)
                    ON CONFLICT (target_type, alias_kind, alias_value) DO UPDATE SET target_id = EXCLUDED.target_id
                    """,
                    (target.id, alias_kind, alias_value),
                )
            return target

    def get_job(self, job_id: str) -> JobRecord:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM sync_jobs WHERE id = %s", (job_id,))
            row = cursor.fetchone()
            if not row:
                raise NotFoundError(f"Job '{job_id}' was not found")
            return self._job(row)

    def transition_job(self, job_id: str, state: JobState, **changes: Any) -> JobRecord:
        allowed = {
            "current_stage",
            "progress_completed",
            "progress_total",
            "progress_unit",
            "pause_reason",
            "quota_bucket",
            "resume_at",
            "resume_is_automatic",
            "checkpoint",
            "partial_errors",
            "lease_owner",
            "lease_expires_at",
        }
        unknown = changes.keys() - allowed
        if unknown:
            raise RepositoryError(f"Unsupported job changes: {', '.join(sorted(unknown))}")
        with self._connection() as connection, connection.cursor() as cursor:
            # Use the same target -> job lock order as request submission.  This
            # prevents a just-finished job from missing a concurrently inserted
            # successor request between its terminal transition and pending scan.
            cursor.execute("SELECT target_id FROM sync_jobs WHERE id = %s", (job_id,))
            target_hint = cursor.fetchone()
            if not target_hint:
                raise NotFoundError(f"Job '{job_id}' was not found")
            if target_hint.get("target_id"):
                cursor.execute("SELECT id FROM collection_targets WHERE id = %s FOR UPDATE", (target_hint["target_id"],))
            cursor.execute("SELECT * FROM sync_jobs WHERE id = %s FOR UPDATE", (job_id,))
            row = cursor.fetchone()
            if not row:
                raise NotFoundError(f"Job '{job_id}' was not found")
            current = self._job(row)
            if state != current.state and state not in _ALLOWED_TRANSITIONS[current.state]:
                raise InvalidStateTransitionError(f"Cannot transition job '{job_id}' from {current.state.value} to {state.value}")
            assignments = ["state = %s"]
            values: list[Any] = [state.value]
            for key, value in changes.items():
                assignments.append(f"{key} = %s")
                values.append(Json(value) if key in {"checkpoint", "partial_errors"} else value)
            values.append(job_id)
            cursor.execute(f"UPDATE sync_jobs SET {', '.join(assignments)}, updated_at = now() WHERE id = %s RETURNING *", values)
            updated = self._job(cursor.fetchone())
            if updated.state.is_terminal:
                cursor.execute(
                    "UPDATE collection_requests SET status = %s, updated_at = now() WHERE job_id = %s",
                    (updated.state.value, updated.id),
                )
                if updated.target_id:
                    if updated.state is JobState.COMPLETED:
                        source_row = self._select_source(cursor, updated.source_id)
                        if source_row:
                            source = self._source(source_row)
                            coverage = self._job_coverage(updated, source.type, source.config)
                            coverage["complete"] = True
                            cursor.execute(
                                "UPDATE collection_targets SET coverage = %s, last_completed_at = now(), updated_at = now() WHERE id = %s",
                                (Json(coverage), updated.target_id),
                            )
                    cursor.execute(
                        """
                        SELECT id::text FROM collection_requests
                        WHERE target_id = %s AND job_id IS NULL AND status = 'queued'
                        ORDER BY created_at
                        FOR UPDATE
                        """,
                        (updated.target_id,),
                    )
                    pending = [str(row["id"]) for row in cursor.fetchall()]
                    if pending:
                        source = self._target_source(cursor, updated.target_id)
                        if source:
                            successor = self._create_target_job(
                                cursor,
                                target_id=updated.target_id,
                                source=source,
                                runtime_config_id=updated.runtime_config_id,
                            )
                            cursor.execute(
                                "UPDATE collection_requests SET job_id = %s, status = 'queued', updated_at = now() WHERE id = ANY(%s)",
                                (successor.id, pending),
                            )
            return updated

    def claim_next_job(self, *, worker_id: str, lease_seconds: int = 120) -> JobRecord | None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                WITH candidate AS (
                  SELECT id
                  FROM sync_jobs
                  WHERE (
                    state = 'queued'
                    OR (state IN ('waiting_retry', 'waiting_quota') AND resume_at IS NOT NULL AND resume_at <= now())
                    OR (state = 'running' AND lease_expires_at IS NOT NULL AND lease_expires_at <= now())
                  )
                  AND (lease_expires_at IS NULL OR lease_expires_at <= now())
                  ORDER BY created_at
                  FOR UPDATE SKIP LOCKED
                  LIMIT 1
                )
                UPDATE sync_jobs AS job
                SET state = 'running', current_stage = CASE WHEN job.state = 'running' THEN 'reclaimed' ELSE 'claimed' END, pause_reason = NULL,
                    quota_bucket = NULL, resume_at = NULL, resume_is_automatic = FALSE, lease_owner = %s,
                    lease_expires_at = now() + (%s * interval '1 second'), updated_at = now()
                FROM candidate
                WHERE job.id = candidate.id
                RETURNING job.*
                """,
                (worker_id, lease_seconds),
            )
            row = cursor.fetchone()
            return self._job(row) if row else None

    def renew_job_lease(self, *, job_id: str, worker_id: str, lease_seconds: int = 120) -> bool:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE sync_jobs
                SET lease_expires_at = now() + (%s * interval '1 second'), updated_at = now()
                WHERE id = %s AND state = 'running' AND lease_owner = %s
                """,
                (lease_seconds, job_id, worker_id),
            )
            return cursor.rowcount == 1

    def checkpoint_job(self, job_id: str, checkpoint: dict[str, Any]) -> JobRecord:
        current = self.get_job(job_id)
        updated = self.transition_job(job_id, current.state, checkpoint=checkpoint)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO sync_checkpoints (job_id, stage, scope_key, request_hash, page_token, batch_cursor, checkpoint_seq)
                VALUES (%s, %s, %s, %s, %s, %s, 1)
                ON CONFLICT (job_id, stage, scope_key) DO UPDATE
                SET page_token = EXCLUDED.page_token,
                    batch_cursor = EXCLUDED.batch_cursor,
                    checkpoint_seq = sync_checkpoints.checkpoint_seq + 1,
                    updated_at = now()
                """,
                (
                    job_id,
                    str(checkpoint.get("stage", "collecting")),
                    str(checkpoint.get("scopeKey", "job")),
                    hashlib.sha256(str(sorted(checkpoint.items())).encode("utf-8")).hexdigest(),
                    checkpoint.get("pageToken"),
                    int(checkpoint.get("batchCursor", 0)),
                ),
            )
        return updated

    def update_job_progress(
        self, job_id: str, *, completed: int, total: int | None, unit: str, current_stage: str | None = None
    ) -> JobRecord:
        current = self.get_job(job_id)
        changes: dict[str, Any] = {"progress_completed": completed, "progress_total": total, "progress_unit": unit}
        if current_stage:
            changes["current_stage"] = current_stage
        return self.transition_job(job_id, current.state, **changes)

    def upsert_channel(self, channel: dict[str, Any]) -> dict[str, Any]:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO channels (youtube_channel_id, handle, title, description, uploads_playlist_id, source_fetched_at)
                VALUES (%(youtube_channel_id)s, %(handle)s, %(title)s, %(description)s, %(uploads_playlist_id)s, %(source_fetched_at)s)
                ON CONFLICT (youtube_channel_id) DO UPDATE SET
                  handle = COALESCE(EXCLUDED.handle, channels.handle),
                  title = COALESCE(EXCLUDED.title, channels.title),
                  description = COALESCE(EXCLUDED.description, channels.description),
                  uploads_playlist_id = COALESCE(EXCLUDED.uploads_playlist_id, channels.uploads_playlist_id),
                  source_fetched_at = EXCLUDED.source_fetched_at
                RETURNING id::text, youtube_channel_id
                """,
                channel,
            )
            return dict(cursor.fetchone())

    def upsert_video(self, video: VideoRecord) -> VideoRecord:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM channels WHERE youtube_channel_id = %s",
                (video.youtube_channel_id,),
            )
            channel_row = cursor.fetchone() if video.youtube_channel_id else None
            channel_id = channel_row["id"] if channel_row else None
            cursor.execute(
                """
                INSERT INTO videos (
                  youtube_video_id, channel_id, title, description, published_at, duration_seconds,
                  privacy_status, made_for_kids, source_fetched_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (youtube_video_id) DO UPDATE SET
                  channel_id = EXCLUDED.channel_id, title = EXCLUDED.title, description = EXCLUDED.description,
                  published_at = EXCLUDED.published_at, duration_seconds = EXCLUDED.duration_seconds,
                  privacy_status = EXCLUDED.privacy_status, made_for_kids = EXCLUDED.made_for_kids,
                  source_fetched_at = EXCLUDED.source_fetched_at
                RETURNING id::text
                """,
                (
                    video.youtube_video_id,
                    channel_id,
                    video.title,
                    video.description,
                    video.published_at,
                    video.duration_seconds,
                    video.privacy_status,
                    video.made_for_kids,
                    video.source_fetched_at,
                ),
            )
            internal_id = str(cursor.fetchone()["id"])
            cursor.execute(
                """
                INSERT INTO video_stat_snapshots (video_id, fetched_at, view_count, like_count, comment_count)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (video_id, fetched_at) DO UPDATE SET
                  view_count = EXCLUDED.view_count, like_count = EXCLUDED.like_count, comment_count = EXCLUDED.comment_count
                """,
                (
                    internal_id,
                    video.source_fetched_at,
                    video.statistics.get("viewCount", 0),
                    video.statistics.get("likeCount", 0),
                    video.statistics.get("commentCount", 0),
                ),
            )
            return replace(video, id=internal_id)

    def link_source_video(self, source_id: str, youtube_video_id: str) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO source_videos (source_id, video_id)
                SELECT %s, id FROM videos WHERE youtube_video_id = %s
                ON CONFLICT (source_id, video_id) DO UPDATE SET last_seen_at = now()
                """,
                (source_id, youtube_video_id),
            )
            if cursor.rowcount != 1:
                raise NotFoundError(f"Video '{youtube_video_id}' was not found")
            cursor.execute(
                """
                INSERT INTO collection_target_videos (target_id, video_id)
                SELECT collection_sources.target_id, video.id FROM collection_sources
                CROSS JOIN (SELECT id FROM videos WHERE youtube_video_id = %s) video
                WHERE collection_sources.id = %s AND collection_sources.target_id IS NOT NULL
                ON CONFLICT (target_id, video_id) DO UPDATE SET last_seen_at = now()
                """,
                (youtube_video_id, source_id),
            )

    def upsert_comment(self, comment: CommentRecord) -> CommentRecord:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id FROM videos WHERE youtube_video_id = %s", (comment.youtube_video_id,))
            video = cursor.fetchone()
            if not video:
                raise NotFoundError(f"Video '{comment.youtube_video_id}' was not found")
            parent_id = None
            if comment.youtube_parent_comment_id:
                cursor.execute("SELECT id FROM comments WHERE youtube_comment_id = %s", (comment.youtube_parent_comment_id,))
                parent = cursor.fetchone()
                parent_id = parent["id"] if parent else None
            cursor.execute(
                """
                INSERT INTO comments (
                  youtube_comment_id, video_id, parent_id, youtube_parent_comment_id, youtube_thread_id,
                  text_display, text_original, like_count, published_at, updated_at, source_fetched_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (youtube_comment_id) DO UPDATE SET
                  parent_id = EXCLUDED.parent_id, youtube_parent_comment_id = EXCLUDED.youtube_parent_comment_id,
                  youtube_thread_id = EXCLUDED.youtube_thread_id, text_display = EXCLUDED.text_display,
                  text_original = EXCLUDED.text_original, like_count = EXCLUDED.like_count,
                  published_at = EXCLUDED.published_at, updated_at = EXCLUDED.updated_at,
                  source_fetched_at = EXCLUDED.source_fetched_at
                RETURNING id::text
                """,
                (
                    comment.youtube_comment_id,
                    video["id"],
                    parent_id,
                    comment.youtube_parent_comment_id,
                    comment.youtube_thread_id,
                    comment.text_display,
                    comment.text_display,
                    comment.like_count,
                    comment.published_at,
                    comment.updated_at,
                    comment.source_fetched_at,
                ),
            )
            return replace(comment, id=str(cursor.fetchone()["id"]))

    def record_api_request(
        self, *, job_id: str, bucket: QuotaBucket, endpoint: str, status_code: int, error_reason: str | None = None
    ) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO api_request_logs (job_id, runtime_config_id, bucket, endpoint, parameter_hash, expected_cost, actual_cost, http_status, error_reason)
                SELECT id, runtime_config_id, %s, %s, %s, 1, 1, %s, %s FROM sync_jobs WHERE id = %s
                """,
                (bucket.value, endpoint, "server-managed", status_code, error_reason, job_id),
            )

    def _source_videos(self, cursor: Any, source_id: str) -> list[VideoRecord]:
        cursor.execute(
            """
            SELECT v.id::text, v.youtube_video_id, c.youtube_channel_id, v.title, v.description, v.published_at,
                   v.duration_seconds, v.privacy_status, v.made_for_kids, v.source_fetched_at,
                   jsonb_build_object(
                     'viewCount', COALESCE(stats.view_count, 0), 'likeCount', COALESCE(stats.like_count, 0),
                     'commentCount', COALESCE(stats.comment_count, 0)
                   ) AS statistics
            FROM source_videos sv
            JOIN videos v ON v.id = sv.video_id
            LEFT JOIN channels c ON c.id = v.channel_id
            LEFT JOIN LATERAL (
              SELECT view_count, like_count, comment_count FROM video_stat_snapshots
              WHERE video_id = v.id ORDER BY fetched_at DESC LIMIT 1
            ) stats ON TRUE
            WHERE sv.source_id = %s
            ORDER BY v.published_at DESC NULLS LAST, v.youtube_video_id
            """,
            (source_id,),
        )
        return [self._video(row) for row in cursor.fetchall()]

    def _target_videos(self, cursor: Any, target_id: str) -> list[VideoRecord]:
        cursor.execute(
            """
            SELECT v.id::text, v.youtube_video_id, c.youtube_channel_id, v.title, v.description, v.published_at,
                   v.duration_seconds, v.privacy_status, v.made_for_kids, v.source_fetched_at,
                   jsonb_build_object(
                     'viewCount', COALESCE(stats.view_count, 0), 'likeCount', COALESCE(stats.like_count, 0),
                     'commentCount', COALESCE(stats.comment_count, 0)
                   ) AS statistics
            FROM collection_target_videos tv
            JOIN videos v ON v.id = tv.video_id
            LEFT JOIN channels c ON c.id = v.channel_id
            LEFT JOIN LATERAL (
              SELECT view_count, like_count, comment_count FROM video_stat_snapshots
              WHERE video_id = v.id ORDER BY fetched_at DESC LIMIT 1
            ) stats ON TRUE
            WHERE tv.target_id = %s
            ORDER BY v.published_at DESC NULLS LAST, v.youtube_video_id
            """,
            (target_id,),
        )
        return [self._video(row) for row in cursor.fetchall()]

    def _comments(self, cursor: Any, video_ids: list[str]) -> list[CommentRecord]:
        if not video_ids:
            return []
        cursor.execute(
            """
            SELECT cm.id::text, cm.youtube_comment_id, v.youtube_video_id, cm.youtube_parent_comment_id,
                   cm.youtube_thread_id, cm.text_display, cm.like_count, cm.published_at, cm.updated_at, cm.source_fetched_at
            FROM comments cm JOIN videos v ON v.id = cm.video_id
            WHERE v.youtube_video_id = ANY(%s)
            ORDER BY cm.published_at DESC NULLS LAST, cm.youtube_comment_id
            """,
            (video_ids,),
        )
        return [self._comment(row) for row in cursor.fetchall()]

    def save_analysis_summary(self, source_id: str) -> dict[str, Any]:
        result = self.get_source_results(source_id)
        summary = result["analysis"]
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO analysis_runs (source_id, state, pipeline_version, policy_gate_version, started_at, completed_at)
                VALUES (%s, 'completed', 'deterministic-v1', 'server-managed', now(), now())
                RETURNING id
                """,
                (source_id,),
            )
            run_id = cursor.fetchone()["id"]
            cursor.execute(
                "INSERT INTO analysis_results (analysis_run_id, result_kind, payload) VALUES (%s, 'basic_summary', %s)",
                (run_id, Json(self._json_safe(summary))),
            )
        return summary

    def get_source_results(self, source_id: str) -> dict[str, Any]:
        with self._connection() as connection, connection.cursor() as cursor:
            source_row = self._select_source(cursor, source_id)
            if not source_row:
                raise NotFoundError(f"Source '{source_id}' was not found")
            source = self._source(source_row)
            if source.target_id:
                cursor.execute("SELECT * FROM sync_jobs WHERE target_id = %s ORDER BY created_at DESC LIMIT 1", (source.target_id,))
            else:
                cursor.execute("SELECT * FROM sync_jobs WHERE source_id = %s ORDER BY created_at DESC LIMIT 1", (source_id,))
            latest_row = cursor.fetchone()
            videos = self._target_videos(cursor, source.target_id) if source.target_id else self._source_videos(cursor, source_id)
            comments = self._comments(cursor, [video.youtube_video_id for video in videos])
            summary = build_summary(videos, comments)
            return {"source": source, "latest_job": self._job(latest_row) if latest_row else None, "videos": videos, "comments": comments, "analysis": summary}

    def get_video_comments(self, video_id: str) -> dict[str, Any]:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT v.id::text, v.youtube_video_id, c.youtube_channel_id, v.title, v.description, v.published_at,
                       v.duration_seconds, v.privacy_status, v.made_for_kids, v.source_fetched_at,
                       jsonb_build_object('viewCount', COALESCE(stats.view_count, 0), 'likeCount', COALESCE(stats.like_count, 0), 'commentCount', COALESCE(stats.comment_count, 0)) AS statistics
                FROM videos v
                LEFT JOIN channels c ON c.id = v.channel_id
                LEFT JOIN LATERAL (SELECT view_count, like_count, comment_count FROM video_stat_snapshots WHERE video_id = v.id ORDER BY fetched_at DESC LIMIT 1) stats ON TRUE
                WHERE v.youtube_video_id = %s OR v.id::text = %s
                """,
                (video_id, video_id),
            )
            row = cursor.fetchone()
            if not row:
                raise NotFoundError(f"Video '{video_id}' was not found")
            video = self._video(row)
            comments = self._comments(cursor, [video.youtube_video_id])
            return {"video": video, "comments": comments, "summary": build_summary([video], comments)}

    def set_target_pin(self, *, target_id: str, enabled: bool, interval_minutes: int) -> dict[str, Any]:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id FROM collection_targets WHERE id = %s FOR UPDATE", (target_id,))
            if not cursor.fetchone():
                raise NotFoundError(f"Collection target '{target_id}' was not found")
            cursor.execute(
                """
                INSERT INTO collection_target_pins (target_id, enabled, interval_minutes, next_run_at)
                VALUES (%s, %s, %s, CASE WHEN %s THEN now() ELSE now() END)
                ON CONFLICT (target_id) DO UPDATE
                SET enabled = EXCLUDED.enabled, interval_minutes = EXCLUDED.interval_minutes,
                    next_run_at = CASE WHEN EXCLUDED.enabled THEN now() ELSE collection_target_pins.next_run_at END,
                    updated_at = now()
                RETURNING target_id::text, enabled, interval_minutes, next_run_at, last_dispatched_at
                """,
                (target_id, enabled, interval_minutes, enabled),
            )
            return dict(cursor.fetchone())

    def get_target_pin(self, *, target_id: str) -> dict[str, Any] | None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT target_id::text, enabled, interval_minutes, next_run_at, last_dispatched_at FROM collection_target_pins WHERE target_id = %s",
                (target_id,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def dispatch_due_pins(self, *, runtime_config_id: str | None = None, limit: int = 10) -> int:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT target_id::text, interval_minutes
                FROM collection_target_pins
                WHERE enabled = TRUE AND next_run_at <= now()
                ORDER BY next_run_at FOR UPDATE SKIP LOCKED LIMIT %s
                """,
                (limit,),
            )
            due = cursor.fetchall()
            dispatched = 0
            for pin in due:
                target_id = str(pin["target_id"])
                cursor.execute(
                    "SELECT 1 FROM sync_jobs WHERE target_id = %s AND state IN ('queued', 'running', 'waiting_retry', 'waiting_quota') LIMIT 1",
                    (target_id,),
                )
                if not cursor.fetchone():
                    source = self._target_source(cursor, target_id)
                    if source:
                        self._create_target_job(cursor, target_id=target_id, source=source, runtime_config_id=runtime_config_id)
                        cursor.execute(
                            "UPDATE collection_target_pins SET last_dispatched_at = now(), next_run_at = now() + (interval_minutes * interval '1 minute'), updated_at = now() WHERE target_id = %s",
                            (target_id,),
                        )
                        dispatched += 1
                        continue
                cursor.execute(
                    "UPDATE collection_target_pins SET next_run_at = now() + (interval_minutes * interval '1 minute'), updated_at = now() WHERE target_id = %s",
                    (target_id,),
                )
            return dispatched

    def list_explore(self, *, limit: int = 60) -> dict[str, Any]:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT c.youtube_channel_id, c.handle, c.title, c.description,
                       COALESCE(video_counts.video_count, 0)::integer AS video_count,
                       COALESCE(comment_counts.comment_count, 0)::integer AS comment_count,
                       GREATEST(c.source_fetched_at, video_counts.last_fetched_at) AS last_fetched_at,
                       target.id::text AS target_id, pin.enabled AS pin_enabled, pin.interval_minutes AS pin_interval_minutes,
                       pin.next_run_at AS pin_next_run_at, pin.last_dispatched_at AS pin_last_dispatched_at
                FROM channels c
                LEFT JOIN LATERAL (SELECT count(*) AS video_count, max(source_fetched_at) AS last_fetched_at FROM videos WHERE channel_id = c.id) video_counts ON TRUE
                LEFT JOIN LATERAL (SELECT count(*) AS comment_count FROM comments cm JOIN videos v ON v.id = cm.video_id WHERE v.channel_id = c.id) comment_counts ON TRUE
                LEFT JOIN collection_targets target ON target.resolved_channel_id = c.id
                LEFT JOIN collection_target_pins pin ON pin.target_id = target.id
                ORDER BY GREATEST(c.source_fetched_at, video_counts.last_fetched_at) DESC NULLS LAST, c.title
                """
            )
            channels = []
            for row in cursor.fetchall():
                pin = None
                if row.get("pin_enabled") is not None:
                    pin = {"target_id": str(row["target_id"]), "enabled": bool(row["pin_enabled"]), "interval_minutes": int(row["pin_interval_minutes"]), "next_run_at": row["pin_next_run_at"], "last_dispatched_at": row["pin_last_dispatched_at"]}
                channels.append({"youtubeChannelId": row["youtube_channel_id"], "handle": row["handle"], "title": row["title"], "description": row["description"], "videoCount": int(row["video_count"]), "commentCount": int(row["comment_count"]), "lastFetchedAt": row["last_fetched_at"], "targetId": str(row["target_id"]) if row.get("target_id") else None, "pin": pin})
            cursor.execute(
                """
                SELECT v.id::text, v.youtube_video_id, c.youtube_channel_id, v.title, v.description, v.published_at,
                       v.duration_seconds, v.privacy_status, v.made_for_kids, v.source_fetched_at,
                       jsonb_build_object('viewCount', COALESCE(stats.view_count, 0), 'likeCount', COALESCE(stats.like_count, 0), 'commentCount', COALESCE(stats.comment_count, 0)) AS statistics
                FROM videos v LEFT JOIN channels c ON c.id = v.channel_id
                LEFT JOIN LATERAL (SELECT view_count, like_count, comment_count FROM video_stat_snapshots WHERE video_id = v.id ORDER BY fetched_at DESC LIMIT 1) stats ON TRUE
                ORDER BY v.source_fetched_at DESC NULLS LAST, v.published_at DESC NULLS LAST LIMIT %s
                """,
                (limit,),
            )
            return {"channels": channels, "videos": [self._video(row) for row in cursor.fetchall()]}
