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
from .domain import CommentRecord, JobRecord, JobState, QuotaBucket, SourceRecord, SourceType, VideoRecord, new_id, utcnow
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

    def create_source(self, *, source_type: SourceType, config: dict[str, Any]) -> SourceRecord:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO collection_sources (type, config)
                VALUES (%s, %s)
                RETURNING id::text, type::text, config, enabled, created_at, updated_at, next_run_at
                """,
                (source_type.value, Json(config)),
            )
            return self._source(cursor.fetchone())

    def get_source(self, source_id: str) -> SourceRecord:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT id::text, type::text, config, enabled, created_at, updated_at, next_run_at FROM collection_sources WHERE id = %s",
                (source_id,),
            )
            row = cursor.fetchone()
            if not row:
                raise NotFoundError(f"Source '{source_id}' was not found")
            return self._source(row)

    def list_sources(self) -> list[SourceRecord]:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id::text, type::text, config, enabled, created_at, updated_at, next_run_at FROM collection_sources ORDER BY created_at")
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
                f"UPDATE collection_sources SET {', '.join(assignments)}, updated_at = now() WHERE id = %s "
                "RETURNING id::text, type::text, config, enabled, created_at, updated_at, next_run_at",
                values,
            )
            row = cursor.fetchone()
            if not row:
                raise NotFoundError(f"Source '{source_id}' was not found")
            return self._source(row)

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
            cursor.execute("SELECT 1 FROM collection_sources WHERE id = %s", (source_id,))
            if not cursor.fetchone():
                raise NotFoundError(f"Source '{source_id}' was not found")
            config_id = runtime_config_id or self._active_runtime_config(cursor)
            cursor.execute(
                """
                INSERT INTO sync_jobs (
                    source_id, runtime_config_id, state, current_stage, idempotency_key,
                    include_comments, max_videos, max_comments_per_video
                )
                VALUES (%s, %s, 'queued', 'queued', %s, %s, %s, %s)
                RETURNING *
                """,
                (source_id, config_id, new_id(), include_comments, max_videos, max_comments_per_video),
            )
            return self._job(cursor.fetchone())

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
            return self._job(cursor.fetchone())

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
            cursor.execute("SELECT id::text, type::text, config, enabled, created_at, updated_at, next_run_at FROM collection_sources WHERE id = %s", (source_id,))
            source_row = cursor.fetchone()
            if not source_row:
                raise NotFoundError(f"Source '{source_id}' was not found")
            source = self._source(source_row)
            cursor.execute("SELECT * FROM sync_jobs WHERE source_id = %s ORDER BY created_at DESC LIMIT 1", (source_id,))
            latest_row = cursor.fetchone()
            videos = self._source_videos(cursor, source_id)
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
