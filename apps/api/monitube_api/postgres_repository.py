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

from .analysis import build_summary, top_words_from_texts
from .fuzzy_search import normalize_search_text, rank_text_fields
from .domain import (
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
from .repositories import (
    CollectionRepository,
    CommentThreadSort,
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


def _strip_nul(value: Any) -> Any:
    """PostgreSQL text fields reject NUL bytes from upstream public metadata."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {key: _strip_nul(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_strip_nul(item) for item in value]
    return value


def _optional_nonnegative_int(value: Any) -> int | None:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


class PostgresRepository(CollectionRepository):
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
        owner_id: str | None = None,
        runtime_config_id: str | None = None,
    ) -> JobRecord:
        with self._connection() as connection, connection.cursor() as cursor:
            subscription = self._select_subscription(cursor, source_id, owner_id=owner_id)
            if subscription:
                target_id = subscription["target_id"]
                worker_source = self._target_source(cursor, str(target_id))
                if not worker_source:
                    raise RepositoryError(f"Target '{target_id}' has no worker source")
                worker_source_id = worker_source.id
            else:
                if self._select_subscription(cursor, source_id) is not None:
                    raise NotFoundError(f"Source '{source_id}' was not found")
                cursor.execute(
                    "SELECT id::text, target_id::text FROM collection_sources "
                    "WHERE id = %s AND (%s::uuid IS NULL OR (owner_id = %s::uuid AND target_id IS NULL))",
                    (source_id, owner_id, owner_id),
                )
                source = cursor.fetchone()
                if not source:
                    raise NotFoundError(f"Source '{source_id}' was not found")
                target_id = source.get("target_id")
                worker_source_id = str(source["id"])
            if target_id:
                cursor.execute("SELECT id FROM collection_targets WHERE id = %s FOR UPDATE", (target_id,))
                cursor.execute(
                    """
                    SELECT * FROM sync_jobs
                    WHERE target_id = %s AND state IN ('queued', 'running', 'waiting_retry', 'waiting_quota')
                    ORDER BY created_at
                    LIMIT 1
                    FOR UPDATE
                    """,
                    (target_id,),
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
                (worker_source_id, target_id, config_id, new_id(), include_comments, max_videos, max_comments_per_video),
            )
            return self._job(cursor.fetchone())

    @staticmethod
    def _desired_coverage(source_type: SourceType, config: dict[str, Any]) -> dict[str, Any]:
        desired: dict[str, Any] = {
            "complete": False,
            "includeComments": bool(config.get("includeComments", False)),
            "collectAllComments": bool(config.get("includeComments", False) and config.get("collectAllComments", False)),
            "maxCommentPagesPerVideo": int(config.get("maxCommentPagesPerVideo") or 1),
        }
        if source_type is SourceType.CHANNEL:
            desired["collectAllVideos"] = bool(config.get("collectAllVideos", False))
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
        merged["collectAllComments"] = bool(
            current.get("collectAllComments", False) or incoming.get("collectAllComments", False)
        )
        merged["maxCommentPagesPerVideo"] = max(
            int(current.get("maxCommentPagesPerVideo") or 1), int(incoming.get("maxCommentPagesPerVideo") or 1)
        )
        if source_type is SourceType.CHANNEL:
            merged["collectAllVideos"] = bool(current.get("collectAllVideos", False) or incoming.get("collectAllVideos", False))
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
        if desired.get("collectAllComments") and not coverage.get("collectAllComments"):
            return False
        if desired.get("collectAllVideos") and not coverage.get("collectAllVideos"):
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
            "collectAllComments": bool(job.include_comments and source_config.get("collectAllComments")),
            "maxCommentPagesPerVideo": int(job.max_comments_per_video or source_config.get("maxCommentPagesPerVideo") or 1),
        }
        if source_type is SourceType.CHANNEL:
            coverage["collectAllVideos"] = bool(source_config.get("collectAllVideos"))
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

    def enqueue_video_jobs(self, *, parent_job: JobRecord, youtube_video_ids: Iterable[str]) -> int:
        ids = list(dict.fromkeys(str(value) for value in youtube_video_ids if str(value)))
        if not ids:
            return 0
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO sync_jobs (
                  source_id, runtime_config_id, parent_job_id, state, current_stage,
                  idempotency_key, include_comments, max_videos, max_comments_per_video,
                  progress_total, progress_unit, checkpoint
                ) VALUES (%s, %s, %s, 'queued', 'queued_video', %s, %s, 1, %s, 1, 'videos', %s)
                ON CONFLICT (source_id, idempotency_key) DO NOTHING
                """,
                [
                    (
                        parent_job.source_id, parent_job.runtime_config_id, parent_job.id,
                        f"video:{parent_job.id}:{video_id}", parent_job.include_comments,
                        parent_job.max_comments_per_video,
                        Json({"jobKind": "video", "youtubeVideoId": video_id}),
                    )
                    for video_id in ids
                ],
            )
            return cursor.rowcount

    def child_job_summary(self, *, parent_job_id: str) -> tuple[int, int, int]:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT count(*)::integer AS total,
                       count(*) FILTER (WHERE state IN ('completed', 'completed_with_warnings', 'failed', 'cancelled'))::integer AS terminal,
                       count(*) FILTER (WHERE state IN ('failed', 'cancelled'))::integer AS failed
                FROM sync_jobs WHERE parent_job_id = %s
                """,
                (parent_job_id,),
            )
            row = cursor.fetchone()
            return int(row["total"]), int(row["terminal"]), int(row["failed"])

    def _submission(self, cursor: Any, request: CollectionRequestRecord) -> CollectionSubmission:
        cursor.execute("SELECT * FROM collection_targets WHERE id = %s", (request.target_id,))
        target_row = cursor.fetchone()
        if not target_row:
            raise NotFoundError(f"Target '{request.target_id}' was not found")
        target = self._target(target_row)
        if request.subscription_id:
            source_row = self._select_subscription_source(cursor, request.subscription_id)
            source = self._source(source_row) if source_row else None
        else:
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
        owner_id: str | None = None,
        runtime_config_id: str | None = None,
    ) -> CollectionSubmission:
        with self._connection() as connection, connection.cursor() as cursor:
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
                    """
                    SELECT * FROM collection_requests
                    WHERE target_id = %s
                      AND user_id IS NOT DISTINCT FROM %s::uuid
                      AND idempotency_key = %s
                    ORDER BY created_at
                    LIMIT 1
                    FOR UPDATE
                    """,
                    (target.id, owner_id, idempotency_key),
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

            subscription: CollectionSubscriptionRecord | None = None
            if owner_id:
                subscription = self._ensure_subscription(
                    cursor,
                    owner_id=owner_id,
                    target_id=target.id,
                    display_config=config,
                )
                self._sync_target_pin_for_subscriptions(cursor, target_id=target.id)

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
                INSERT INTO collection_requests (
                    target_id, source_id, request_config, idempotency_key, job_id, status, user_id, subscription_id
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    target.id,
                    source.id if source_is_new else None,
                    Json(config),
                    idempotency_key,
                    request_job_id,
                    request_status,
                    owner_id,
                    subscription.id if subscription else None,
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
                # Move user-facing subscriptions before retiring the provisional
                # target.  Preserve each non-conflicting subscription UUID so
                # existing Sources URLs remain valid.  Only a user who already
                # has a destination subscription needs a merge/delete.
                cursor.execute(
                    """
                    UPDATE collection_requests request
                    SET subscription_id = destination.id
                    FROM collection_subscriptions provisional
                    JOIN collection_subscriptions destination
                      ON destination.user_id = provisional.user_id
                     AND destination.target_id = %s
                    WHERE provisional.target_id = %s
                      AND request.subscription_id = provisional.id
                    """,
                    (target.id, current_target.id),
                )
                cursor.execute(
                    """
                    UPDATE collection_subscriptions destination
                    SET enabled = destination.enabled OR provisional.enabled,
                        updated_at = now()
                    FROM collection_subscriptions provisional
                    WHERE provisional.target_id = %s
                      AND destination.target_id = %s
                      AND destination.user_id = provisional.user_id
                    """,
                    (current_target.id, target.id),
                )
                cursor.execute(
                    """
                    DELETE FROM collection_subscriptions provisional
                    USING collection_subscriptions destination
                    WHERE provisional.target_id = %s
                      AND destination.target_id = %s
                      AND destination.user_id = provisional.user_id
                    """,
                    (current_target.id, target.id),
                )
                cursor.execute(
                    "UPDATE collection_subscriptions SET target_id = %s, updated_at = now() WHERE target_id = %s",
                    (target.id, current_target.id),
                )
                # The target-scoped idempotency key is unique per user.  Requests
                # from two targets can legitimately share it before promotion; the
                # older provisional audit row remains, but no longer competes with
                # the canonical request's retry key after the target merge.
                cursor.execute(
                    """
                    UPDATE collection_requests provisional_request
                    SET idempotency_key = NULL, updated_at = now()
                    FROM collection_requests canonical_request
                    WHERE provisional_request.target_id = %s
                      AND canonical_request.target_id = %s
                      AND provisional_request.user_id IS NOT DISTINCT FROM canonical_request.user_id
                      AND provisional_request.idempotency_key = canonical_request.idempotency_key
                      AND provisional_request.idempotency_key IS NOT NULL
                    """,
                    (current_target.id, target.id),
                )
                cursor.execute(
                    """
                    INSERT INTO collection_target_pins (
                        target_id, enabled, interval_minutes, next_run_at, last_dispatched_at
                    )
                    SELECT %s, enabled, interval_minutes, next_run_at, last_dispatched_at
                    FROM collection_target_pins
                    WHERE target_id = %s
                    ON CONFLICT (target_id) DO UPDATE
                    SET enabled = collection_target_pins.enabled OR EXCLUDED.enabled,
                        next_run_at = LEAST(collection_target_pins.next_run_at, EXCLUDED.next_run_at),
                        updated_at = now()
                    """,
                    (target.id, current_target.id),
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
                self._sync_target_pin_for_subscriptions(cursor, target_id=target.id)
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

    def get_job(self, job_id: str, *, owner_id: str | None = None) -> JobRecord:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT job.*
                FROM sync_jobs job
                LEFT JOIN collection_sources source ON source.id = job.source_id
                WHERE job.id = %s
                  AND (
                    %s::uuid IS NULL
                    OR EXISTS (
                      SELECT 1 FROM collection_subscriptions subscription
                      WHERE subscription.target_id = job.target_id AND subscription.user_id = %s::uuid
                    )
                    OR (source.target_id IS NULL AND source.owner_id = %s::uuid)
                  )
                """,
                (job_id, owner_id, owner_id, owner_id),
            )
            row = cursor.fetchone()
            if not row:
                raise NotFoundError(f"Job '{job_id}' was not found")
            return self._job(row)

    def list_jobs_for_source(
        self, source_id: str, *, limit: int = 20, owner_id: str | None = None
    ) -> list[JobRecord]:
        with self._connection() as connection, connection.cursor() as cursor:
            source_row = self._select_subscription_source(cursor, source_id, owner_id=owner_id)
            if source_row:
                source = self._source(source_row)
            else:
                if self._select_subscription(cursor, source_id) is not None:
                    raise NotFoundError(f"Source '{source_id}' was not found")
                source_row = self._select_source(cursor, source_id)
                if not source_row:
                    raise NotFoundError(f"Source '{source_id}' was not found")
                if owner_id is not None:
                    if source_row.get("target_id"):
                        raise NotFoundError(f"Source '{source_id}' was not found")
                    cursor.execute("SELECT 1 FROM collection_sources WHERE id = %s AND owner_id = %s", (source_id, owner_id))
                    if not cursor.fetchone():
                        raise NotFoundError(f"Source '{source_id}' was not found")
                source = self._source(source_row)
            if source.target_id:
                cursor.execute("SELECT * FROM sync_jobs WHERE target_id = %s ORDER BY updated_at DESC LIMIT %s", (source.target_id, limit))
            else:
                cursor.execute("SELECT * FROM sync_jobs WHERE source_id = %s ORDER BY updated_at DESC LIMIT %s", (source_id, limit))
            return [self._job(row) for row in cursor.fetchall()]

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
            # Use one global UUID order for every target affected by a terminal
            # parent. Two collections can share a video; locking only each job's
            # own target first would let A->B and B->A deadlock. Request
            # submission also takes the target lock before the job lock.
            cursor.execute(
                "SELECT target_id, parent_job_id, state, checkpoint FROM sync_jobs WHERE id = %s",
                (job_id,),
            )
            target_hint = cursor.fetchone()
            if not target_hint:
                raise NotFoundError(f"Job '{job_id}' was not found")
            terminal_target_ids: list[str] = []
            if target_hint.get("target_id"):
                current_state = JobState(str(target_hint["state"]))
                if (
                    state.is_terminal
                    and not current_state.is_terminal
                    and target_hint.get("parent_job_id") is None
                ):
                    pending_checkpoint = changes.get("checkpoint")
                    checkpoint_hint = (
                        pending_checkpoint
                        if isinstance(pending_checkpoint, dict)
                        else dict(target_hint.get("checkpoint") or {})
                    )
                    cursor.execute(
                        """
                        WITH touched_video_ids AS (
                          SELECT video.id
                          FROM sync_jobs child
                          JOIN videos video
                            ON video.youtube_video_id = child.checkpoint ->> 'youtubeVideoId'
                          WHERE child.parent_job_id = %s
                          UNION
                          SELECT video.id
                          FROM videos video
                          WHERE video.youtube_video_id = %s
                        ), affected AS (
                          SELECT DISTINCT membership.target_id
                          FROM collection_target_videos membership
                          JOIN touched_video_ids touched ON touched.id = membership.video_id
                          UNION
                          SELECT %s::uuid
                        )
                        SELECT target_id::text
                        FROM affected
                        ORDER BY target_id
                        """,
                        (
                            job_id,
                            str(checkpoint_hint.get("youtubeVideoId") or ""),
                            target_hint["target_id"],
                        ),
                    )
                    terminal_target_ids = [str(item["target_id"]) for item in cursor.fetchall()]
                    cursor.execute(
                        """
                        SELECT id
                        FROM collection_targets
                        WHERE id = ANY(%s::uuid[])
                        ORDER BY id
                        FOR UPDATE
                        """,
                        (terminal_target_ids,),
                    )
                    cursor.fetchall()
                else:
                    cursor.execute(
                        "SELECT id FROM collection_targets WHERE id = %s FOR UPDATE",
                        (target_hint["target_id"],),
                    )
            cursor.execute("SELECT * FROM sync_jobs WHERE id = %s FOR UPDATE", (job_id,))
            row = cursor.fetchone()
            if not row:
                raise NotFoundError(f"Job '{job_id}' was not found")
            current = self._job(row)
            if state != current.state and state not in _ALLOWED_TRANSITIONS[current.state]:
                raise InvalidStateTransitionError(f"Cannot transition job '{job_id}' from {current.state.value} to {state.value}")
            became_terminal = not current.state.is_terminal and state.is_terminal
            assignments = ["state = %s"]
            values: list[Any] = [state.value]
            for key, value in changes.items():
                assignments.append(f"{key} = %s")
                values.append(Json(value) if key in {"checkpoint", "partial_errors"} else value)
            values.append(job_id)
            cursor.execute(f"UPDATE sync_jobs SET {', '.join(assignments)}, updated_at = now() WHERE id = %s RETURNING *", values)
            updated = self._job(cursor.fetchone())
            if became_terminal:
                cursor.execute(
                    "UPDATE collection_requests SET status = %s, updated_at = now() WHERE job_id = %s",
                    (updated.state.value, updated.id),
                )
                if updated.target_id:
                    if updated.state in {JobState.COMPLETED, JobState.COMPLETED_WITH_WARNINGS}:
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
                # Child video jobs are implementation details.  A parent (or a
                # direct, targetless legacy job) advances the public data
                # version once, after all committed child work is visible.
                if updated.parent_job_id is None:
                    affected_targets: list[tuple[str, int]] = []
                    if updated.target_id:
                        if not terminal_target_ids:
                            terminal_target_ids = [updated.target_id]
                        cursor.execute(
                            """
                            UPDATE collection_targets target
                            SET data_version = target.data_version + 1, updated_at = now()
                            WHERE target.id = ANY(%s::uuid[])
                            RETURNING target.id::text, target.data_version
                            """,
                            (terminal_target_ids,),
                        )
                        affected_targets = [
                            (str(item["id"]), int(item["data_version"]))
                            for item in cursor.fetchall()
                        ]
                    else:
                        cursor.execute(
                            """
                            UPDATE collection_sources
                            SET data_version = data_version + 1, updated_at = now()
                            WHERE id = %s
                            RETURNING data_version
                            """,
                            (updated.source_id,),
                        )
                        source_version_row = cursor.fetchone()
                        if (
                            source_version_row
                            and self.enable_target_summary_write
                            and updated.state in {JobState.COMPLETED, JobState.COMPLETED_WITH_WARNINGS}
                        ):
                            cursor.execute(
                                """
                                INSERT INTO analysis_runs (
                                  source_id, job_id, data_version, state,
                                  pipeline_version, policy_gate_version, sample_plan, coverage
                                )
                                SELECT %s, %s, %s, 'queued', 'deterministic-v2',
                                       'server-managed', %s, %s
                                WHERE NOT EXISTS (
                                  SELECT 1 FROM analysis_runs
                                  WHERE target_id IS NULL AND source_id = %s
                                    AND data_version = %s
                                    AND pipeline_version = 'deterministic-v2'
                                )
                                """,
                                (
                                    updated.source_id,
                                    updated.id,
                                    int(source_version_row["data_version"]),
                                    Json({"strategy": "per-video-recent", "maxComments": 50_000, "maxPerVideo": 1_000}),
                                    Json({"partial": updated.state is JobState.COMPLETED_WITH_WARNINGS}),
                                    updated.source_id,
                                    int(source_version_row["data_version"]),
                                ),
                            )
                    if (
                        affected_targets
                        and self.enable_target_summary_write
                        and updated.state in {JobState.COMPLETED, JobState.COMPLETED_WITH_WARNINGS}
                    ):
                        for target_id, data_version in affected_targets:
                            cursor.execute(
                                """
                                INSERT INTO analysis_runs (
                                  source_id, target_id, job_id, data_version, state,
                                  pipeline_version, policy_gate_version, sample_plan, coverage
                                )
                                SELECT %s, %s, %s, %s, 'queued', 'deterministic-v2',
                                       'server-managed', %s, %s
                                WHERE NOT EXISTS (
                                  SELECT 1 FROM analysis_runs
                                  WHERE target_id = %s AND data_version = %s
                                    AND pipeline_version = 'deterministic-v2'
                                )
                                """,
                                (
                                    updated.source_id if target_id == updated.target_id else None,
                                    target_id,
                                    updated.id,
                                    data_version,
                                    Json({"strategy": "per-video-recent", "maxComments": 50_000, "maxPerVideo": 1_000}),
                                    Json({"partial": updated.state is JobState.COMPLETED_WITH_WARNINGS}),
                                    target_id,
                                    data_version,
                                ),
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
        channel = _strip_nul(channel)
        statistics = dict(channel.get("statistics") or {})
        payload = {**channel, "thumbnail_url": channel.get("thumbnail_url")}
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO channels (youtube_channel_id, handle, title, description, thumbnail_url, uploads_playlist_id, source_fetched_at)
                VALUES (%(youtube_channel_id)s, %(handle)s, %(title)s, %(description)s, %(thumbnail_url)s, %(uploads_playlist_id)s, %(source_fetched_at)s)
                ON CONFLICT (youtube_channel_id) DO UPDATE SET
                  handle = COALESCE(EXCLUDED.handle, channels.handle),
                  title = COALESCE(EXCLUDED.title, channels.title),
                  description = COALESCE(EXCLUDED.description, channels.description),
                  thumbnail_url = COALESCE(EXCLUDED.thumbnail_url, channels.thumbnail_url),
                  uploads_playlist_id = COALESCE(EXCLUDED.uploads_playlist_id, channels.uploads_playlist_id),
                  source_fetched_at = EXCLUDED.source_fetched_at
                RETURNING id::text, youtube_channel_id
                """,
                payload,
            )
            stored = dict(cursor.fetchone())
            if statistics:
                cursor.execute(
                    """
                    INSERT INTO channel_snapshots (channel_id, fetched_at, subscriber_count, view_count, video_count, hidden_subscriber_count)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (channel_id, fetched_at) DO UPDATE SET
                      subscriber_count = EXCLUDED.subscriber_count, view_count = EXCLUDED.view_count,
                      video_count = EXCLUDED.video_count, hidden_subscriber_count = EXCLUDED.hidden_subscriber_count
                    """,
                    (
                        stored["id"], channel["source_fetched_at"],
                        _optional_nonnegative_int(statistics.get("subscriberCount")),
                        _optional_nonnegative_int(statistics.get("viewCount")),
                        _optional_nonnegative_int(statistics.get("videoCount")),
                        bool(statistics.get("hiddenSubscriberCount")) if statistics.get("hiddenSubscriberCount") is not None else None,
                    ),
                )
            return stored

    def upsert_video(self, video: VideoRecord) -> VideoRecord:
        video = replace(
            video,
            youtube_video_id=_strip_nul(video.youtube_video_id),
            youtube_channel_id=_strip_nul(video.youtube_channel_id),
            title=_strip_nul(video.title),
            description=_strip_nul(video.description),
            privacy_status=_strip_nul(video.privacy_status),
        )
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

    def get_videos_by_youtube_ids(self, youtube_video_ids: Iterable[str]) -> dict[str, VideoRecord]:
        video_ids = list(dict.fromkeys(youtube_video_ids))
        if not video_ids:
            return {}
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT v.id::text, v.youtube_video_id, c.youtube_channel_id, v.title, v.description,
                       v.published_at, v.duration_seconds, v.privacy_status, v.made_for_kids,
                       COALESCE(
                         (SELECT jsonb_build_object(
                           'viewCount', vs.view_count,
                           'likeCount', vs.like_count,
                           'commentCount', vs.comment_count
                         )
                         FROM video_stat_snapshots vs
                         WHERE vs.video_id = v.id
                         ORDER BY vs.fetched_at DESC
                         LIMIT 1),
                         '{}'::jsonb
                       ) AS statistics,
                       v.source_fetched_at
                FROM videos v
                LEFT JOIN channels c ON c.id = v.channel_id
                WHERE v.youtube_video_id = ANY(%s)
                """,
                (video_ids,),
            )
            return {row["youtube_video_id"]: self._video(row) for row in cursor.fetchall()}

    def count_videos_by_channel(self, youtube_channel_id: str) -> int:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT count(*)::integer AS video_count
                FROM videos v
                JOIN channels c ON c.id = v.channel_id
                WHERE c.youtube_channel_id = %s
                """,
                (youtube_channel_id,),
            )
            return int(cursor.fetchone()["video_count"])

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

    def source_video_ids(self, source_id: str, youtube_video_ids: Iterable[str]) -> set[str]:
        ids = list(dict.fromkeys(youtube_video_ids))
        if not ids:
            return set()
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT video.youtube_video_id FROM source_videos source_video
                   JOIN videos video ON video.id = source_video.video_id
                   WHERE source_video.source_id = %s AND video.youtube_video_id = ANY(%s)""",
                (source_id, ids),
            )
            return {str(row["youtube_video_id"]) for row in cursor.fetchall()}

    def count_source_videos(self, source_id: str) -> int:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT count(*)::integer AS video_count FROM source_videos WHERE source_id = %s", (source_id,))
            return int(cursor.fetchone()["video_count"])

    def upsert_comment(self, comment: CommentRecord) -> CommentRecord:
        return self.persist_comment_page([comment])[0]

    def persist_comment_page(
        self,
        comments: Iterable[CommentRecord],
        *,
        job_id: str | None = None,
        checkpoint: dict[str, Any] | None = None,
    ) -> list[CommentRecord]:
        """Persist one upstream page using one connection and one transaction.

        Top-level comments are written before replies so parent foreign keys are
        resolved deterministically.  The rollup is recomputed while holding the
        video row lock: replaying a page after a crash therefore cannot inflate
        counts.  A supplied checkpoint commits in the same transaction.
        """

        sanitized_page = [
            replace(
                comment,
                youtube_comment_id=_strip_nul(comment.youtube_comment_id),
                youtube_video_id=_strip_nul(comment.youtube_video_id),
                youtube_parent_comment_id=_strip_nul(comment.youtube_parent_comment_id),
                youtube_thread_id=_strip_nul(comment.youtube_thread_id),
                text_display=_strip_nul(comment.text_display),
            )
            for comment in comments
        ]
        # Upstream pages should already be unique, but de-duplicating here keeps
        # both the SQL upsert and rollup delta idempotent under malformed/replayed
        # responses.  Dict insertion order preserves the first-seen page order.
        page_by_id: dict[str, CommentRecord] = {}
        for comment in sanitized_page:
            page_by_id[comment.youtube_comment_id] = comment
        page = list(page_by_id.values())
        video_ids = {comment.youtube_video_id for comment in page}
        if len(video_ids) > 1:
            raise RepositoryError("A comment page must belong to exactly one video")
        if checkpoint is not None and not job_id:
            raise RepositoryError("job_id is required when persisting a checkpoint")

        with self._connection() as connection, connection.cursor() as cursor:
            video: dict[str, Any] | None = None
            existing_comments: dict[str, dict[str, Any]] = {}
            absolute_rollup_required = False
            if video_ids:
                youtube_video_id = next(iter(video_ids))
                cursor.execute("SELECT id FROM videos WHERE youtube_video_id = %s FOR UPDATE", (youtube_video_id,))
                video = cursor.fetchone()
                if not video:
                    raise NotFoundError(f"Video '{youtube_video_id}' was not found")
                cursor.execute(
                    """
                    SELECT youtube_comment_id, video_id, youtube_parent_comment_id,
                           COALESCE(published_at, source_fetched_at) AS effective_published_at
                    FROM comments
                    WHERE youtube_comment_id = ANY(%s)
                    FOR UPDATE
                    """,
                    ([item.youtube_comment_id for item in page],),
                )
                existing_comments = {
                    str(item["youtube_comment_id"]): dict(item)
                    for item in cursor.fetchall()
                }
                for item in page:
                    previous = existing_comments.get(item.youtube_comment_id)
                    if previous is None:
                        continue
                    if str(previous["video_id"]) != str(video["id"]):
                        raise RepositoryError(
                            f"Comment '{item.youtube_comment_id}' cannot move between videos"
                        )
                    effective_published_at = item.published_at or item.source_fetched_at
                    if (
                        previous.get("youtube_parent_comment_id")
                        != item.youtube_parent_comment_id
                        or previous.get("effective_published_at")
                        != effective_published_at
                    ):
                        # Category changes and a correction to the current maximum
                        # cannot be represented safely as a simple positive delta.
                        absolute_rollup_required = True

            upsert_sql = """
                INSERT INTO comments (
                  youtube_comment_id, video_id, parent_id, youtube_parent_comment_id, youtube_thread_id,
                  author_channel_id, author_display_name, text_display, text_original, like_count,
                  published_at, updated_at, source_fetched_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (youtube_comment_id) DO UPDATE SET
                  video_id = EXCLUDED.video_id, parent_id = EXCLUDED.parent_id,
                  youtube_parent_comment_id = EXCLUDED.youtube_parent_comment_id,
                  youtube_thread_id = EXCLUDED.youtube_thread_id,
                  text_display = EXCLUDED.text_display, text_original = EXCLUDED.text_original,
                  author_channel_id = EXCLUDED.author_channel_id,
                  author_display_name = EXCLUDED.author_display_name,
                  like_count = EXCLUDED.like_count, published_at = EXCLUDED.published_at,
                  updated_at = EXCLUDED.updated_at, source_fetched_at = EXCLUDED.source_fetched_at
            """

            def parameters(comment: CommentRecord, parent_id: Any | None) -> tuple[Any, ...]:
                return (
                    comment.youtube_comment_id,
                    video["id"] if video else None,
                    parent_id,
                    comment.youtube_parent_comment_id,
                    comment.youtube_thread_id,
                    comment.author_channel_id,
                    comment.author_display_name,
                    comment.text_display,
                    comment.text_display,
                    comment.like_count,
                    comment.published_at,
                    comment.updated_at,
                    comment.source_fetched_at,
                )

            top_level = [item for item in page if not item.youtube_parent_comment_id]
            replies = [item for item in page if item.youtube_parent_comment_id]
            if top_level:
                cursor.executemany(upsert_sql, [parameters(item, None) for item in top_level])

            parent_ids = list(
                dict.fromkeys(
                    item.youtube_parent_comment_id
                    for item in replies
                    if item.youtube_parent_comment_id
                )
            )
            parent_map: dict[str, Any] = {}
            if parent_ids:
                cursor.execute(
                    """
                    SELECT youtube_comment_id, id
                    FROM comments
                    WHERE youtube_comment_id = ANY(%s)
                    """,
                    (parent_ids,),
                )
                parent_map = {
                    str(item["youtube_comment_id"]): item["id"]
                    for item in cursor.fetchall()
                }
            if replies:
                cursor.executemany(
                    upsert_sql,
                    [
                        parameters(item, parent_map.get(str(item.youtube_parent_comment_id)))
                        for item in replies
                    ],
                )

            stored_by_youtube_id: dict[str, CommentRecord] = {}
            if page:
                cursor.execute(
                    """
                    SELECT youtube_comment_id, id::text
                    FROM comments
                    WHERE youtube_comment_id = ANY(%s)
                    """,
                    ([item.youtube_comment_id for item in page],),
                )
                database_ids = {
                    str(item["youtube_comment_id"]): str(item["id"])
                    for item in cursor.fetchall()
                }
                stored_by_youtube_id = {
                    item.youtube_comment_id: replace(
                        item, id=database_ids[item.youtube_comment_id]
                    )
                    for item in page
                }

            if video is not None and self.enable_comment_rollup_dual_write:
                cursor.execute(
                    "SELECT video_id FROM video_comment_rollups WHERE video_id = %s FOR UPDATE",
                    (video["id"],),
                )
                rollup_exists = cursor.fetchone() is not None
                new_comments = [
                    item for item in page if item.youtube_comment_id not in existing_comments
                ]
                if absolute_rollup_required or not rollup_exists:
                    cursor.execute(
                        """
                        INSERT INTO video_comment_rollups (
                          video_id, stored_count, top_level_count, reply_count,
                          latest_published_at, updated_at, last_reconciled_at
                        )
                        SELECT
                          %s,
                          count(*)::bigint,
                          count(*) FILTER (WHERE youtube_parent_comment_id IS NULL)::bigint,
                          count(*) FILTER (WHERE youtube_parent_comment_id IS NOT NULL)::bigint,
                          max(COALESCE(published_at, source_fetched_at)),
                          now(), now()
                        FROM comments
                        WHERE video_id = %s
                        ON CONFLICT (video_id) DO UPDATE SET
                          stored_count = EXCLUDED.stored_count,
                          top_level_count = EXCLUDED.top_level_count,
                          reply_count = EXCLUDED.reply_count,
                          latest_published_at = EXCLUDED.latest_published_at,
                          updated_at = EXCLUDED.updated_at,
                          last_reconciled_at = EXCLUDED.last_reconciled_at
                        """,
                        (video["id"], video["id"]),
                    )
                elif new_comments:
                    top_level_delta = sum(
                        1 for item in new_comments if not item.youtube_parent_comment_id
                    )
                    reply_delta = len(new_comments) - top_level_delta
                    effective_times = [
                        item.published_at or item.source_fetched_at
                        for item in new_comments
                        if item.published_at or item.source_fetched_at
                    ]
                    latest_delta = max(effective_times) if effective_times else None
                    cursor.execute(
                        """
                        UPDATE video_comment_rollups
                        SET stored_count = stored_count + %s,
                            top_level_count = top_level_count + %s,
                            reply_count = reply_count + %s,
                            latest_published_at = CASE
                              WHEN %s::timestamptz IS NULL THEN latest_published_at
                              WHEN latest_published_at IS NULL THEN %s::timestamptz
                              ELSE GREATEST(latest_published_at, %s::timestamptz)
                            END,
                            updated_at = now()
                        WHERE video_id = %s
                        """,
                        (
                            len(new_comments),
                            top_level_delta,
                            reply_delta,
                            latest_delta,
                            latest_delta,
                            latest_delta,
                            video["id"],
                        ),
                    )

            if checkpoint is not None and job_id is not None:
                cursor.execute(
                    """
                    UPDATE sync_jobs
                    SET checkpoint = %s, updated_at = now()
                    WHERE id = %s AND state = 'running'
                    """,
                    (Json(checkpoint), job_id),
                )
                if cursor.rowcount != 1:
                    raise RepositoryError(f"Running job '{job_id}' was not found while checkpointing")
                cursor.execute(
                    """
                    INSERT INTO sync_checkpoints (
                      job_id, stage, scope_key, request_hash, page_token, batch_cursor, checkpoint_seq
                    ) VALUES (%s, %s, %s, %s, %s, %s, 1)
                    ON CONFLICT (job_id, stage, scope_key) DO UPDATE SET
                      request_hash = EXCLUDED.request_hash,
                      page_token = EXCLUDED.page_token,
                      batch_cursor = EXCLUDED.batch_cursor,
                      checkpoint_seq = sync_checkpoints.checkpoint_seq + 1,
                      updated_at = now()
                    """,
                    (
                        job_id,
                        str(checkpoint.get("stage", "comments")),
                        str(checkpoint.get("scopeKey", "job")),
                        hashlib.sha256(str(sorted(checkpoint.items())).encode("utf-8")).hexdigest(),
                        checkpoint.get("pageToken"),
                        int(checkpoint.get("batchCursor", 0)),
                    ),
                )

            return [stored_by_youtube_id[item.youtube_comment_id] for item in page]

    def existing_comment_ids(self, youtube_comment_ids: Iterable[str]) -> set[str]:
        comment_ids = list(dict.fromkeys(youtube_comment_ids))
        if not comment_ids:
            return set()
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT youtube_comment_id FROM comments WHERE youtube_comment_id = ANY(%s)", (comment_ids,))
            return {str(row["youtube_comment_id"]) for row in cursor.fetchall()}

    def comment_counts_by_video(self, youtube_video_ids: Iterable[str]) -> dict[str, int]:
        video_ids = list(dict.fromkeys(youtube_video_ids))
        if not video_ids:
            return {}
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT v.youtube_video_id, count(c.id)::integer AS comment_count
                FROM videos v
                LEFT JOIN comments c ON c.video_id = v.id
                WHERE v.youtube_video_id = ANY(%s)
                GROUP BY v.youtube_video_id
                """,
                (video_ids,),
            )
            return {str(row["youtube_video_id"]): int(row["comment_count"]) for row in cursor.fetchall()}

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
                   cm.youtube_thread_id, cm.author_channel_id, cm.author_display_name, cm.text_display, cm.like_count, cm.published_at, cm.updated_at, cm.source_fetched_at
            FROM comments cm JOIN videos v ON v.id = cm.video_id
            WHERE v.youtube_video_id = ANY(%s)
            ORDER BY cm.published_at DESC NULLS LAST, cm.youtube_comment_id
            """,
            (video_ids,),
        )
        return [self._comment(row) for row in cursor.fetchall()]

    def _resolve_source_scope(
        self, cursor: Any, source_id: str, *, owner_id: str | None
    ) -> tuple[SourceRecord, str | None, str]:
        """Resolve a public subscription/source ID to its bounded read scope."""

        source_row = self._select_subscription_source(cursor, source_id, owner_id=owner_id)
        if source_row:
            source = self._source(source_row)
            return source, source.target_id, source_id
        if self._select_subscription(cursor, source_id) is not None:
            raise NotFoundError(f"Source '{source_id}' was not found")
        source_row = self._select_source(cursor, source_id)
        if not source_row:
            raise NotFoundError(f"Source '{source_id}' was not found")
        if owner_id is not None:
            cursor.execute(
                "SELECT 1 FROM collection_sources WHERE id = %s AND owner_id = %s",
                (source_id, owner_id),
            )
            if not cursor.fetchone():
                raise NotFoundError(f"Source '{source_id}' was not found")
        source = self._source(source_row)
        return source, source.target_id, source_id

    def get_scope_data_version(
        self, *, target_id: str | None, source_id: str
    ) -> int:
        """Return the monotonic identity used only for derived cache keys."""

        with self._connection() as connection, connection.cursor() as cursor:
            if target_id:
                cursor.execute(
                    "SELECT data_version FROM collection_targets WHERE id = %s",
                    (target_id,),
                )
            else:
                cursor.execute(
                    "SELECT data_version FROM collection_sources WHERE id = %s AND target_id IS NULL",
                    (source_id,),
                )
            row = cursor.fetchone()
            if not row:
                raise NotFoundError("Collection data scope was not found")
            return int(row["data_version"] or 0)

    def get_owner_explore_generation(self, *, owner_id: str) -> str:
        """Hash subscription ACL/update state and every visible target version."""

        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT md5(COALESCE(string_agg(
                  concat_ws(':', subscription.target_id::text, target.data_version::text,
                            extract(epoch FROM subscription.updated_at)::text),
                  ',' ORDER BY subscription.target_id
                ), '')) AS generation
                FROM collection_subscriptions subscription
                JOIN collection_targets target ON target.id = subscription.target_id
                WHERE subscription.user_id = %s
                """,
                (owner_id,),
            )
            return str(cursor.fetchone()["generation"])

    def list_active_parent_jobs(self, *, owner_id: str) -> list[dict[str, Any]]:
        """Return only user-visible, active coordinator jobs in one query."""

        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT subscription.id::text AS public_source_id, job.*
                FROM collection_subscriptions subscription
                JOIN sync_jobs job ON job.target_id = subscription.target_id
                WHERE subscription.user_id = %s
                  AND job.parent_job_id IS NULL
                  AND job.state NOT IN ('completed', 'completed_with_warnings', 'failed', 'cancelled')
                UNION ALL
                SELECT source.id::text AS public_source_id, job.*
                FROM collection_sources source
                JOIN sync_jobs job ON job.source_id = source.id
                WHERE source.owner_id = %s
                  AND source.target_id IS NULL
                  AND job.target_id IS NULL
                  AND job.parent_job_id IS NULL
                  AND job.state NOT IN ('completed', 'completed_with_warnings', 'failed', 'cancelled')
                ORDER BY created_at DESC
                """,
                (owner_id, owner_id),
            )
            return [
                {
                    "source_id": str(row["public_source_id"]),
                    "target_id": str(row["target_id"]) if row.get("target_id") else None,
                    "job": self._job(row),
                }
                for row in cursor.fetchall()
            ]

    def list_recent_failed_parent_jobs(
        self, *, owner_id: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Return owner-scoped failed coordinator jobs in reverse failure order."""

        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                WITH visible_parent_failures AS MATERIALIZED (
                  SELECT subscription.id::text AS public_source_id,
                         target.id::text AS public_target_id,
                         target.type AS public_source_type,
                         COALESCE(NULLIF(subscription.display_config, '{}'::jsonb), target.config)
                           AS public_source_config,
                         target.canonical_key AS public_canonical_key,
                         job.updated_at AS failed_at,
                         job.*
                  FROM collection_subscriptions subscription
                  JOIN collection_targets target ON target.id = subscription.target_id
                  JOIN sync_jobs job ON job.target_id = target.id
                  WHERE subscription.user_id = %s
                    AND job.updated_at >= subscription.created_at
                    AND job.parent_job_id IS NULL
                    AND job.state = 'failed'

                  UNION ALL

                  SELECT source.id::text AS public_source_id,
                         NULL::text AS public_target_id,
                         source.type AS public_source_type,
                         source.config AS public_source_config,
                         NULL::text AS public_canonical_key,
                         job.updated_at AS failed_at,
                         job.*
                  FROM collection_sources source
                  JOIN sync_jobs job ON job.source_id = source.id
                  WHERE source.owner_id = %s
                    AND source.target_id IS NULL
                    AND job.target_id IS NULL
                    AND job.parent_job_id IS NULL
                    AND job.state = 'failed'
                  ORDER BY failed_at DESC, id DESC
                  LIMIT %s
                ),
                ranked_failed_children AS (
                  SELECT child.parent_job_id,
                         child.pause_reason AS representative_child_pause_reason,
                         child.partial_errors AS representative_child_partial_errors,
                         count(*) OVER (PARTITION BY child.parent_job_id)::integer
                           AS failed_child_count,
                         row_number() OVER (
                           PARTITION BY child.parent_job_id
                           ORDER BY
                             CASE
                               WHEN NULLIF(btrim(child.pause_reason), '') IS NOT NULL
                                 OR jsonb_array_length(COALESCE(child.partial_errors, '[]'::jsonb)) > 0
                               THEN 0 ELSE 1
                             END,
                             child.updated_at DESC,
                             child.id DESC
                         ) AS failure_rank
                  FROM sync_jobs child
                  JOIN visible_parent_failures parent ON parent.id = child.parent_job_id
                  WHERE child.parent_job_id IS NOT NULL
                    AND child.state = 'failed'
                )
                SELECT parent.*,
                       COALESCE(child.failed_child_count, 0)::integer AS failed_child_count,
                       child.representative_child_pause_reason,
                       COALESCE(child.representative_child_partial_errors, '[]'::jsonb)
                         AS representative_child_partial_errors
                FROM visible_parent_failures parent
                LEFT JOIN ranked_failed_children child
                  ON child.parent_job_id = parent.id AND child.failure_rank = 1
                ORDER BY parent.failed_at DESC, parent.id DESC
                """,
                (owner_id, owner_id, limit),
            )
            return [
                {
                    "source_id": str(row["public_source_id"]),
                    "target_id": str(row["public_target_id"])
                    if row.get("public_target_id")
                    else None,
                    "source_type": SourceType(row["public_source_type"]),
                    "source_config": dict(row.get("public_source_config") or {}),
                    "canonical_key": row.get("public_canonical_key"),
                    "failed_at": row["failed_at"],
                    "job": self._job(row),
                    "failed_child_count": int(row.get("failed_child_count") or 0),
                    "representative_child_pause_reason": row.get(
                        "representative_child_pause_reason"
                    ),
                    "representative_child_partial_errors": list(
                        row.get("representative_child_partial_errors") or []
                    ),
                }
                for row in cursor.fetchall()
            ]

    @staticmethod
    def _visible_video_cte(target_id: str | None) -> str:
        if target_id:
            return """
                SELECT membership.video_id, membership.first_seen_at
                FROM collection_target_videos membership
                WHERE membership.target_id = %s
            """
        return """
            SELECT membership.video_id, membership.first_seen_at
            FROM source_videos membership
            WHERE membership.source_id = %s
        """

    def get_source_overview(self, source_id: str, *, owner_id: str | None = None) -> dict[str, Any]:
        """Return exact aggregates and rankings without hydrating comment text."""

        with self._connection() as connection, connection.cursor() as cursor:
            source, target_id, membership_id = self._resolve_source_scope(
                cursor, source_id, owner_id=owner_id
            )
            scope_value = target_id or membership_id
            if target_id:
                cursor.execute(
                    "SELECT data_version, coverage FROM collection_targets WHERE id = %s",
                    (target_id,),
                )
            else:
                cursor.execute(
                    "SELECT data_version, '{}'::jsonb AS coverage FROM collection_sources WHERE id = %s",
                    (membership_id,),
                )
            version_row = cursor.fetchone() or {"data_version": 0, "coverage": {}}
            data_version = int(version_row.get("data_version") or 0)

            if target_id:
                cursor.execute(
                    """
                    SELECT * FROM sync_jobs
                    WHERE target_id = %s AND parent_job_id IS NULL
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (target_id,),
                )
            else:
                cursor.execute(
                    """
                    SELECT * FROM sync_jobs
                    WHERE source_id = %s AND target_id IS NULL AND parent_job_id IS NULL
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (membership_id,),
                )
            latest_job_row = cursor.fetchone()
            latest_job = self._job(latest_job_row) if latest_job_row else None
            if target_id:
                cursor.execute(
                    """
                    SELECT * FROM sync_jobs
                    WHERE target_id = %s AND parent_job_id IS NULL
                      AND state IN ('completed', 'completed_with_warnings', 'failed', 'cancelled')
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (target_id,),
                )
            else:
                cursor.execute(
                    """
                    SELECT * FROM sync_jobs
                    WHERE source_id = %s AND target_id IS NULL AND parent_job_id IS NULL
                      AND state IN ('completed', 'completed_with_warnings', 'failed', 'cancelled')
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (membership_id,),
                )
            latest_terminal_row = cursor.fetchone()
            latest_terminal = self._job(latest_terminal_row) if latest_terminal_row else None

            visible_cte = self._visible_video_cte(target_id)
            if self.enable_comment_rollup_read:
                aggregate_sql = f"""
                    WITH visible_video AS ({visible_cte})
                    SELECT count(*)::bigint AS video_count,
                           COALESCE(sum(rollup.stored_count), 0)::bigint AS comment_count,
                           max(video.published_at) AS latest_video_published_at,
                           max(rollup.latest_published_at) AS latest_comment_published_at
                    FROM visible_video membership
                    JOIN videos video ON video.id = membership.video_id
                    LEFT JOIN video_comment_rollups rollup ON rollup.video_id = video.id
                """
            else:
                aggregate_sql = f"""
                    WITH visible_video AS ({visible_cte}), comment_aggregate AS (
                      SELECT comment.video_id, count(*)::bigint AS stored_count,
                             max(COALESCE(comment.published_at, comment.source_fetched_at)) AS latest_published_at
                      FROM comments comment
                      JOIN visible_video membership ON membership.video_id = comment.video_id
                      GROUP BY comment.video_id
                    )
                    SELECT count(*)::bigint AS video_count,
                           COALESCE(sum(comment_aggregate.stored_count), 0)::bigint AS comment_count,
                           max(video.published_at) AS latest_video_published_at,
                           max(comment_aggregate.latest_published_at) AS latest_comment_published_at
                    FROM visible_video membership
                    JOIN videos video ON video.id = membership.video_id
                    LEFT JOIN comment_aggregate ON comment_aggregate.video_id = video.id
                """
            cursor.execute(aggregate_sql, (scope_value,))
            exact = cursor.fetchone() or {}

            summary_row: dict[str, Any] | None = None
            if self.enable_target_summary_read and target_id:
                cursor.execute(
                    """
                    SELECT run.data_version, run.job_id::text, run.completed_at, run.coverage,
                           result.payload
                    FROM analysis_runs run
                    JOIN analysis_results result ON result.analysis_run_id = run.id
                    WHERE run.target_id = %s AND run.state = 'completed'
                      AND run.pipeline_version = 'deterministic-v2'
                      AND result.result_kind = 'basic_summary'
                      AND result.deleted_at IS NULL
                      AND (result.expires_at IS NULL OR result.expires_at > now())
                    ORDER BY run.data_version DESC, run.completed_at DESC
                    LIMIT 1
                    """,
                    (target_id,),
                )
                summary_row = cursor.fetchone()
            elif self.enable_target_summary_read:
                cursor.execute(
                    """
                    SELECT run.data_version, run.job_id::text, run.completed_at, run.coverage,
                           result.payload
                    FROM analysis_runs run
                    JOIN analysis_results result ON result.analysis_run_id = run.id
                    WHERE run.target_id IS NULL AND run.source_id = %s
                      AND run.state = 'completed'
                      AND run.pipeline_version = 'deterministic-v2'
                      AND result.result_kind = 'basic_summary'
                      AND result.deleted_at IS NULL
                      AND (result.expires_at IS NULL OR result.expires_at > now())
                    ORDER BY run.data_version DESC, run.completed_at DESC
                    LIMIT 1
                    """,
                    (membership_id,),
                )
                summary_row = cursor.fetchone()
            summary_payload = dict(summary_row.get("payload") or {}) if summary_row else {}
            summary_version = int(summary_row.get("data_version") or 0) if summary_row else -1

            if target_id:
                cursor.execute(
                    """
                    SELECT run.state, run.coverage FROM analysis_runs run
                    WHERE run.target_id = %s AND run.data_version = %s
                      AND run.pipeline_version = 'deterministic-v2'
                    ORDER BY run.created_at DESC LIMIT 1
                    """,
                    (target_id, data_version),
                )
            else:
                cursor.execute(
                    """
                    SELECT run.state, run.coverage FROM analysis_runs run
                    WHERE run.target_id IS NULL AND run.source_id = %s
                      AND run.data_version = %s
                      AND run.pipeline_version = 'deterministic-v2'
                    ORDER BY run.created_at DESC LIMIT 1
                    """,
                    (membership_id, data_version),
                )
            current_run = cursor.fetchone()
            run_state = str(current_run["state"]) if current_run else None
            analysis_coverage = dict(
                (current_run or {}).get("coverage")
                or (summary_row or {}).get("coverage")
                or {}
            )
            status = "fresh"
            if summary_version == data_version:
                top_words_status = "fresh"
            elif run_state in {"queued", "running"}:
                top_words_status = "building"
            elif run_state == "failed":
                top_words_status = "failed"
            else:
                top_words_status = "stale" if summary_row else "building"

            partial_data = bool(
                latest_terminal
                and latest_terminal.state
                in {JobState.COMPLETED_WITH_WARNINGS, JobState.FAILED, JobState.CANCELLED}
            )
            if latest_terminal and latest_terminal.state in {JobState.FAILED, JobState.CANCELLED}:
                status = "stale" if summary_row else "failed"
                top_words_status = "stale" if summary_row else "failed"

            generated_at = (
                summary_payload.get("generatedAt")
                or (summary_row.get("completed_at") if summary_row else None)
                or utcnow()
            )
            summary = {
                "videoCount": int(exact.get("video_count") or 0),
                "commentCount": int(exact.get("comment_count") or 0),
                "latestVideoPublishedAt": exact.get("latest_video_published_at"),
                "latestCommentPublishedAt": exact.get("latest_comment_published_at"),
                "topWords": list(summary_payload.get("topWords") or []),
                "generatedAt": generated_at,
                "asOfJobId": (
                    (str(summary_row["job_id"]) if summary_row.get("job_id") else None)
                    if summary_row
                    else (latest_terminal.id if latest_terminal else None)
                ),
                "dataVersion": data_version,
                "status": status,
                "topWordsStatus": top_words_status,
                "partialData": partial_data,
                "coverage": analysis_coverage,
            }

            top_videos: dict[str, list[VideoRecord]] = {}
            metric_columns = {
                "views": "view_count",
                "likes": "like_count",
                "comments": "comment_count",
            }
            for label, column in metric_columns.items():
                cursor.execute(
                    f"""
                    WITH visible_video AS ({visible_cte})
                    SELECT video.id::text, video.youtube_video_id, channel.youtube_channel_id,
                           video.title, video.description, video.published_at, video.duration_seconds,
                           video.privacy_status, video.made_for_kids, video.source_fetched_at,
                           jsonb_build_object(
                             'viewCount', COALESCE(stats.view_count, 0),
                             'likeCount', COALESCE(stats.like_count, 0),
                             'commentCount', COALESCE(stats.comment_count, 0)
                           ) AS statistics
                    FROM visible_video membership
                    JOIN videos video ON video.id = membership.video_id
                    LEFT JOIN channels channel ON channel.id = video.channel_id
                    LEFT JOIN LATERAL (
                      SELECT view_count, like_count, comment_count
                      FROM video_stat_snapshots snapshot
                      WHERE snapshot.video_id = video.id
                      ORDER BY snapshot.fetched_at DESC LIMIT 1
                    ) stats ON TRUE
                    ORDER BY COALESCE(stats.{column}, 0) DESC,
                             COALESCE(video.published_at, video.source_fetched_at, 'epoch'::timestamptz) DESC,
                             video.youtube_video_id DESC
                    LIMIT 6
                    """,
                    (scope_value,),
                )
                top_videos[label] = [self._video(row) for row in cursor.fetchall()]

            return {
                "source": source,
                "latest_job": latest_job,
                "summary": summary,
                "top_videos": top_videos,
            }

    def get_source_videos_page(
        self,
        source_id: str,
        *,
        owner_id: str | None = None,
        cursor: str | None = None,
        limit: int = 60,
    ) -> dict[str, Any]:
        with self._connection() as connection, connection.cursor() as db_cursor:
            _, target_id, membership_id = self._resolve_source_scope(
                db_cursor, source_id, owner_id=owner_id
            )
            filter_hash = source_video_filter_hash()
            decoded = decode_source_video_cursor(
                cursor, scope=source_id, filter_hash=filter_hash
            )
            snapshot_at = decoded.snapshot_at if decoded else utcnow()
            scope_value = target_id or membership_id
            visible_cte = self._visible_video_cte(target_id)
            db_cursor.execute(
                f"""
                WITH visible_video AS ({visible_cte})
                SELECT count(*)::bigint AS total
                FROM visible_video
                WHERE first_seen_at <= %s
                """,
                (scope_value, snapshot_at),
            )
            total = int(db_cursor.fetchone()["total"])

            after_sql = ""
            params: list[Any] = [scope_value, snapshot_at]
            if decoded:
                after_sql = """
                  AND (
                    COALESCE(video.published_at, video.source_fetched_at, 'epoch'::timestamptz),
                    video.youtube_video_id
                  ) < (%s, %s)
                """
                params.extend([decoded.effective_at, decoded.youtube_video_id])
            params.append(limit + 1)
            db_cursor.execute(
                f"""
                WITH visible_video AS ({visible_cte})
                SELECT video.id::text, video.youtube_video_id, channel.youtube_channel_id,
                       video.title, video.description, video.published_at, video.duration_seconds,
                       video.privacy_status, video.made_for_kids,
                       COALESCE(video.source_fetched_at, 'epoch'::timestamptz) AS source_fetched_at,
                       jsonb_build_object(
                         'viewCount', COALESCE(stats.view_count, 0),
                         'likeCount', COALESCE(stats.like_count, 0),
                         'commentCount', COALESCE(stats.comment_count, 0)
                       ) AS statistics
                FROM visible_video membership
                JOIN videos video ON video.id = membership.video_id
                LEFT JOIN channels channel ON channel.id = video.channel_id
                LEFT JOIN LATERAL (
                  SELECT view_count, like_count, comment_count
                  FROM video_stat_snapshots snapshot
                  WHERE snapshot.video_id = video.id
                  ORDER BY snapshot.fetched_at DESC LIMIT 1
                ) stats ON TRUE
                WHERE membership.first_seen_at <= %s
                {after_sql}
                ORDER BY COALESCE(video.published_at, video.source_fetched_at, 'epoch'::timestamptz) DESC,
                         video.youtube_video_id DESC
                LIMIT %s
                """,
                params,
            )
            candidates = [self._video(row) for row in db_cursor.fetchall()]
            has_more = len(candidates) > limit
            videos = candidates[:limit]
            next_cursor = (
                encode_source_video_cursor(
                    videos[-1],
                    snapshot_at=snapshot_at,
                    scope=source_id,
                    filter_hash=filter_hash,
                )
                if has_more and videos
                else None
            )
            return {
                "videos": videos,
                "next_cursor": next_cursor,
                "snapshot_at": snapshot_at,
                "total": total,
            }

    def enqueue_missing_analysis_runs(self, *, limit: int = 100) -> int:
        """Seed current target/source versions without duplicating active work."""

        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                WITH candidate AS (
                  SELECT target.id AS target_id, target.data_version,
                         source.id AS source_id, latest_job.id AS job_id
                  FROM collection_targets target
                  LEFT JOIN LATERAL (
                    SELECT id FROM collection_sources
                    WHERE target_id = target.id ORDER BY created_at LIMIT 1
                  ) source ON TRUE
                  LEFT JOIN LATERAL (
                    SELECT id FROM sync_jobs
                    WHERE target_id = target.id AND parent_job_id IS NULL
                      AND state IN ('completed', 'completed_with_warnings')
                    ORDER BY created_at DESC LIMIT 1
                  ) latest_job ON TRUE
                  WHERE NOT EXISTS (
                    SELECT 1 FROM analysis_runs run
                    WHERE run.target_id = target.id
                      AND run.data_version = target.data_version
                      AND run.pipeline_version = 'deterministic-v2'
                  )
                  ORDER BY target.updated_at DESC
                  LIMIT %s
                )
                INSERT INTO analysis_runs (
                  source_id, target_id, job_id, data_version, state,
                  pipeline_version, policy_gate_version, sample_plan
                )
                SELECT source_id, target_id, job_id, data_version, 'queued',
                       'deterministic-v2', 'server-managed', %s
                FROM candidate
                ON CONFLICT DO NOTHING
                """,
                (
                    limit,
                    Json({"strategy": "per-video-recent", "maxComments": 50_000, "maxPerVideo": 1_000}),
                ),
            )
            inserted = max(0, cursor.rowcount)
            cursor.execute(
                """
                WITH candidate AS (
                  SELECT source.id AS source_id, source.data_version,
                         latest_job.id AS job_id
                  FROM collection_sources source
                  LEFT JOIN LATERAL (
                    SELECT id FROM sync_jobs
                    WHERE source_id = source.id AND target_id IS NULL
                      AND parent_job_id IS NULL
                      AND state IN ('completed', 'completed_with_warnings')
                    ORDER BY created_at DESC LIMIT 1
                  ) latest_job ON TRUE
                  WHERE source.target_id IS NULL
                    AND NOT EXISTS (
                      SELECT 1 FROM analysis_runs run
                      WHERE run.target_id IS NULL AND run.source_id = source.id
                        AND run.data_version = source.data_version
                        AND run.pipeline_version = 'deterministic-v2'
                    )
                  ORDER BY source.updated_at DESC
                  LIMIT %s
                )
                INSERT INTO analysis_runs (
                  source_id, job_id, data_version, state,
                  pipeline_version, policy_gate_version, sample_plan
                )
                SELECT source_id, job_id, data_version, 'queued',
                       'deterministic-v2', 'server-managed', %s
                FROM candidate
                ON CONFLICT DO NOTHING
                """,
                (
                    max(0, limit - inserted),
                    Json({"strategy": "per-video-recent", "maxComments": 50_000, "maxPerVideo": 1_000}),
                ),
            )
            return inserted + max(0, cursor.rowcount)

    def claim_next_analysis_run(
        self, *, worker_id: str, lease_seconds: int = 900
    ) -> dict[str, Any] | None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                WITH candidate AS (
                  SELECT id
                  FROM analysis_runs
                  WHERE (
                    (state = 'queued' AND (resume_at IS NULL OR resume_at <= now()))
                    OR (state = 'running' AND lease_expires_at <= now())
                  )
                  ORDER BY created_at
                  FOR UPDATE SKIP LOCKED
                  LIMIT 1
                )
                UPDATE analysis_runs run
                SET state = 'running', lease_owner = %s,
                    lease_expires_at = now() + (%s * interval '1 second'),
                    started_at = COALESCE(started_at, now()), resume_at = NULL,
                    last_error = NULL
                FROM candidate
                WHERE run.id = candidate.id
                RETURNING run.id::text, run.source_id::text, run.target_id::text,
                          run.job_id::text, run.data_version, run.retry_count,
                          run.sample_plan, run.coverage
                """,
                (worker_id, lease_seconds),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def complete_analysis_run(
        self,
        run_id: str,
        *,
        worker_id: str,
        max_comments: int = 50_000,
        max_per_video: int = 1_000,
    ) -> dict[str, Any]:
        """Build one bounded deterministic summary and atomically publish it."""

        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM analysis_runs
                WHERE id = %s AND state = 'running' AND lease_owner = %s
                FOR UPDATE
                """,
                (run_id, worker_id),
            )
            run = cursor.fetchone()
            if not run:
                raise RepositoryError(f"Analysis run '{run_id}' is no longer leased by this worker")
            target_id = str(run["target_id"]) if run.get("target_id") else None
            source_id = str(run["source_id"]) if run.get("source_id") else None
            if not target_id and not source_id:
                raise RepositoryError(f"Analysis run '{run_id}' has no target or source scope")
            visible_cte = self._visible_video_cte(target_id)
            scope_value = target_id or source_id
            cursor.execute(
                f"""
                WITH visible_video AS ({visible_cte}), comment_aggregate AS (
                  SELECT comment.video_id, count(*)::bigint AS stored_count,
                         max(COALESCE(comment.published_at, comment.source_fetched_at)) AS latest_published_at
                  FROM comments comment
                  JOIN visible_video membership ON membership.video_id = comment.video_id
                  GROUP BY comment.video_id
                )
                SELECT count(*)::bigint AS video_count,
                       COALESCE(sum(comment_aggregate.stored_count), 0)::bigint AS comment_count,
                       max(video.published_at) AS latest_video_published_at,
                       max(comment_aggregate.latest_published_at) AS latest_comment_published_at
                FROM visible_video membership
                JOIN videos video ON video.id = membership.video_id
                LEFT JOIN comment_aggregate ON comment_aggregate.video_id = video.id
                """,
                (scope_value,),
            )
            aggregate = cursor.fetchone() or {}
            video_count = int(aggregate.get("video_count") or 0)
            per_video_limit = min(
                max_per_video,
                max(1, (max_comments + max(1, video_count) - 1) // max(1, video_count)),
            )
            cursor.execute(
                f"""
                WITH visible_video AS ({visible_cte})
                SELECT sample.text_display
                FROM visible_video membership
                JOIN LATERAL (
                  SELECT comment.text_display
                  FROM comments comment
                  WHERE comment.video_id = membership.video_id
                    AND comment.text_display IS NOT NULL
                    AND btrim(comment.text_display) <> ''
                  ORDER BY COALESCE(comment.published_at, comment.source_fetched_at, 'epoch'::timestamptz) DESC,
                           comment.youtube_comment_id DESC
                  LIMIT %s
                ) sample ON TRUE
                ORDER BY membership.video_id
                LIMIT %s
                """,
                (scope_value, per_video_limit, max_comments),
            )
            sampled_texts = [str(row["text_display"]) for row in cursor.fetchall()]
            generated_at = utcnow()
            summary = {
                "videoCount": video_count,
                "commentCount": int(aggregate.get("comment_count") or 0),
                "latestVideoPublishedAt": aggregate.get("latest_video_published_at"),
                "latestCommentPublishedAt": aggregate.get("latest_comment_published_at"),
                "topWords": top_words_from_texts(sampled_texts),
                "generatedAt": generated_at,
            }
            coverage = dict(run.get("coverage") or {})
            coverage.update(
                {
                    "sampledComments": len(sampled_texts),
                    "totalComments": summary["commentCount"],
                    "sampleRatio": (
                        len(sampled_texts) / summary["commentCount"]
                        if summary["commentCount"]
                        else 1.0
                    ),
                }
            )
            cursor.execute(
                """
                INSERT INTO analysis_results (analysis_run_id, result_kind, payload)
                VALUES (%s, 'basic_summary', %s)
                ON CONFLICT (analysis_run_id, result_kind) WHERE deleted_at IS NULL
                DO UPDATE SET payload = EXCLUDED.payload, created_at = now()
                """,
                (run_id, Json(self._json_safe(summary))),
            )
            cursor.execute(
                """
                UPDATE analysis_runs
                SET state = 'completed', completed_at = now(), coverage = %s,
                    lease_owner = NULL, lease_expires_at = NULL, last_error = NULL
                WHERE id = %s AND lease_owner = %s
                """,
                (Json(coverage), run_id, worker_id),
            )
            if cursor.rowcount != 1:
                raise RepositoryError(f"Analysis run '{run_id}' lease was lost")
            return summary

    def fail_analysis_run(
        self,
        run_id: str,
        *,
        worker_id: str,
        error: str,
        max_retries: int = 3,
    ) -> str:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE analysis_runs
                SET retry_count = retry_count + 1,
                    state = CASE WHEN retry_count + 1 >= %s THEN 'failed' ELSE 'queued' END,
                    resume_at = CASE
                      WHEN retry_count + 1 >= %s THEN NULL
                      ELSE now() + (power(2, retry_count) * interval '30 seconds')
                    END,
                    last_error = %s, lease_owner = NULL, lease_expires_at = NULL,
                    completed_at = CASE WHEN retry_count + 1 >= %s THEN now() ELSE NULL END
                WHERE id = %s AND state = 'running' AND lease_owner = %s
                RETURNING state
                """,
                (max_retries, max_retries, error[:1_000], max_retries, run_id, worker_id),
            )
            row = cursor.fetchone()
            return str(row["state"]) if row else "lost"

    def save_analysis_summary(self, source_id: str) -> dict[str, Any]:
        """Compatibility producer: enqueue bounded analysis instead of reading all comments."""

        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT id::text, target_id::text, data_version FROM collection_sources WHERE id = %s",
                (source_id,),
            )
            source = cursor.fetchone()
            if not source:
                raise NotFoundError(f"Source '{source_id}' was not found")
            target_id = str(source["target_id"]) if source.get("target_id") else None
            if target_id:
                cursor.execute("SELECT data_version FROM collection_targets WHERE id = %s", (target_id,))
                version = int(cursor.fetchone()["data_version"])
            else:
                version = int(source.get("data_version") or 0)
            cursor.execute(
                """
                INSERT INTO analysis_runs (
                  source_id, target_id, data_version, state,
                  pipeline_version, policy_gate_version, sample_plan
                )
                SELECT %s, %s, %s, 'queued', 'deterministic-v2',
                       'server-managed', %s
                WHERE NOT EXISTS (
                  SELECT 1 FROM analysis_runs
                  WHERE data_version = %s AND pipeline_version = 'deterministic-v2'
                    AND (
                      (%s::uuid IS NOT NULL AND target_id = %s::uuid)
                      OR (%s::uuid IS NULL AND target_id IS NULL AND source_id = %s::uuid)
                    )
                )
                ON CONFLICT DO NOTHING
                RETURNING id::text
                """,
                (
                    source_id,
                    target_id,
                    version,
                    Json({"strategy": "per-video-recent", "maxComments": 50_000, "maxPerVideo": 1_000}),
                    version,
                    target_id,
                    target_id,
                    target_id,
                    source_id,
                ),
            )
            row = cursor.fetchone()
            return {"queued": bool(row), "dataVersion": version, "analysisRunId": str(row["id"]) if row else None}

    def get_source_results(self, source_id: str, *, owner_id: str | None = None) -> dict[str, Any]:
        """Legacy compatibility response without hydrating comment rows.

        The additive overview/video APIs are preferred, but older clients still
        receive the complete video list and exact summary.  No comment text ever
        crosses the database boundary for this endpoint.
        """

        overview = self.get_source_overview(source_id, owner_id=owner_id)
        with self._connection() as connection, connection.cursor() as cursor:
            source = overview["source"]
            videos = (
                self._target_videos(cursor, source.target_id)
                if source.target_id
                else self._source_videos(cursor, source_id)
            )
            return {
                "source": source,
                "latest_job": overview.get("latest_job"),
                "videos": videos,
                "comments": [],
                "analysis": overview["summary"],
            }

    def get_video_comments(self, video_id: str, *, owner_id: str | None = None) -> dict[str, Any]:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT v.id::text, v.youtube_video_id, c.youtube_channel_id, v.title, v.description, v.published_at,
                       v.duration_seconds, v.privacy_status, v.made_for_kids, v.source_fetched_at,
                       jsonb_build_object('viewCount', COALESCE(stats.view_count, 0), 'likeCount', COALESCE(stats.like_count, 0), 'commentCount', COALESCE(stats.comment_count, 0)) AS statistics
                FROM videos v
                LEFT JOIN channels c ON c.id = v.channel_id
                LEFT JOIN LATERAL (SELECT view_count, like_count, comment_count FROM video_stat_snapshots WHERE video_id = v.id ORDER BY fetched_at DESC LIMIT 1) stats ON TRUE
                WHERE (v.youtube_video_id = %s OR v.id::text = %s)
                  AND (
                    %s::uuid IS NULL
                    OR EXISTS (
                      SELECT 1
                      FROM collection_target_videos membership
                      JOIN collection_subscriptions subscription ON subscription.target_id = membership.target_id
                      WHERE membership.video_id = v.id AND subscription.user_id = %s::uuid
                    )
                  )
                """,
                (video_id, video_id, owner_id, owner_id),
            )
            row = cursor.fetchone()
            if not row:
                raise NotFoundError(f"Video '{video_id}' was not found")
            video = self._video(row)
            cursor.execute(
                """
                SELECT comment.id::text, comment.youtube_comment_id,
                       video.youtube_video_id, comment.youtube_parent_comment_id,
                       comment.youtube_thread_id, comment.author_channel_id,
                       comment.author_display_name, comment.text_display,
                       comment.like_count, comment.published_at, comment.updated_at,
                       comment.source_fetched_at
                FROM comments comment
                JOIN videos video ON video.id = comment.video_id
                WHERE comment.video_id = %s
                ORDER BY COALESCE(comment.published_at, comment.source_fetched_at, 'epoch'::timestamptz) DESC,
                         comment.youtube_comment_id DESC
                LIMIT 100
                """,
                (row["id"],),
            )
            comments = [self._comment(item) for item in cursor.fetchall()]
            cursor.execute(
                """
                SELECT count(*)::bigint AS comment_count,
                       max(COALESCE(published_at, source_fetched_at)) AS latest_comment_published_at
                FROM comments WHERE video_id = %s
                """,
                (row["id"],),
            )
            aggregate = cursor.fetchone()
            summary = {
                "videoCount": 1,
                "commentCount": int(aggregate["comment_count"]),
                "latestVideoPublishedAt": video.published_at,
                "latestCommentPublishedAt": aggregate.get("latest_comment_published_at"),
                "topWords": top_words_from_texts(comment.text_display for comment in comments),
                "generatedAt": utcnow(),
            }
            return {"video": video, "comments": comments, "summary": summary}

    def get_video_comment_threads(
        self, video_id: str, *, owner_id: str | None = None, cursor: str | None = None,
        limit: int = 20, sort: CommentThreadSort = "newest"
    ) -> dict[str, Any]:
        cursor_key = decode_comment_thread_cursor(cursor, sort)
        with self._connection() as connection, connection.cursor() as db_cursor:
            db_cursor.execute(
                """
                SELECT v.id::text, v.youtube_video_id, c.youtube_channel_id, v.title, v.description, v.published_at,
                       v.duration_seconds, v.privacy_status, v.made_for_kids, v.source_fetched_at,
                       jsonb_build_object('viewCount', COALESCE(stats.view_count, 0), 'likeCount', COALESCE(stats.like_count, 0), 'commentCount', COALESCE(stats.comment_count, 0)) AS statistics
                FROM videos v
                LEFT JOIN channels c ON c.id = v.channel_id
                LEFT JOIN LATERAL (SELECT view_count, like_count, comment_count FROM video_stat_snapshots WHERE video_id = v.id ORDER BY fetched_at DESC LIMIT 1) stats ON TRUE
                WHERE (v.youtube_video_id = %s OR v.id::text = %s)
                  AND (
                    %s::uuid IS NULL
                    OR EXISTS (
                      SELECT 1
                      FROM collection_target_videos membership
                      JOIN collection_subscriptions subscription ON subscription.target_id = membership.target_id
                      WHERE membership.video_id = v.id AND subscription.user_id = %s::uuid
                    )
                  )
                """,
                (video_id, video_id, owner_id, owner_id),
            )
            video_row = db_cursor.fetchone()
            if not video_row:
                raise NotFoundError(f"Video '{video_id}' was not found")
            video = self._video(video_row)

            params: list[Any] = [video_row["id"]]
            cursor_filter = ""
            if sort == "oldest":
                order_by = """
                  COALESCE(cm.published_at, cm.source_fetched_at, 'epoch'::timestamptz) ASC,
                  cm.youtube_comment_id ASC
                """
                if cursor_key:
                    cursor_filter = """
                      AND (COALESCE(cm.published_at, cm.source_fetched_at, 'epoch'::timestamptz), cm.youtube_comment_id)
                          > (%s::timestamptz, %s)
                    """
                    params.extend(cursor_key)
            elif sort == "recommended":
                order_by = """
                  COALESCE(cm.like_count, 0) DESC,
                  COALESCE(cm.published_at, cm.source_fetched_at, 'epoch'::timestamptz) DESC,
                  cm.youtube_comment_id DESC
                """
                if cursor_key:
                    cursor_filter = """
                      AND (COALESCE(cm.like_count, 0),
                           COALESCE(cm.published_at, cm.source_fetched_at, 'epoch'::timestamptz),
                           cm.youtube_comment_id)
                          < (%s::bigint, %s::timestamptz, %s)
                    """
                    params.extend(cursor_key)
            else:
                order_by = """
                  COALESCE(cm.published_at, cm.source_fetched_at, 'epoch'::timestamptz) DESC,
                  cm.youtube_comment_id DESC
                """
                if cursor_key:
                    cursor_filter = """
                      AND (COALESCE(cm.published_at, cm.source_fetched_at, 'epoch'::timestamptz), cm.youtube_comment_id)
                          < (%s::timestamptz, %s)
                    """
                    params.extend(cursor_key)
            params.append(limit + 1)
            db_cursor.execute(
                f"""
                SELECT cm.id::text, cm.youtube_comment_id, v.youtube_video_id, cm.youtube_parent_comment_id,
                       cm.youtube_thread_id, cm.author_channel_id, cm.author_display_name, cm.text_display,
                       cm.like_count, cm.published_at, cm.updated_at, cm.source_fetched_at
                FROM comments cm
                JOIN videos v ON v.id = cm.video_id
                WHERE cm.video_id = %s AND cm.youtube_parent_comment_id IS NULL
                {cursor_filter}
                ORDER BY {order_by}
                LIMIT %s
                """,
                tuple(params),
            )
            page = [self._comment(row) for row in db_cursor.fetchall()]
            has_more = len(page) > limit
            page = page[:limit]

            reply_groups: dict[str, list[CommentRecord]] = {item.youtube_comment_id: [] for item in page}
            reply_counts: dict[str, int] = {item.youtube_comment_id: 0 for item in page}
            parent_ids = [item.youtube_comment_id for item in page]
            if parent_ids:
                db_cursor.execute(
                    """
                    WITH ranked_replies AS (
                      SELECT cm.id::text, cm.youtube_comment_id, v.youtube_video_id, cm.youtube_parent_comment_id,
                             cm.youtube_thread_id, cm.author_channel_id, cm.author_display_name, cm.text_display,
                             cm.like_count, cm.published_at, cm.updated_at, cm.source_fetched_at,
                             COUNT(*) OVER (PARTITION BY cm.youtube_parent_comment_id) AS reply_count,
                             ROW_NUMBER() OVER (
                               PARTITION BY cm.youtube_parent_comment_id
                               ORDER BY COALESCE(cm.published_at, cm.source_fetched_at, 'epoch'::timestamptz) ASC,
                                        cm.youtube_comment_id ASC
                             ) AS reply_rank
                      FROM comments cm
                      JOIN videos v ON v.id = cm.video_id
                      WHERE cm.youtube_parent_comment_id = ANY(%s)
                    )
                    SELECT * FROM ranked_replies
                    WHERE reply_rank <= 2
                    ORDER BY youtube_parent_comment_id, reply_rank
                    """,
                    (parent_ids,),
                )
                for reply_row in db_cursor.fetchall():
                    parent_id = str(reply_row["youtube_parent_comment_id"])
                    reply_counts[parent_id] = int(reply_row["reply_count"])
                    reply_groups[parent_id].append(self._comment(reply_row))

            return {
                "video": video,
                "items": [
                    {
                        "comment": comment,
                        "replies_preview": reply_groups[comment.youtube_comment_id],
                        "stored_reply_count": reply_counts[comment.youtube_comment_id],
                    }
                    for comment in page
                ],
                "next_cursor": encode_comment_thread_cursor(page[-1], sort) if has_more and page else None,
            }

    def get_comment_replies(
        self, comment_id: str, *, owner_id: str | None = None, cursor: str | None = None, limit: int = 20
    ) -> dict[str, Any]:
        cursor_key = decode_comment_cursor(cursor)
        with self._connection() as connection, connection.cursor() as db_cursor:
            db_cursor.execute(
                """
                SELECT cm.youtube_comment_id, cm.youtube_parent_comment_id, cm.video_id
                FROM comments cm
                JOIN videos v ON v.id = cm.video_id
                WHERE cm.youtube_comment_id = %s
                  AND (
                    %s::uuid IS NULL
                    OR EXISTS (
                      SELECT 1
                      FROM collection_target_videos membership
                      JOIN collection_subscriptions subscription ON subscription.target_id = membership.target_id
                      WHERE membership.video_id = v.id AND subscription.user_id = %s::uuid
                    )
                  )
                """,
                (comment_id, owner_id, owner_id),
            )
            comment_row = db_cursor.fetchone()
            if not comment_row:
                raise NotFoundError(f"Comment '{comment_id}' was not found")
            root_comment_id = comment_row.get("youtube_parent_comment_id") or comment_row["youtube_comment_id"]

            params: list[Any] = [root_comment_id]
            cursor_filter = ""
            if cursor_key:
                cursor_filter = """
                  AND (COALESCE(cm.published_at, cm.source_fetched_at, 'epoch'::timestamptz), cm.youtube_comment_id)
                      > (%s::timestamptz, %s)
                """
                params.extend(cursor_key)
            params.append(limit + 1)
            db_cursor.execute(
                f"""
                SELECT cm.id::text, cm.youtube_comment_id, v.youtube_video_id, cm.youtube_parent_comment_id,
                       cm.youtube_thread_id, cm.author_channel_id, cm.author_display_name, cm.text_display,
                       cm.like_count, cm.published_at, cm.updated_at, cm.source_fetched_at
                FROM comments cm
                JOIN videos v ON v.id = cm.video_id
                WHERE cm.youtube_parent_comment_id = %s
                {cursor_filter}
                ORDER BY COALESCE(cm.published_at, cm.source_fetched_at, 'epoch'::timestamptz) ASC,
                         cm.youtube_comment_id ASC
                LIMIT %s
                """,
                tuple(params),
            )
            page = [self._comment(row) for row in db_cursor.fetchall()]
            has_more = len(page) > limit
            page = page[:limit]
            return {
                "comments": page,
                "next_cursor": encode_comment_cursor(page[-1]) if has_more and page else None,
            }

    def get_comment_detail(self, comment_id: str, *, owner_id: str | None = None) -> dict[str, Any]:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT cm.id::text, cm.youtube_comment_id, cm.youtube_parent_comment_id, cm.youtube_thread_id,
                       cm.author_channel_id, cm.author_display_name, cm.text_display, cm.like_count,
                       cm.published_at, cm.updated_at, cm.source_fetched_at,
                       v.id::text AS video_db_id, v.youtube_video_id, c.youtube_channel_id, c.title AS channel_title, v.title, v.description,
                       v.published_at AS video_published_at, v.duration_seconds, v.privacy_status, v.made_for_kids,
                       v.source_fetched_at AS video_fetched_at,
                       jsonb_build_object('viewCount', COALESCE(stats.view_count, 0), 'likeCount', COALESCE(stats.like_count, 0), 'commentCount', COALESCE(stats.comment_count, 0)) AS statistics
                FROM comments cm
                JOIN videos v ON v.id = cm.video_id
                LEFT JOIN channels c ON c.id = v.channel_id
                LEFT JOIN LATERAL (SELECT view_count, like_count, comment_count FROM video_stat_snapshots WHERE video_id = v.id ORDER BY fetched_at DESC LIMIT 1) stats ON TRUE
                WHERE cm.youtube_comment_id = %s
                  AND (
                    %s::uuid IS NULL
                    OR EXISTS (
                      SELECT 1
                      FROM collection_target_videos membership
                      JOIN collection_subscriptions subscription ON subscription.target_id = membership.target_id
                      WHERE membership.video_id = v.id AND subscription.user_id = %s::uuid
                    )
                  )
                """, (comment_id, owner_id, owner_id),
            )
            row = cursor.fetchone()
            if not row:
                raise NotFoundError(f"Comment '{comment_id}' was not found")
            comment = self._comment(row)
            video = self._video({**row, "id": row["video_db_id"], "published_at": row.get("video_published_at"), "source_fetched_at": row.get("video_fetched_at")})
            parent_comment: CommentRecord | None = None
            root_comment_id = comment.youtube_comment_id
            if comment.youtube_parent_comment_id:
                cursor.execute(
                    """
                    SELECT parent.id::text, parent.youtube_comment_id, v.youtube_video_id,
                           parent.youtube_parent_comment_id, parent.youtube_thread_id,
                           parent.author_channel_id, parent.author_display_name, parent.text_display,
                           parent.like_count, parent.published_at, parent.updated_at, parent.source_fetched_at
                    FROM comments parent
                    JOIN videos v ON v.id = parent.video_id
                    WHERE parent.youtube_comment_id = %s
                    """,
                    (comment.youtube_parent_comment_id,),
                )
                parent_row = cursor.fetchone()
                if parent_row:
                    parent_comment = self._comment(parent_row)
                    root_comment_id = parent_comment.youtube_comment_id
            # ``youtube_parent_comment_id`` is the canonical YouTube relation
            # for a reply.  Use it instead of relying only on the optional
            # internal ``parent_id`` link so replies stored before their parent
            # are still available in the detail view.
            cursor.execute(
                """
                WITH ranked_replies AS (
                  SELECT cm.id::text, cm.youtube_comment_id, v.youtube_video_id, cm.youtube_parent_comment_id,
                         cm.youtube_thread_id, cm.author_channel_id, cm.author_display_name, cm.text_display,
                         cm.like_count, cm.published_at, cm.updated_at, cm.source_fetched_at,
                         COUNT(*) OVER () AS stored_reply_count,
                         ROW_NUMBER() OVER (
                           ORDER BY COALESCE(cm.published_at, cm.source_fetched_at, 'epoch'::timestamptz) ASC,
                                    cm.youtube_comment_id ASC
                         ) AS reply_rank
                  FROM comments cm
                  JOIN videos v ON v.id = cm.video_id
                  WHERE cm.youtube_parent_comment_id = %s
                    AND (
                      %s::uuid IS NULL
                      OR EXISTS (
                        SELECT 1
                        FROM collection_target_videos membership
                        JOIN collection_subscriptions subscription ON subscription.target_id = membership.target_id
                        WHERE membership.video_id = v.id AND subscription.user_id = %s::uuid
                      )
                    )
                )
                SELECT * FROM ranked_replies
                WHERE reply_rank <= 2 OR youtube_comment_id = %s
                ORDER BY reply_rank
                """,
                (root_comment_id, owner_id, owner_id, comment.youtube_comment_id),
            )
            reply_rows = cursor.fetchall()
            replies = [self._comment(reply) for reply in reply_rows]
            stored_reply_count = int(reply_rows[0]["stored_reply_count"]) if reply_rows else 0
            author_comments: list[dict[str, Any]] = []
            if comment.author_channel_id:
                cursor.execute(
                    """
                    SELECT cm.id::text, cm.youtube_comment_id, cm.youtube_parent_comment_id, cm.youtube_thread_id,
                           cm.author_channel_id, cm.author_display_name, cm.text_display, cm.like_count,
                           cm.published_at, cm.updated_at, cm.source_fetched_at,
                           v.id::text AS video_db_id, v.youtube_video_id, c.youtube_channel_id, c.title AS channel_title, v.title, v.description,
                           v.published_at AS video_published_at, v.duration_seconds, v.privacy_status, v.made_for_kids,
                           v.source_fetched_at AS video_fetched_at,
                           jsonb_build_object('viewCount', COALESCE(stats.view_count, 0), 'likeCount', COALESCE(stats.like_count, 0), 'commentCount', COALESCE(stats.comment_count, 0)) AS statistics
                    FROM comments cm
                    JOIN videos v ON v.id = cm.video_id
                    LEFT JOIN channels c ON c.id = v.channel_id
                    LEFT JOIN LATERAL (SELECT view_count, like_count, comment_count FROM video_stat_snapshots WHERE video_id = v.id ORDER BY fetched_at DESC LIMIT 1) stats ON TRUE
                    WHERE cm.author_channel_id = %s
                      AND cm.youtube_comment_id <> %s
                      -- Direct replies are already rendered in the dedicated
                      -- reply section above; do not list a self-reply twice.
                      AND cm.youtube_parent_comment_id IS DISTINCT FROM %s
                      AND (
                        %s::uuid IS NULL
                        OR EXISTS (
                          SELECT 1
                          FROM collection_target_videos membership
                          JOIN collection_subscriptions subscription ON subscription.target_id = membership.target_id
                          WHERE membership.video_id = v.id AND subscription.user_id = %s::uuid
                        )
                      )
                    ORDER BY cm.published_at DESC NULLS LAST, cm.source_fetched_at DESC
                    LIMIT 50
                    """, (comment.author_channel_id, comment_id, root_comment_id, owner_id, owner_id),
                )
                for related in cursor.fetchall():
                    author_comments.append({
                        "comment": self._comment(related),
                        "video": self._video({**related, "id": related["video_db_id"], "published_at": related.get("video_published_at"), "source_fetched_at": related.get("video_fetched_at")}),
                        "channel_title": related.get("channel_title"),
                    })
            return {
                "comment": comment,
                "video": video,
                "parent_comment": parent_comment,
                "replies": replies,
                "stored_reply_count": stored_reply_count,
                "author_comments": author_comments,
            }

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
                WHERE enabled = TRUE
                  AND next_run_at <= now()
                  AND EXISTS (
                    SELECT 1 FROM collection_subscriptions subscription
                    WHERE subscription.target_id = collection_target_pins.target_id
                      AND subscription.enabled = TRUE
                  )
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

    def list_explore_channels(self, *, owner_id: str | None = None) -> list[dict[str, Any]]:
        """Aggregate each channel once over the caller's distinct visible videos."""

        if owner_id is None:
            visible_cte = """
                SELECT video.id AS video_id, video.channel_id, video.source_fetched_at
                FROM videos video
                WHERE video.channel_id IS NOT NULL
            """
            visible_params: tuple[Any, ...] = ()
        else:
            visible_cte = """
                SELECT membership.video_id, video.channel_id,
                       max(video.source_fetched_at) AS source_fetched_at
                FROM collection_target_videos membership
                JOIN collection_subscriptions subscription
                  ON subscription.target_id = membership.target_id
                 AND subscription.user_id = %s::uuid
                JOIN videos video ON video.id = membership.video_id
                WHERE video.channel_id IS NOT NULL
                GROUP BY membership.video_id, video.channel_id
            """
            visible_params = (owner_id,)

        if self.enable_explore_rollup and self.enable_comment_rollup_read:
            comment_cte = """
                SELECT visible.channel_id,
                       COALESCE(sum(rollup.stored_count), 0)::bigint AS comment_count
                FROM visible_video visible
                LEFT JOIN video_comment_rollups rollup ON rollup.video_id = visible.video_id
                GROUP BY visible.channel_id
            """
        else:
            comment_cte = """
                SELECT visible.channel_id, count(comment.id)::bigint AS comment_count
                FROM visible_video visible
                LEFT JOIN comments comment ON comment.video_id = visible.video_id
                GROUP BY visible.channel_id
            """

        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                WITH visible_video AS ({visible_cte}),
                video_aggregate AS (
                  SELECT channel_id, count(*)::bigint AS video_count,
                         max(source_fetched_at) AS last_fetched_at
                  FROM visible_video GROUP BY channel_id
                ),
                comment_aggregate AS ({comment_cte}),
                latest_video_stats AS (
                  SELECT DISTINCT ON (snapshot.video_id)
                         snapshot.video_id, snapshot.comment_count
                  FROM video_stat_snapshots snapshot
                  JOIN visible_video visible ON visible.video_id = snapshot.video_id
                  ORDER BY snapshot.video_id, snapshot.fetched_at DESC
                ),
                youtube_comment_aggregate AS (
                  SELECT visible.channel_id,
                         COALESCE(sum(COALESCE(stats.comment_count, 0)), 0)::bigint AS youtube_comment_count
                  FROM visible_video visible
                  LEFT JOIN latest_video_stats stats ON stats.video_id = visible.video_id
                  GROUP BY visible.channel_id
                )
                SELECT channel.youtube_channel_id, channel.handle, channel.title,
                       channel.description, channel.thumbnail_url,
                       video_aggregate.video_count,
                       COALESCE(comment_aggregate.comment_count, 0) AS comment_count,
                       COALESCE(youtube_comments.youtube_comment_count, 0) AS youtube_comment_count,
                       channel_stats.subscriber_count, channel_stats.view_count,
                       channel_stats.video_count AS youtube_video_count,
                       channel_stats.hidden_subscriber_count,
                       GREATEST(channel.source_fetched_at, video_aggregate.last_fetched_at) AS last_fetched_at,
                       target.id::text AS target_id,
                       pin.enabled AS pin_enabled, pin.interval_minutes AS pin_interval_minutes,
                       pin.next_run_at AS pin_next_run_at,
                       pin.last_dispatched_at AS pin_last_dispatched_at
                FROM video_aggregate
                JOIN channels channel ON channel.id = video_aggregate.channel_id
                LEFT JOIN comment_aggregate ON comment_aggregate.channel_id = channel.id
                LEFT JOIN youtube_comment_aggregate youtube_comments ON youtube_comments.channel_id = channel.id
                LEFT JOIN LATERAL (
                  SELECT subscriber_count, view_count, video_count, hidden_subscriber_count
                  FROM channel_snapshots snapshot
                  WHERE snapshot.channel_id = channel.id
                    AND (subscriber_count IS NOT NULL OR view_count IS NOT NULL
                         OR video_count IS NOT NULL OR hidden_subscriber_count IS NOT NULL)
                  ORDER BY fetched_at DESC LIMIT 1
                ) channel_stats ON TRUE
                LEFT JOIN LATERAL (
                  SELECT candidate.id
                  FROM collection_targets candidate
                  WHERE candidate.resolved_channel_id = channel.id
                    AND (
                      %s::uuid IS NULL OR EXISTS (
                        SELECT 1 FROM collection_subscriptions subscription
                        WHERE subscription.target_id = candidate.id
                          AND subscription.user_id = %s::uuid
                      )
                    )
                  ORDER BY candidate.updated_at DESC, candidate.id
                  LIMIT 1
                ) target ON TRUE
                LEFT JOIN collection_target_pins pin ON pin.target_id = target.id
                ORDER BY GREATEST(channel.source_fetched_at, video_aggregate.last_fetched_at) DESC NULLS LAST,
                         channel.title, channel.youtube_channel_id
                """,
                (*visible_params, owner_id, owner_id),
            )
            channels: list[dict[str, Any]] = []
            for row in cursor.fetchall():
                pin = None
                if row.get("pin_enabled") is not None:
                    pin = {
                        "target_id": str(row["target_id"]),
                        "enabled": bool(row["pin_enabled"]),
                        "interval_minutes": int(row["pin_interval_minutes"]),
                        "next_run_at": row["pin_next_run_at"],
                        "last_dispatched_at": row["pin_last_dispatched_at"],
                    }
                video_count = int(row["video_count"] or 0)
                youtube_video_count = int(row["youtube_video_count"] or 0)
                comment_count = int(row["comment_count"] or 0)
                youtube_comment_count = int(row["youtube_comment_count"] or 0)
                channels.append(
                    {
                        "youtubeChannelId": row["youtube_channel_id"],
                        "handle": row["handle"],
                        "title": row["title"],
                        "description": row["description"],
                        "thumbnailUrl": row["thumbnail_url"],
                        "subscriberCount": row["subscriber_count"],
                        "viewCount": row["view_count"],
                        "youtubeVideoCount": row["youtube_video_count"],
                        "hiddenSubscriberCount": row["hidden_subscriber_count"],
                        "videoCount": video_count,
                        "commentCount": comment_count,
                        "youtubeCommentCount": youtube_comment_count,
                        "videoCollectionRate": (
                            min(100, round((video_count / youtube_video_count) * 100))
                            if youtube_video_count else 0
                        ),
                        "commentCollectionRate": (
                            min(100, round((comment_count / youtube_comment_count) * 100))
                            if youtube_comment_count else 0
                        ),
                        "lastFetchedAt": row["last_fetched_at"],
                        "targetId": str(row["target_id"]) if row.get("target_id") else None,
                        "pin": pin,
                    }
                )
            return channels

    def list_explore_videos_page(
        self,
        *,
        owner_id: str | None = None,
        channel_id: str | None = None,
        cursor: str | None = None,
        limit: int = 60,
    ) -> dict[str, Any]:
        scope = f"owner:{owner_id}" if owner_id is not None else "public"
        filter_hash = explore_video_filter_hash(channel_id)
        decoded = decode_explore_video_cursor(
            cursor, scope=scope, filter_hash=filter_hash
        )
        snapshot_at = decoded.snapshot_at if decoded else utcnow()

        if owner_id is None:
            visible_cte = """
                SELECT video.id AS video_id,
                       COALESCE(video.source_fetched_at, 'epoch'::timestamptz) AS first_seen_at
                FROM videos video
            """
            visible_params: list[Any] = []
        else:
            visible_cte = """
                SELECT membership.video_id,
                       min(GREATEST(membership.first_seen_at, subscription.created_at)) AS first_seen_at
                FROM collection_target_videos membership
                JOIN collection_subscriptions subscription
                  ON subscription.target_id = membership.target_id
                 AND subscription.user_id = %s::uuid
                GROUP BY membership.video_id
            """
            visible_params = [owner_id]

        channel_filter = ""
        filter_params: list[Any] = []
        if channel_id:
            channel_filter = "AND channel.youtube_channel_id = %s"
            filter_params.append(channel_id)
        cursor_filter = ""
        cursor_params: list[Any] = []
        if decoded:
            cursor_filter = """
              AND (
                COALESCE(video.published_at, video.source_fetched_at, 'epoch'::timestamptz),
                COALESCE(video.source_fetched_at, 'epoch'::timestamptz),
                video.youtube_video_id
              ) < (%s, %s, %s)
            """
            cursor_params.extend(
                [decoded.effective_at, decoded.fetched_at, decoded.youtube_video_id]
            )

        with self._connection() as connection, connection.cursor() as db_cursor:
            db_cursor.execute(
                f"""
                WITH visible_video AS ({visible_cte})
                SELECT count(*)::bigint AS total
                FROM visible_video visible
                JOIN videos video ON video.id = visible.video_id
                LEFT JOIN channels channel ON channel.id = video.channel_id
                WHERE visible.first_seen_at <= %s
                {channel_filter}
                """,
                (*visible_params, snapshot_at, *filter_params),
            )
            total = int(db_cursor.fetchone()["total"])
            db_cursor.execute(
                f"""
                WITH visible_video AS ({visible_cte})
                SELECT video.id::text, video.youtube_video_id, channel.youtube_channel_id,
                       video.title, video.description, video.published_at,
                       video.duration_seconds, video.privacy_status, video.made_for_kids,
                       COALESCE(video.source_fetched_at, 'epoch'::timestamptz) AS source_fetched_at,
                       jsonb_build_object(
                         'viewCount', COALESCE(stats.view_count, 0),
                         'likeCount', COALESCE(stats.like_count, 0),
                         'commentCount', COALESCE(stats.comment_count, 0)
                       ) AS statistics
                FROM visible_video visible
                JOIN videos video ON video.id = visible.video_id
                LEFT JOIN channels channel ON channel.id = video.channel_id
                LEFT JOIN LATERAL (
                  SELECT view_count, like_count, comment_count
                  FROM video_stat_snapshots snapshot
                  WHERE snapshot.video_id = video.id
                  ORDER BY snapshot.fetched_at DESC LIMIT 1
                ) stats ON TRUE
                WHERE visible.first_seen_at <= %s
                {channel_filter}
                {cursor_filter}
                ORDER BY COALESCE(video.published_at, video.source_fetched_at, 'epoch'::timestamptz) DESC,
                         COALESCE(video.source_fetched_at, 'epoch'::timestamptz) DESC,
                         video.youtube_video_id DESC
                LIMIT %s
                """,
                (
                    *visible_params,
                    snapshot_at,
                    *filter_params,
                    *cursor_params,
                    limit + 1,
                ),
            )
            candidates = [self._video(row) for row in db_cursor.fetchall()]
            has_more = len(candidates) > limit
            videos = candidates[:limit]
            next_cursor = (
                encode_explore_video_cursor(
                    videos[-1],
                    snapshot_at=snapshot_at,
                    scope=scope,
                    filter_hash=filter_hash,
                )
                if has_more and videos else None
            )
            return {
                "videos": videos,
                "next_cursor": next_cursor,
                "snapshot_at": snapshot_at,
                "total": total,
            }

    def list_explore(
        self, *, limit: int = 60, offset: int = 0, channel_id: str | None = None, owner_id: str | None = None
    ) -> dict[str, Any]:
        # Compatibility wrapper: keep the old offset contract while reusing the
        # set-based channel aggregate. New clients use the keyset endpoint.
        channels = self.list_explore_channels(owner_id=owner_id)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                WITH visible_video AS (
                  SELECT DISTINCT video.id
                  FROM videos video
                  LEFT JOIN collection_target_videos membership ON membership.video_id = video.id
                  LEFT JOIN collection_subscriptions subscription
                    ON subscription.target_id = membership.target_id
                   AND subscription.user_id = %s::uuid
                  WHERE %s::uuid IS NULL OR subscription.id IS NOT NULL
                )
                SELECT video.id::text, video.youtube_video_id, channel.youtube_channel_id,
                       video.title, video.description, video.published_at,
                       video.duration_seconds, video.privacy_status, video.made_for_kids,
                       COALESCE(video.source_fetched_at, 'epoch'::timestamptz) AS source_fetched_at,
                       jsonb_build_object(
                         'viewCount', COALESCE(stats.view_count, 0),
                         'likeCount', COALESCE(stats.like_count, 0),
                         'commentCount', COALESCE(stats.comment_count, 0)
                       ) AS statistics
                FROM visible_video visible
                JOIN videos video ON video.id = visible.id
                LEFT JOIN channels channel ON channel.id = video.channel_id
                LEFT JOIN LATERAL (
                  SELECT view_count, like_count, comment_count
                  FROM video_stat_snapshots snapshot
                  WHERE snapshot.video_id = video.id
                  ORDER BY snapshot.fetched_at DESC LIMIT 1
                ) stats ON TRUE
                WHERE (%s::text IS NULL OR channel.youtube_channel_id = %s)
                ORDER BY COALESCE(video.published_at, video.source_fetched_at, 'epoch'::timestamptz) DESC,
                         video.source_fetched_at DESC, video.youtube_video_id DESC
                LIMIT %s OFFSET %s
                """,
                (owner_id, owner_id, channel_id, channel_id, limit + 1, offset),
            )
            rows = cursor.fetchall()
            page_rows = rows[:limit]
            return {
                "channels": channels,
                "videos": [self._video(row) for row in page_rows],
                "next_offset": offset + len(page_rows) if len(rows) > limit else None,
            }

    def list_channel_subscriber_history(
        self, *, youtube_channel_id: str, limit: int = 180, owner_id: str | None = None
    ) -> list[dict[str, Any]]:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT snapshot.fetched_at, snapshot.subscriber_count, snapshot.hidden_subscriber_count
                FROM channel_snapshots snapshot
                JOIN channels channel ON channel.id = snapshot.channel_id
                WHERE channel.youtube_channel_id = %s
                  AND (snapshot.subscriber_count IS NOT NULL OR snapshot.hidden_subscriber_count IS NOT NULL)
                  AND (
                    %s::uuid IS NULL
                    OR EXISTS (
                      SELECT 1
                      FROM videos v
                      JOIN collection_target_videos membership ON membership.video_id = v.id
                      JOIN collection_subscriptions subscription ON subscription.target_id = membership.target_id
                      WHERE v.channel_id = channel.id AND subscription.user_id = %s::uuid
                    )
                  )
                ORDER BY snapshot.fetched_at DESC
                LIMIT %s
                """,
                (youtube_channel_id, owner_id, owner_id, limit),
            )
            return [
                {"fetchedAt": row["fetched_at"], "subscriberCount": row["subscriber_count"], "hiddenSubscriberCount": row["hidden_subscriber_count"]}
                for row in reversed(cursor.fetchall())
            ]

    def search_collected(self, *, query: str, limit: int = 20, owner_id: str | None = None, scope: str = "all") -> dict[str, Any]:
        """Search persisted public data with a Jaro-Winkler tolerance layer.

        Search scoring deliberately runs in the application so results are
        consistent for Korean text as well as Latin scripts, without depending on
        a database-specific fuzzy-search extension.
        """

        terms = query.split()

        def escaped_pattern(value: str) -> str:
            return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"

        exact_patterns = [escaped_pattern(term.casefold()) for term in terms]
        normalized_query = normalize_search_text(query)
        short_query = len(normalized_query) == 2
        prefix_pattern = (
            normalized_query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        )
        # ACL is applied inside each candidate query, before this hard cap.
        candidate_limit = min(300, max(100, limit * 10))

        # Scalar LIKE clauses are intentional. PostgreSQL cannot turn
        # ``ILIKE ALL(array_parameter)`` into pg_trgm index conditions, which
        # made a common term scan every visible video's comments. A fixed SQL
        # fragment per term remains injection-safe because only the values are
        # parameters, and is logically equivalent to ILIKE ALL.
        video_indexed_contains = " AND ".join(
            "search_document.document ILIKE %s" for _ in exact_patterns
        ) or "TRUE"
        comment_indexed_contains = " AND ".join(
            "lower(COALESCE(cm.text_display, '')) ILIKE %s" for _ in exact_patterns
        ) or "TRUE"

        with self._connection() as connection, connection.cursor() as cursor:
            video_results: list[dict[str, Any]] = []
            if scope in {"all", "videos"}:
                if short_query:
                    video_cte = ""
                    video_join = ""
                    video_acl = """
                      (%s::uuid IS NULL
                       OR EXISTS (
                         SELECT 1 FROM collection_target_videos membership
                         JOIN collection_subscriptions subscription
                           ON subscription.target_id = membership.target_id
                         WHERE membership.video_id = v.id
                           AND subscription.user_id = %s::uuid
                       ))
                    """
                    video_match = """
                      (v.youtube_video_id ILIKE %s ESCAPE '\\'
                       OR ltrim(COALESCE(c.handle, ''), '@') ILIKE %s ESCAPE '\\'
                       OR COALESCE(v.title, '') ILIKE %s ESCAPE '\\')
                    """
                    video_order = "v.source_fetched_at DESC NULLS LAST"
                    video_limit = "LIMIT %s"
                    video_params: tuple[Any, ...] = (
                        owner_id, owner_id, prefix_pattern, prefix_pattern, prefix_pattern, candidate_limit
                    )
                elif self.enable_search_trigram:
                    # MATERIALIZED is an optimization barrier: build the
                    # pg_trgm bitmap candidates first, then apply ACL and LIMIT.
                    # There must be no LIMIT in this CTE because that could
                    # discard authorized rows behind unauthorized candidates.
                    video_cte = f"""
                    WITH matched_videos AS MATERIALIZED (
                      SELECT search_document.video_id,
                             similarity(search_document.document, %s) AS search_score
                      FROM video_search_documents search_document
                      WHERE (({video_indexed_contains})
                             OR search_document.document %% %s)
                    ),
                    authorized_videos AS MATERIALIZED (
                      SELECT search_candidate.video_id, search_candidate.search_score,
                             v.source_fetched_at
                      FROM matched_videos search_candidate
                      JOIN videos v ON v.id = search_candidate.video_id
                      WHERE (
                        %s::uuid IS NULL
                        OR EXISTS (
                          SELECT 1 FROM collection_target_videos membership
                          JOIN collection_subscriptions subscription
                            ON subscription.target_id = membership.target_id
                          WHERE membership.video_id = search_candidate.video_id
                            AND subscription.user_id = %s::uuid
                        )
                      )
                      ORDER BY search_candidate.search_score DESC,
                               v.source_fetched_at DESC NULLS LAST
                      LIMIT %s
                    )
                    """
                    video_join = "JOIN authorized_videos search_candidate ON search_candidate.video_id = v.id"
                    video_acl = "TRUE"
                    video_match = "TRUE"
                    video_order = "search_candidate.search_score DESC, search_candidate.source_fetched_at DESC NULLS LAST"
                    video_limit = ""
                    video_params = (
                        normalized_query, *exact_patterns, normalized_query,
                        owner_id, owner_id, candidate_limit,
                    )
                else:
                    video_cte = ""
                    video_join = ""
                    video_acl = """
                      (%s::uuid IS NULL
                       OR EXISTS (
                         SELECT 1 FROM collection_target_videos membership
                         JOIN collection_subscriptions subscription
                           ON subscription.target_id = membership.target_id
                         WHERE membership.video_id = v.id
                           AND subscription.user_id = %s::uuid
                       ))
                    """
                    video_match = "lower(concat_ws(' ', v.title, v.description, c.title, c.handle)) ILIKE ALL(%s)"
                    video_order = "v.source_fetched_at DESC NULLS LAST"
                    video_limit = "LIMIT %s"
                    video_params = (owner_id, owner_id, exact_patterns, candidate_limit)
                cursor.execute(
                f"""
                {video_cte}
                SELECT v.id::text, v.youtube_video_id, c.youtube_channel_id, c.title AS channel_title,
                       c.handle AS channel_handle, v.title, v.description, v.published_at,
                       v.duration_seconds, v.privacy_status, v.made_for_kids, v.source_fetched_at,
                       jsonb_build_object('viewCount', COALESCE(stats.view_count, 0), 'likeCount', COALESCE(stats.like_count, 0), 'commentCount', COALESCE(stats.comment_count, 0)) AS statistics
                FROM videos v
                LEFT JOIN channels c ON c.id = v.channel_id
                {video_join}
                LEFT JOIN LATERAL (
                  SELECT view_count, like_count, comment_count FROM video_stat_snapshots
                  WHERE video_id = v.id ORDER BY fetched_at DESC LIMIT 1
                ) stats ON TRUE
                WHERE {video_acl}
                  AND {video_match}
                ORDER BY {video_order}
                {video_limit}
                """,
                video_params,
                )
                for row in cursor.fetchall():
                    if short_query:
                        short_fields = {
                            "id": row.get("youtube_video_id"),
                            "title": row.get("title"),
                            "handle": row.get("channel_handle"),
                        }
                        matched_fields = [
                            name
                            for name, value in short_fields.items()
                            if value
                            and normalize_search_text(str(value)).startswith(normalized_query)
                        ]
                        score = 1.0 if matched_fields else 0.0
                    else:
                        score, matched_fields = rank_text_fields(query, {
                            "id": row.get("youtube_video_id"),
                            "title": row.get("title"), "description": row.get("description"),
                            "channel": row.get("channel_title"), "handle": row.get("channel_handle"),
                        })
                    if matched_fields:
                        video_results.append({"video": self._video(row), "score": score, "matched_fields": matched_fields})

            comment_results: list[dict[str, Any]] = []
            if scope in {"all", "comments"} and not short_query:
                if self.enable_search_trigram:
                    # Keep every search match available until after ACL. This
                    # forces the GIN BitmapOr path without changing per-owner
                    # visibility or the existing ranking/candidate semantics.
                    # Only narrow identifiers/ranking columns cross the first
                    # barrier so a broad term cannot spill full comment rows.
                    comment_cte = f"""
                    WITH matched_comments AS MATERIALIZED (
                      SELECT cm.id AS comment_id, cm.video_id, cm.source_fetched_at,
                             similarity(lower(COALESCE(cm.text_display, '')), %s) AS search_score
                      FROM comments cm
                      WHERE cm.text_display IS NOT NULL
                        AND (({comment_indexed_contains})
                             OR lower(COALESCE(cm.text_display, '')) %% %s)
                    ),
                    authorized_comments AS MATERIALIZED (
                      SELECT search_candidate.comment_id, search_candidate.video_id,
                             search_candidate.source_fetched_at, search_candidate.search_score
                      FROM matched_comments search_candidate
                      WHERE (
                        %s::uuid IS NULL
                        OR EXISTS (
                          SELECT 1 FROM collection_target_videos membership
                          JOIN collection_subscriptions subscription
                            ON subscription.target_id = membership.target_id
                          WHERE membership.video_id = search_candidate.video_id
                            AND subscription.user_id = %s::uuid
                        )
                      )
                      ORDER BY search_candidate.search_score DESC,
                               search_candidate.source_fetched_at DESC NULLS LAST
                      LIMIT %s
                    )
                    """
                    comment_from = """authorized_comments search_candidate
                    JOIN comments cm ON cm.id = search_candidate.comment_id"""
                    comment_acl = "TRUE"
                    comment_match = "TRUE"
                    comment_order = "search_candidate.search_score DESC, search_candidate.source_fetched_at DESC NULLS LAST"
                    comment_limit = ""
                    comment_params: tuple[Any, ...] = (
                        normalized_query, *exact_patterns, normalized_query,
                        owner_id, owner_id, candidate_limit,
                    )
                else:
                    comment_cte = ""
                    comment_from = "comments cm"
                    comment_acl = """
                      (%s::uuid IS NULL
                       OR EXISTS (
                         SELECT 1 FROM collection_target_videos membership
                         JOIN collection_subscriptions subscription
                           ON subscription.target_id = membership.target_id
                         WHERE membership.video_id = v.id
                           AND subscription.user_id = %s::uuid
                       ))
                    """
                    comment_match = "lower(COALESCE(cm.text_display, '')) ILIKE ALL(%s)"
                    comment_order = "cm.source_fetched_at DESC NULLS LAST"
                    comment_limit = "LIMIT %s"
                    comment_params = (owner_id, owner_id, exact_patterns, candidate_limit)
                cursor.execute(
                f"""
                {comment_cte}
                SELECT cm.id::text AS comment_id, cm.youtube_comment_id, cm.youtube_parent_comment_id,
                       cm.youtube_thread_id, cm.author_channel_id, cm.author_display_name, cm.text_display, cm.like_count, cm.published_at AS comment_published_at,
                       cm.updated_at AS comment_updated_at, cm.source_fetched_at AS comment_fetched_at,
                       v.id::text AS video_db_id, v.youtube_video_id, c.youtube_channel_id,
                       c.title AS channel_title, c.handle AS channel_handle, v.title, v.description,
                       v.published_at, v.duration_seconds, v.privacy_status, v.made_for_kids,
                       v.source_fetched_at,
                       jsonb_build_object('viewCount', COALESCE(stats.view_count, 0), 'likeCount', COALESCE(stats.like_count, 0), 'commentCount', COALESCE(stats.comment_count, 0)) AS statistics
                FROM {comment_from}
                JOIN videos v ON v.id = cm.video_id
                LEFT JOIN channels c ON c.id = v.channel_id
                LEFT JOIN LATERAL (
                  SELECT view_count, like_count, comment_count FROM video_stat_snapshots
                  WHERE video_id = v.id ORDER BY fetched_at DESC LIMIT 1
                ) stats ON TRUE
                WHERE {comment_acl}
                  AND cm.text_display IS NOT NULL
                  AND {comment_match}
                ORDER BY {comment_order}
                {comment_limit}
                """,
                comment_params,
                )
                for row in cursor.fetchall():
                    score, matched_fields = rank_text_fields(query, {"comment": row.get("text_display")})
                    if not matched_fields:
                        continue
                    comment = self._comment({
                    "id": row["comment_id"], "youtube_comment_id": row["youtube_comment_id"],
                    "youtube_video_id": row["youtube_video_id"], "youtube_parent_comment_id": row.get("youtube_parent_comment_id"),
                    "youtube_thread_id": row.get("youtube_thread_id"), "author_channel_id": row.get("author_channel_id"),
                    "author_display_name": row.get("author_display_name"), "text_display": row.get("text_display"),
                    "like_count": row.get("like_count"), "published_at": row.get("comment_published_at"),
                    "updated_at": row.get("comment_updated_at"), "source_fetched_at": row.get("comment_fetched_at"),
                    })
                    video = self._video({
                    "id": row["video_db_id"], "youtube_video_id": row["youtube_video_id"],
                    "youtube_channel_id": row.get("youtube_channel_id"), "title": row.get("title"),
                    "description": row.get("description"), "published_at": row.get("published_at"),
                    "duration_seconds": row.get("duration_seconds"), "privacy_status": row.get("privacy_status"),
                    "made_for_kids": row.get("made_for_kids"), "source_fetched_at": row.get("source_fetched_at"),
                    "statistics": row.get("statistics"),
                    })
                    comment_results.append({
                        "comment": comment, "video": video, "channel_title": row.get("channel_title"),
                        "score": score, "matched_fields": matched_fields,
                    })

            video_results.sort(key=lambda item: (item["score"], item["video"].source_fetched_at), reverse=True)
            comment_results.sort(key=lambda item: (item["score"], item["comment"].source_fetched_at), reverse=True)
            return {"videos": video_results[:limit], "comments": comment_results[:limit]}
