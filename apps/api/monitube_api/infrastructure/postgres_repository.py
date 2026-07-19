"""psycopg-backed repository for server-managed collection jobs and public data."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
import hashlib
from typing import Any, Iterable, Iterator

try:  # Keep the in-memory API usable when optional runtime dependencies are absent.
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Json
    from psycopg_pool import ConnectionPool, PoolTimeout
except ImportError:  # pragma: no cover - exercised only in minimal local installs
    psycopg = None  # type: ignore[assignment]
    dict_row = None  # type: ignore[assignment]
    ConnectionPool = None  # type: ignore[assignment,misc]

    class PoolTimeout(Exception):
        pass

    class Json:  # type: ignore[no-redef]
        def __init__(self, value: Any) -> None:
            self.value = value

from ..analysis import build_summary, top_words_from_texts
from ..collection_policy import (
    coverage_satisfies,
    desired_coverage,
    job_coverage,
    merge_collection_config,
)
from ..fuzzy_search import normalize_search_text, rank_text_fields
from ..ports import CollectionRepository
from ..ports.results import CommentThreadSort
from ..domain import (
    CollectionRequestRecord,
    CollectionSubmission,
    CollectionSubscriptionRecord,
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
from .postgres_comments import PostgresCommentReadMixin
from .postgres_explore import PostgresExploreMixin
from .postgres_jobs import PostgresJobMixin
from .postgres_results import PostgresResultMixin
from .postgres_writes import PostgresCollectionWriteMixin
from ..repositories import (
    InvalidCursorError,
    InvalidStateTransitionError,
    NotFoundError,
    RepositoryError,
    RepositoryUnavailableError,
    _ALLOWED_TRANSITIONS,
    decode_comment_cursor,
    decode_comment_thread_cursor,
    decode_explore_video_cursor,
    decode_source_video_cursor,
    encode_comment_cursor,
    encode_comment_thread_cursor,
    encode_explore_video_cursor,
    encode_source_video_cursor,
    explore_video_filter_hash,
    source_video_filter_hash,
)


from .postgres_support import _optional_nonnegative_int, _strip_nul


class PostgresRepository(
    PostgresCommentReadMixin,
    PostgresExploreMixin,
    PostgresJobMixin,
    PostgresCollectionWriteMixin,
    PostgresResultMixin,
    CollectionRepository,
):
    """Synchronous PostgreSQL repository used by FastAPI and the polling worker.

    The class persists an opaque secret reference and a fingerprint in
    ``youtube_runtime_configs``. It intentionally has no parameter or column for a
    raw API key.
    """

    def __init__(
        self,
        database_url: str,
        *,
        connect: Any | None = None,
        pool_min_size: int = 1,
        pool_max_size: int = 8,
        pool_timeout_seconds: float = 3.0,
        enable_target_summary_write: bool = True,
        enable_target_summary_read: bool = True,
        enable_comment_batch_write: bool = True,
        enable_comment_rollup_dual_write: bool = True,
        enable_comment_rollup_read: bool = False,
        enable_explore_rollup: bool = False,
        enable_search_trigram: bool = True,
    ) -> None:
        if not database_url:
            raise ValueError("database_url is required")
        self.database_url = database_url
        self._connect_override = connect
        self._pool_timeout_seconds = pool_timeout_seconds
        self.enable_target_summary_write = enable_target_summary_write
        self.enable_target_summary_read = enable_target_summary_read
        self.enable_comment_batch_write = enable_comment_batch_write
        self.enable_comment_rollup_dual_write = enable_comment_rollup_dual_write
        self.enable_comment_rollup_read = enable_comment_rollup_read
        self.enable_explore_rollup = enable_explore_rollup
        self.enable_search_trigram = enable_search_trigram
        self._pool: Any | None = None
        if connect is None and ConnectionPool is not None:
            self._pool = ConnectionPool(
                conninfo=database_url,
                min_size=max(1, pool_min_size),
                max_size=max(pool_min_size, pool_max_size),
                timeout=pool_timeout_seconds,
                kwargs={"row_factory": dict_row},
                open=True,
                name="monitube-db",
            )

    @property
    def pool(self) -> Any | None:
        """Process-local pool shared with the auth store when PostgreSQL is used."""

        return self._pool

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()

    def check_readiness(self) -> dict[str, Any]:
        """Acquire a pool connection and verify the latest required migration."""

        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT 1 AS ok")
            cursor.fetchone()
            cursor.execute(
                """
                SELECT EXISTS (
                  SELECT 1 FROM monitube_schema_migrations
                  WHERE filename = '016_search_planner_statistics.sql'
                ) AS migration_current
                """
            )
            row = cursor.fetchone()
            checks: dict[str, Any] = {
                "database": "ok",
                "migrationCurrent": bool(row and row.get("migration_current")),
                "pool": "enabled" if self._pool is not None else "direct",
            }
            if self._pool is not None:
                stats = self._pool.get_stats()
                checks["poolStats"] = {
                    key: int(stats.get(key, 0))
                    for key in (
                        "pool_size",
                        "pool_available",
                        "requests_waiting",
                        "requests_errors",
                    )
                }
            return checks

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        pooled = False
        if self._connect_override is not None:
            connection = self._connect_override()
        elif self._pool is not None:
            try:
                connection = self._pool.getconn(timeout=self._pool_timeout_seconds)
                pooled = True
            except PoolTimeout as exc:
                raise RepositoryUnavailableError("Database connection pool is busy; retry shortly") from exc
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
            if pooled:
                self._pool.putconn(connection)
            else:
                connection.close()

    @staticmethod
    def _source(row: dict[str, Any]) -> SourceRecord:
        latest_job = row.get("latest_job")
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
            latest_job=PostgresRepository._job(latest_job) if isinstance(latest_job, dict) else None,
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
            user_id=str(row["user_id"]) if row.get("user_id") else None,
            subscription_id=str(row["subscription_id"]) if row.get("subscription_id") else None,
        )

    @staticmethod
    def _subscription(row: dict[str, Any]) -> CollectionSubscriptionRecord:
        return CollectionSubscriptionRecord(
            id=str(row["id"]),
            user_id=str(row["user_id"]),
            target_id=str(row["target_id"]),
            display_config=dict(row.get("display_config") or {}),
            enabled=bool(row["enabled"]),
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
            parent_job_id=str(row["parent_job_id"]) if row.get("parent_job_id") else None,
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
            author_channel_id=row.get("author_channel_id"),
            author_display_name=row.get("author_display_name"),
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

    def sync_runtime_keys(self, *, runtime_config_id: str, api_keys: tuple[str, ...], encryption_key: str) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            for key in api_keys:
                fingerprint = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
                cursor.execute(
                    """
                    INSERT INTO youtube_runtime_keys (runtime_config_id, key_fingerprint, encrypted_key, status, unavailable_until)
                    VALUES (%s, %s, pgp_sym_encrypt(%s, %s, 'cipher-algo=aes256,compress-algo=0'), 'active', NULL)
                    ON CONFLICT (runtime_config_id, key_fingerprint) DO UPDATE
                    SET encrypted_key = EXCLUDED.encrypted_key, status = 'active', unavailable_until = NULL, updated_at = now()
                    """,
                    (runtime_config_id, fingerprint, key, encryption_key),
                )

    def record_runtime_key_state(self, *, runtime_config_id: str | None, key_fingerprint: str, error_reason: str | None = None) -> None:
        if not runtime_config_id:
            return
        with self._connection() as connection, connection.cursor() as cursor:
            if error_reason:
                cursor.execute(
                    """UPDATE youtube_runtime_keys SET status = 'cooling_down', failure_count = failure_count + 1,
                       last_error_reason = %s, unavailable_until = now() + (LEAST(3, failure_count + 1) * interval '1 hour'), updated_at = now()
                       WHERE runtime_config_id = %s AND key_fingerprint = %s""",
                    (error_reason[:200], runtime_config_id, key_fingerprint),
                )
            else:
                cursor.execute("UPDATE youtube_runtime_keys SET status = 'active', failure_count = 0, last_error_reason = NULL, unavailable_until = NULL, last_used_at = now(), updated_at = now() WHERE runtime_config_id = %s AND key_fingerprint = %s", (runtime_config_id, key_fingerprint))

    def load_runtime_keys(self, *, runtime_config_id: str, encryption_key: str) -> tuple[str, ...]:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT pgp_sym_decrypt(encrypted_key, %s)::text AS api_key FROM youtube_runtime_keys WHERE runtime_config_id = %s AND status <> 'disabled' ORDER BY created_at",
                (encryption_key, runtime_config_id),
            )
            return tuple(str(row["api_key"]) for row in cursor.fetchall())

    @staticmethod
    def _select_source(cursor: Any, source_id: str) -> dict[str, Any] | None:
        cursor.execute(
            """
            SELECT cs.id::text, cs.type::text, cs.config, cs.enabled, cs.created_at, cs.updated_at, cs.next_run_at,
                   cs.target_id::text, ct.canonical_key, ct.coverage, ct.last_completed_at, latest_job.latest_job
            FROM collection_sources cs
            LEFT JOIN collection_targets ct ON ct.id = cs.target_id
            LEFT JOIN LATERAL (
              SELECT to_jsonb(job) AS latest_job
              FROM sync_jobs job
              WHERE (
                  (cs.target_id IS NOT NULL AND job.target_id = cs.target_id)
                  OR (cs.target_id IS NULL AND job.source_id = cs.id)
                )
                AND job.parent_job_id IS NULL
              ORDER BY job.created_at DESC
              LIMIT 1
            ) latest_job ON TRUE
            WHERE cs.id = %s
            """,
            (source_id,),
        )
        return cursor.fetchone()

    @staticmethod
    def _select_subscription(cursor: Any, subscription_id: str, *, owner_id: str | None = None) -> dict[str, Any] | None:
        cursor.execute(
            """
            SELECT subscription.id::text, subscription.user_id::text, subscription.target_id::text,
                   subscription.display_config, subscription.enabled, subscription.created_at, subscription.updated_at
            FROM collection_subscriptions subscription
            WHERE subscription.id = %s
              AND (%s::uuid IS NULL OR subscription.user_id = %s::uuid)
            """,
            (subscription_id, owner_id, owner_id),
        )
        return cursor.fetchone()

    @staticmethod
    def _select_subscription_source(
        cursor: Any, subscription_id: str, *, owner_id: str | None = None
    ) -> dict[str, Any] | None:
        """Return a subscription projected to the existing public SourceRecord shape."""

        cursor.execute(
            """
            SELECT subscription.id::text AS id,
                   target.type::text,
                   COALESCE(NULLIF(subscription.display_config, '{}'::jsonb), target.config) AS config,
                   subscription.enabled,
                   subscription.created_at,
                   subscription.updated_at,
                   NULL::timestamptz AS next_run_at,
                   target.id::text AS target_id,
                   target.canonical_key,
                   target.coverage,
                   target.last_completed_at,
                   latest_job.latest_job
            FROM collection_subscriptions subscription
            JOIN collection_targets target ON target.id = subscription.target_id
            LEFT JOIN LATERAL (
              SELECT to_jsonb(job) AS latest_job
              FROM sync_jobs job
              WHERE job.target_id = target.id AND job.parent_job_id IS NULL
              ORDER BY job.created_at DESC
              LIMIT 1
            ) latest_job ON TRUE
            WHERE subscription.id = %s
              AND (%s::uuid IS NULL OR subscription.user_id = %s::uuid)
            """,
            (subscription_id, owner_id, owner_id),
        )
        return cursor.fetchone()

    @staticmethod
    def _subscription_target_ids(cursor: Any, *, owner_id: str, enabled_only: bool = True) -> set[str]:
        cursor.execute(
            """
            SELECT target_id::text
            FROM collection_subscriptions
            WHERE user_id = %s
              AND (NOT %s OR enabled = TRUE)
            """,
            (owner_id, enabled_only),
        )
        return {str(row["target_id"]) for row in cursor.fetchall()}

    def _ensure_subscription(
        self, cursor: Any, *, owner_id: str, target_id: str, display_config: dict[str, Any]
    ) -> CollectionSubscriptionRecord:
        cursor.execute(
            """
            INSERT INTO collection_subscriptions (user_id, target_id, display_config, enabled)
            VALUES (%s, %s, %s, TRUE)
            ON CONFLICT (user_id, target_id) DO UPDATE
            SET display_config = CASE
                    WHEN EXCLUDED.display_config = '{}'::jsonb THEN collection_subscriptions.display_config
                    ELSE EXCLUDED.display_config
                END,
                enabled = TRUE,
                updated_at = now()
            RETURNING id::text, user_id::text, target_id::text, display_config, enabled, created_at, updated_at
            """,
            (owner_id, target_id, Json(display_config)),
        )
        return self._subscription(cursor.fetchone())

    def _sync_target_pin_for_subscriptions(self, cursor: Any, *, target_id: str) -> None:
        """Enable a target pin only while at least one subscription is enabled."""

        cursor.execute(
            "SELECT type::text FROM collection_targets WHERE id = %s FOR UPDATE",
            (target_id,),
        )
        target = cursor.fetchone()
        if not target:
            return
        cursor.execute(
            "SELECT EXISTS (SELECT 1 FROM collection_subscriptions WHERE target_id = %s AND enabled = TRUE) AS has_enabled",
            (target_id,),
        )
        has_enabled = bool(cursor.fetchone()["has_enabled"])
        if not has_enabled:
            cursor.execute(
                "UPDATE collection_target_pins SET enabled = FALSE, updated_at = now() WHERE target_id = %s",
                (target_id,),
            )
            return
        cursor.execute(
            "SELECT 1 FROM collection_target_pins WHERE target_id = %s FOR UPDATE",
            (target_id,),
        )
        if cursor.fetchone():
            cursor.execute(
                "UPDATE collection_target_pins SET enabled = TRUE, next_run_at = now(), updated_at = now() WHERE target_id = %s",
                (target_id,),
            )
        elif target["type"] == SourceType.CHANNEL.value:
            cursor.execute(
                """
                INSERT INTO collection_target_pins (target_id, enabled, interval_minutes, next_run_at)
                VALUES (%s, TRUE, 360, now())
                ON CONFLICT (target_id) DO UPDATE
                SET enabled = TRUE, next_run_at = now(), updated_at = now()
                """,
                (target_id,),
            )

    def get_subscription(
        self, subscription_id: str, *, owner_id: str | None = None
    ) -> CollectionSubscriptionRecord:
        with self._connection() as connection, connection.cursor() as cursor:
            row = self._select_subscription(cursor, subscription_id, owner_id=owner_id)
            if not row:
                raise NotFoundError(f"Subscription '{subscription_id}' was not found")
            return self._subscription(row)

    def list_subscriptions(self, *, owner_id: str) -> list[CollectionSubscriptionRecord]:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id::text, user_id::text, target_id::text, display_config, enabled, created_at, updated_at
                FROM collection_subscriptions
                WHERE user_id = %s
                ORDER BY created_at, id
                """,
                (owner_id,),
            )
            return [self._subscription(row) for row in cursor.fetchall()]

    def ensure_subscription(
        self, *, owner_id: str, target_id: str, display_config: dict[str, Any]
    ) -> CollectionSubscriptionRecord:
        with self._connection() as connection, connection.cursor() as cursor:
            subscription = self._ensure_subscription(
                cursor,
                owner_id=owner_id,
                target_id=target_id,
                display_config=display_config,
            )
            self._sync_target_pin_for_subscriptions(cursor, target_id=target_id)
            return subscription

    def create_source(self, *, source_type: SourceType, config: dict[str, Any], owner_id: str | None = None) -> SourceRecord:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO collection_sources (type, config, owner_id)
                VALUES (%s, %s, %s)
                RETURNING id::text
                """,
                (source_type.value, Json(config), owner_id),
            )
            row = self._select_source(cursor, str(cursor.fetchone()["id"]))
            assert row is not None
            return self._source(row)

    def get_source(self, source_id: str, *, owner_id: str | None = None) -> SourceRecord:
        with self._connection() as connection, connection.cursor() as cursor:
            subscription_row = self._select_subscription_source(cursor, source_id, owner_id=owner_id)
            if subscription_row:
                return self._source(subscription_row)
            # Do not fall through to a physical source if a subscription with this
            # ID exists but belongs to another user.
            if self._select_subscription(cursor, source_id) is not None:
                raise NotFoundError(f"Source '{source_id}' was not found")
            row = self._select_source(cursor, source_id)
            if not row:
                raise NotFoundError(f"Source '{source_id}' was not found")
            if owner_id is not None:
                if row.get("target_id"):
                    raise NotFoundError(f"Source '{source_id}' was not found")
                cursor.execute("SELECT 1 FROM collection_sources WHERE id = %s AND owner_id = %s", (source_id, owner_id))
                if not cursor.fetchone():
                    raise NotFoundError(f"Source '{source_id}' was not found")
            return self._source(row)

    def list_sources(self, *, owner_id: str | None = None) -> list[SourceRecord]:
        with self._connection() as connection, connection.cursor() as cursor:
            if owner_id is not None:
                cursor.execute(
                    """
                    SELECT subscription.id::text AS id,
                           target.type::text,
                           COALESCE(NULLIF(subscription.display_config, '{}'::jsonb), target.config) AS config,
                           subscription.enabled,
                           subscription.created_at,
                           subscription.updated_at,
                           NULL::timestamptz AS next_run_at,
                           target.id::text AS target_id,
                           target.canonical_key,
                           target.coverage,
                           target.last_completed_at,
                           latest_job.latest_job
                    FROM collection_subscriptions subscription
                    JOIN collection_targets target ON target.id = subscription.target_id
                    LEFT JOIN LATERAL (
                      SELECT to_jsonb(job) AS latest_job
                      FROM sync_jobs job
                      WHERE job.target_id = target.id AND job.parent_job_id IS NULL
                      ORDER BY job.created_at DESC
                      LIMIT 1
                    ) latest_job ON TRUE
                    WHERE subscription.user_id = %s

                    UNION ALL

                    SELECT source.id::text AS id,
                           source.type::text,
                           source.config,
                           source.enabled,
                           source.created_at,
                           source.updated_at,
                           source.next_run_at,
                           NULL::text AS target_id,
                           NULL::text AS canonical_key,
                           '{}'::jsonb AS coverage,
                           NULL::timestamptz AS last_completed_at,
                           latest_job.latest_job
                    FROM collection_sources source
                    LEFT JOIN LATERAL (
                      SELECT to_jsonb(job) AS latest_job
                      FROM sync_jobs job
                      WHERE job.source_id = source.id
                        AND job.target_id IS NULL
                        AND job.parent_job_id IS NULL
                      ORDER BY job.created_at DESC
                      LIMIT 1
                    ) latest_job ON TRUE
                    WHERE source.owner_id = %s AND source.target_id IS NULL

                    ORDER BY created_at, id
                    """,
                    (owner_id, owner_id),
                )
                return [self._source(row) for row in cursor.fetchall()]

            cursor.execute(
                """
                SELECT cs.id::text, cs.type::text, cs.config, cs.enabled, cs.created_at, cs.updated_at, cs.next_run_at,
                       cs.target_id::text, ct.canonical_key, ct.coverage, ct.last_completed_at, latest_job.latest_job
                FROM collection_sources cs
                LEFT JOIN collection_targets ct ON ct.id = cs.target_id
                LEFT JOIN LATERAL (
                  SELECT to_jsonb(job) AS latest_job
                  FROM sync_jobs job
                  WHERE (
                      (cs.target_id IS NOT NULL AND job.target_id = cs.target_id)
                      OR (cs.target_id IS NULL AND job.source_id = cs.id)
                    )
                    AND job.parent_job_id IS NULL
                  ORDER BY job.created_at DESC
                  LIMIT 1
                ) latest_job ON TRUE
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

    def source_owned_by(self, *, source_id: str, owner_id: str) -> bool:
        with self._connection() as connection, connection.cursor() as cursor:
            subscription = self._select_subscription(cursor, source_id, owner_id=owner_id)
            if subscription:
                return True
            cursor.execute(
                "SELECT 1 FROM collection_sources WHERE id = %s AND owner_id = %s AND target_id IS NULL",
                (source_id, owner_id),
            )
            return cursor.fetchone() is not None

    def target_owned_by(self, *, target_id: str, owner_id: str) -> bool:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM collection_subscriptions WHERE target_id = %s AND user_id = %s LIMIT 1",
                (target_id, owner_id),
            )
            return cursor.fetchone() is not None

    def job_owned_by(self, *, job_id: str, owner_id: str) -> bool:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT 1
                FROM sync_jobs job
                LEFT JOIN collection_subscriptions subscription ON subscription.target_id = job.target_id
                LEFT JOIN collection_sources source ON source.id = job.source_id
                WHERE job.id = %s
                  AND (subscription.user_id = %s OR (source.owner_id = %s AND source.target_id IS NULL))
                LIMIT 1
                """,
                (job_id, owner_id, owner_id),
            )
            return cursor.fetchone() is not None

    def owner_has_sources(self, *, owner_id: str) -> bool:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT 1
                FROM collection_subscriptions
                WHERE user_id = %s
                UNION ALL
                SELECT 1 FROM collection_sources WHERE owner_id = %s AND target_id IS NULL
                LIMIT 1
                """,
                (owner_id, owner_id),
            )
            return cursor.fetchone() is not None

    def subscription_target_ids(self, *, owner_id: str, enabled_only: bool = True) -> set[str]:
        with self._connection() as connection, connection.cursor() as cursor:
            return self._subscription_target_ids(cursor, owner_id=owner_id, enabled_only=enabled_only)

    def assign_source_owner(self, *, source_id: str, owner_id: str) -> None:
        """Compatibility shim for older routes; new requests already have a subscription."""

        with self._connection() as connection, connection.cursor() as cursor:
            if self._select_subscription(cursor, source_id) is not None:
                return
            cursor.execute(
                "UPDATE collection_sources SET owner_id = %s WHERE id = %s AND owner_id IS NULL RETURNING target_id::text",
                (owner_id, source_id),
            )
            row = cursor.fetchone()
            if row and row.get("target_id"):
                self._ensure_subscription(cursor, owner_id=owner_id, target_id=str(row["target_id"]), display_config={})
                self._sync_target_pin_for_subscriptions(cursor, target_id=str(row["target_id"]))

    def update_source(self, source_id: str, *, owner_id: str | None = None, **changes: Any) -> SourceRecord:
        allowed = {"enabled", "config", "next_run_at"}
        unknown = changes.keys() - allowed
        if unknown:
            raise RepositoryError(f"Unsupported source changes: {', '.join(sorted(unknown))}")
        with self._connection() as connection, connection.cursor() as cursor:
            subscription = self._select_subscription(cursor, source_id, owner_id=owner_id)
            if subscription:
                assignments: list[str] = []
                values: list[Any] = []
                if "enabled" in changes:
                    assignments.append("enabled = %s")
                    values.append(bool(changes["enabled"]))
                if "config" in changes:
                    assignments.append("display_config = %s")
                    values.append(Json(changes["config"]))
                if assignments:
                    values.append(source_id)
                    cursor.execute(
                        f"UPDATE collection_subscriptions SET {', '.join(assignments)}, updated_at = now() WHERE id = %s",
                        values,
                    )
                    self._sync_target_pin_for_subscriptions(cursor, target_id=subscription.target_id)
                selected = self._select_subscription_source(cursor, source_id, owner_id=owner_id)
                assert selected is not None
                return self._source(selected)
            if self._select_subscription(cursor, source_id) is not None:
                raise NotFoundError(f"Source '{source_id}' was not found")
            if not changes:
                return self.get_source(source_id, owner_id=owner_id)
            assignments = []
            values = []
            for key, value in changes.items():
                assignments.append(f"{key} = %s")
                values.append(Json(value) if key == "config" else value)
            values.extend((source_id, owner_id, owner_id))
            cursor.execute(
                f"UPDATE collection_sources SET {', '.join(assignments)}, updated_at = now() "
                "WHERE id = %s AND (%s::uuid IS NULL OR (owner_id = %s::uuid AND target_id IS NULL)) RETURNING id::text",
                values,
            )
            row = cursor.fetchone()
            if not row:
                raise NotFoundError(f"Source '{source_id}' was not found")
            selected = self._select_source(cursor, str(row["id"]))
            assert selected is not None
            return self._source(selected)

    def delete_source(self, source_id: str, *, owner_id: str | None = None) -> None:
        with self._connection() as connection, connection.cursor() as cursor:
            subscription = self._select_subscription(cursor, source_id, owner_id=owner_id)
            if subscription:
                # Deleting a subscription intentionally consumes any request
                # replay key tied to it.  Without this, a later retry would
                # find the historical audit row after its FK is SET NULL and
                # return the internal worker source instead of a new
                # user-facing subscription.
                cursor.execute(
                    "UPDATE collection_requests SET idempotency_key = NULL, updated_at = now() WHERE subscription_id = %s",
                    (source_id,),
                )
                cursor.execute("DELETE FROM collection_subscriptions WHERE id = %s", (source_id,))
                self._sync_target_pin_for_subscriptions(cursor, target_id=subscription.target_id)
                return
            if self._select_subscription(cursor, source_id) is not None:
                raise NotFoundError(f"Source '{source_id}' was not found")
            cursor.execute(
                "SELECT target_id::text FROM collection_sources WHERE id = %s "
                "AND (%s::uuid IS NULL OR (owner_id = %s::uuid AND target_id IS NULL)) FOR UPDATE",
                (source_id, owner_id, owner_id),
            )
            row = cursor.fetchone()
            if not row:
                raise NotFoundError(f"Source '{source_id}' was not found")
            target_id = row.get("target_id")
            if not target_id:
                cursor.execute("DELETE FROM collection_sources WHERE id = %s", (source_id,))
                return
            # A physical legacy source is never a reason to erase a shared target
            # that has user subscriptions.  The old destructive behavior remains
            # only for genuinely unclaimed legacy targets.
            cursor.execute("SELECT 1 FROM collection_subscriptions WHERE target_id = %s LIMIT 1", (target_id,))
            if cursor.fetchone():
                cursor.execute("DELETE FROM collection_sources WHERE id = %s", (source_id,))
                return
            cursor.execute("DELETE FROM collection_sources WHERE target_id = %s", (target_id,))
            cursor.execute("DELETE FROM collection_targets WHERE id = %s", (target_id,))
