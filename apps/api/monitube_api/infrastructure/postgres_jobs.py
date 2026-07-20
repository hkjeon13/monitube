"""PostgreSQL target coordination, jobs, leases, and checkpoints."""

import hashlib
from typing import Any, Iterable

from psycopg.types.json import Json

from ..collection_policy import (
    coverage_satisfies,
    desired_coverage,
    job_coverage,
    merge_collection_config,
)
from ..domain import (
    CollectionRequestRecord,
    CollectionSubmission,
    CollectionSubscriptionRecord,
    CollectionTargetRecord,
    JobRecord,
    JobState,
    SourceRecord,
    SourceType,
    new_id,
)
from ..repositories import (
    InvalidStateTransitionError,
    NotFoundError,
    RepositoryError,
    _ALLOWED_TRANSITIONS,
)


class PostgresJobMixin:
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

    _desired_coverage = staticmethod(desired_coverage)
    _merge_config = staticmethod(merge_collection_config)
    _coverage_satisfies = staticmethod(coverage_satisfies)
    _job_coverage = staticmethod(job_coverage)

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
