"""Resumable absolute backfill and reconciliation for comment rollups.

Run this as a one-off worker command after migration 015 is installed and
comment rollup dual-writing is enabled::

    python -m monitube_worker.rollup_backfill

The command deliberately uses one PostgreSQL session so a session advisory lock
prevents concurrent backfill processes.  Each bounded batch is its own
transaction.  Its durable UUID cursor is advanced only after every video in the
batch has been locked and its absolute aggregate has been upserted.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import logging
import os
import signal
from threading import Event
from time import monotonic
from typing import Any, Callable, Iterator, Mapping, Sequence
from uuid import UUID

import psycopg
from psycopg import errors
from psycopg.rows import dict_row

from monitube_api.settings import Settings


LOGGER = logging.getLogger(__name__)
BACKFILL_NAME = "video_comment_rollups"
_RESUMABLE_STATES = {
    "schema_ready",
    "dual_write_enabled",
    "backfill_running",
    "reconciling",
}


@dataclass(frozen=True, slots=True)
class RollupBackfillConfig:
    """Tunable limits for a low-impact production backfill."""

    batch_size: int = 100
    sleep_seconds: float = 0.10
    lock_timeout_ms: int = 1_000
    max_reconcile_passes: int = 3
    dual_write_enabled: bool = False

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> "RollupBackfillConfig":
        values = environment or os.environ

        def positive_int(name: str, default: int) -> int:
            try:
                value = int(values.get(name, default))
            except (TypeError, ValueError):
                return default
            return value if value > 0 else default

        def nonnegative_float(name: str, default: float) -> float:
            try:
                value = float(values.get(name, default))
            except (TypeError, ValueError):
                return default
            return value if value >= 0 else default

        dual_write = str(values.get("ENABLE_COMMENT_ROLLUP_DUAL_WRITE", "")).strip().lower()
        return cls(
            batch_size=positive_int("ROLLUP_BACKFILL_BATCH_SIZE", 100),
            sleep_seconds=nonnegative_float("ROLLUP_BACKFILL_SLEEP_SECONDS", 0.10),
            lock_timeout_ms=positive_int("ROLLUP_BACKFILL_LOCK_TIMEOUT_MS", 1_000),
            max_reconcile_passes=positive_int("ROLLUP_BACKFILL_MAX_RECONCILE_PASSES", 3),
            dual_write_enabled=dual_write in {"1", "true", "yes", "on"},
        )


@dataclass(frozen=True, slots=True)
class BackfillProgress:
    state: str
    cursor: str | None
    processed: int
    total: int
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class ReconciliationCounts:
    mismatched: int
    missing: int

    @property
    def clean(self) -> bool:
        return self.mismatched == 0 and self.missing == 0


@dataclass(frozen=True, slots=True)
class BackfillResult:
    state: str
    processed: int
    total: int
    mismatched: int | None
    missing: int | None
    elapsed_seconds: float


class ConcurrentBackfillError(RuntimeError):
    """Another rollup backfill owns the database advisory lock."""


class ReconciliationError(RuntimeError):
    """The source rows and derived rollups did not converge."""


class GracefulStop(RuntimeError):
    """Internal control flow used to leave a resumable cursor intact."""


class RollupBackfill:
    """Drive the durable backfill state machine on a PostgreSQL connection."""

    def __init__(
        self,
        connection: Any,
        *,
        config: RollupBackfillConfig,
        should_stop: Callable[[], bool] | None = None,
        sleeper: Callable[[float], bool] | None = None,
    ) -> None:
        self.connection = connection
        self.config = config
        self.should_stop = should_stop or (lambda: False)
        self.sleeper = sleeper or _sleep
        self._phase = "schema_ready"

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        try:
            yield
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def _acquire_advisory_lock(self) -> None:
        with self._transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS acquired",
                (BACKFILL_NAME,),
            )
            row = cursor.fetchone()
            if not row or not bool(_row_value(row, "acquired", 0)):
                raise ConcurrentBackfillError("another video comment rollup backfill is already running")

    def _release_advisory_lock(self) -> None:
        try:
            with self._transaction(), self.connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                    (BACKFILL_NAME,),
                )
        except Exception:  # connection close also releases a session lock
            LOGGER.exception("Could not explicitly release the rollup backfill advisory lock")

    def _require_schema(self) -> None:
        with self._transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                  to_regclass('public.video_comment_rollups') IS NOT NULL AS rollups_ready,
                  to_regclass('public.maintenance_backfills') IS NOT NULL AS progress_ready
                """
            )
            row = cursor.fetchone()
            if not row or not bool(_row_value(row, "rollups_ready", 0)) or not bool(
                _row_value(row, "progress_ready", 1)
            ):
                raise RuntimeError("migration 015 must be applied before the rollup backfill")

    def _ensure_progress(self) -> BackfillProgress:
        with self._transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO maintenance_backfills (name, state, total)
                VALUES (%s, 'schema_ready', (SELECT count(*) FROM videos))
                ON CONFLICT (name) DO NOTHING
                """,
                (BACKFILL_NAME,),
            )
            cursor.execute(
                """
                SELECT state, cursor, processed, COALESCE(total, 0) AS total, last_error
                FROM maintenance_backfills
                WHERE name = %s
                """,
                (BACKFILL_NAME,),
            )
            return _progress_from_row(cursor.fetchone())

    def _load_progress(self) -> BackfillProgress:
        with self._transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT state, cursor, processed, COALESCE(total, 0) AS total, last_error
                FROM maintenance_backfills
                WHERE name = %s
                """,
                (BACKFILL_NAME,),
            )
            return _progress_from_row(cursor.fetchone())

    def _set_state(
        self,
        state: str,
        *,
        reset_cursor: bool = False,
        clear_error: bool = True,
    ) -> BackfillProgress:
        with self._transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE maintenance_backfills
                SET state = %s,
                    cursor = CASE WHEN %s THEN NULL ELSE cursor END,
                    processed = CASE WHEN %s THEN 0 ELSE processed END,
                    total = (SELECT count(*) FROM videos),
                    last_error = CASE WHEN %s THEN NULL ELSE last_error END,
                    started_at = COALESCE(started_at, now()),
                    completed_at = NULL,
                    updated_at = now()
                WHERE name = %s
                RETURNING state, cursor, processed, COALESCE(total, 0) AS total, last_error
                """,
                (state, reset_cursor, reset_cursor, clear_error, BACKFILL_NAME),
            )
            return _progress_from_row(cursor.fetchone())

    def _resume_failed(self, progress: BackfillProgress) -> BackfillProgress:
        previous_phase = (progress.last_error or "").split(" | ", 1)[0]
        if previous_phase not in _RESUMABLE_STATES:
            previous_phase = "backfill_running"
        return self._set_state(previous_phase, clear_error=True)

    def _fetch_video_batch(self, after: str | None) -> list[str]:
        if after is not None:
            after = str(UUID(after))
        with self._transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id::text
                FROM videos
                WHERE (%s::uuid IS NULL OR id > %s::uuid)
                ORDER BY id
                LIMIT %s
                """,
                (after, after, self.config.batch_size),
            )
            return [str(_row_value(row, "id", 0)) for row in cursor.fetchall()]

    def _apply_batch(self, video_ids: Sequence[str], phase: str) -> None:
        """Lock, recompute, upsert, and checkpoint one batch atomically."""

        if not video_ids:
            return
        with self._transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('lock_timeout', %s, true)",
                (f"{self.config.lock_timeout_ms}ms",),
            )
            applied = 0
            for raw_video_id in video_ids:
                video_id = str(UUID(raw_video_id))
                # All live writers and this backfill acquire this row first.  The
                # aggregate cannot race a committed comment-page dual-write.
                cursor.execute(
                    "SELECT id FROM videos WHERE id = %s::uuid FOR UPDATE",
                    (video_id,),
                )
                if cursor.fetchone() is None:  # concurrently deleted; FK cascade is authoritative
                    continue
                cursor.execute(
                    """
                    INSERT INTO video_comment_rollups (
                      video_id, stored_count, top_level_count, reply_count,
                      latest_published_at, updated_at, last_reconciled_at
                    )
                    SELECT
                      %s::uuid,
                      count(*)::bigint,
                      count(*) FILTER (WHERE youtube_parent_comment_id IS NULL)::bigint,
                      count(*) FILTER (WHERE youtube_parent_comment_id IS NOT NULL)::bigint,
                      max(COALESCE(published_at, source_fetched_at)),
                      now(), now()
                    FROM comments
                    WHERE video_id = %s::uuid
                    ON CONFLICT (video_id) DO UPDATE SET
                      stored_count = EXCLUDED.stored_count,
                      top_level_count = EXCLUDED.top_level_count,
                      reply_count = EXCLUDED.reply_count,
                      latest_published_at = EXCLUDED.latest_published_at,
                      updated_at = EXCLUDED.updated_at,
                      last_reconciled_at = EXCLUDED.last_reconciled_at
                    """,
                    (video_id, video_id),
                )
                applied += 1

            cursor.execute(
                """
                UPDATE maintenance_backfills
                SET cursor = %s,
                    processed = processed + %s,
                    updated_at = now(),
                    last_error = NULL
                WHERE name = %s AND state = %s
                """,
                (str(UUID(video_ids[-1])), len(video_ids), BACKFILL_NAME, phase),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"rollup backfill state changed while applying {phase} batch")
            LOGGER.info(
                "Rollup %s batch scanned=%s applied=%s through=%s",
                phase,
                len(video_ids),
                applied,
                video_ids[-1],
            )

    def _count_mismatches(self) -> ReconciliationCounts:
        with self._transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                """
                WITH actual AS (
                  SELECT
                    video.id AS video_id,
                    count(comment.id)::bigint AS stored_count,
                    count(comment.id) FILTER (
                      WHERE comment.youtube_parent_comment_id IS NULL
                    )::bigint AS top_level_count,
                    count(comment.id) FILTER (
                      WHERE comment.youtube_parent_comment_id IS NOT NULL
                    )::bigint AS reply_count,
                    max(COALESCE(comment.published_at, comment.source_fetched_at)) AS latest_published_at
                  FROM videos video
                  LEFT JOIN comments comment ON comment.video_id = video.id
                  GROUP BY video.id
                )
                SELECT
                  count(*) FILTER (WHERE rollup.video_id IS NULL)::bigint AS missing,
                  count(*) FILTER (
                    WHERE rollup.video_id IS NOT NULL AND (
                      rollup.stored_count IS DISTINCT FROM actual.stored_count
                      OR rollup.top_level_count IS DISTINCT FROM actual.top_level_count
                      OR rollup.reply_count IS DISTINCT FROM actual.reply_count
                      OR rollup.latest_published_at IS DISTINCT FROM actual.latest_published_at
                    )
                  )::bigint AS mismatched
                FROM actual
                LEFT JOIN video_comment_rollups rollup ON rollup.video_id = actual.video_id
                """
            )
            row = cursor.fetchone()
            return ReconciliationCounts(
                mismatched=int(_row_value(row, "mismatched", 1) or 0),
                missing=int(_row_value(row, "missing", 0) or 0),
            )

    def _mark_ready(self) -> BackfillProgress:
        # ANALYZE is part of the cutover contract.  The UPDATE repeats the
        # zero-mismatch test in its own statement snapshot, so even a caller bug
        # cannot persist ``ready`` while a missing or stale rollup is visible.
        with self._transaction(), self.connection.cursor() as cursor:
            cursor.execute("ANALYZE video_comment_rollups")
            cursor.execute(
                """
                WITH actual AS (
                  SELECT
                    video.id AS video_id,
                    count(comment.id)::bigint AS stored_count,
                    count(comment.id) FILTER (
                      WHERE comment.youtube_parent_comment_id IS NULL
                    )::bigint AS top_level_count,
                    count(comment.id) FILTER (
                      WHERE comment.youtube_parent_comment_id IS NOT NULL
                    )::bigint AS reply_count,
                    max(COALESCE(comment.published_at, comment.source_fetched_at)) AS latest_published_at
                  FROM videos video
                  LEFT JOIN comments comment ON comment.video_id = video.id
                  GROUP BY video.id
                ), mismatch AS (
                  SELECT 1
                  FROM actual
                  LEFT JOIN video_comment_rollups rollup ON rollup.video_id = actual.video_id
                  WHERE rollup.video_id IS NULL
                     OR rollup.stored_count IS DISTINCT FROM actual.stored_count
                     OR rollup.top_level_count IS DISTINCT FROM actual.top_level_count
                     OR rollup.reply_count IS DISTINCT FROM actual.reply_count
                     OR rollup.latest_published_at IS DISTINCT FROM actual.latest_published_at
                  LIMIT 1
                )
                UPDATE maintenance_backfills
                SET state = 'ready', cursor = NULL,
                    processed = (SELECT count(*) FROM videos),
                    total = (SELECT count(*) FROM videos),
                    last_error = NULL, completed_at = now(), updated_at = now()
                WHERE name = %s AND state = 'reconciling'
                  AND NOT EXISTS (SELECT 1 FROM mismatch)
                RETURNING state, cursor, processed, COALESCE(total, 0) AS total, last_error
                """,
                (BACKFILL_NAME,),
            )
            row = cursor.fetchone()
            if row is None:
                raise ReconciliationError(
                    "rollup backfill was not reconciling or a mismatch appeared before ready"
                )
            return _progress_from_row(row)

    def _record_failure(self, phase: str, error: BaseException) -> None:
        message = f"{phase} | {type(error).__name__}: {error}"[:2_000]
        try:
            with self._transaction(), self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE maintenance_backfills
                    SET state = 'failed', last_error = %s, updated_at = now()
                    WHERE name = %s
                    """,
                    (message, BACKFILL_NAME),
                )
        except Exception:
            LOGGER.exception("Could not persist rollup backfill failure state")

    def _wait(self) -> None:
        if self.config.sleep_seconds and self.sleeper(self.config.sleep_seconds):
            raise GracefulStop

    def run(self) -> BackfillResult:
        started = monotonic()
        final_counts: ReconciliationCounts | None = None
        self._acquire_advisory_lock()
        try:
            self._require_schema()
            progress = self._ensure_progress()
            self._phase = progress.state

            # A previous ready marker is verified again.  If source rows changed
            # through an old writer, the command repairs them before returning.
            if progress.state == "ready":
                final_counts = self._count_mismatches()
                if final_counts.clean:
                    return _result(progress, final_counts, started)
                if not self.config.dual_write_enabled:
                    raise RuntimeError(
                        "ENABLE_COMMENT_ROLLUP_DUAL_WRITE must be enabled before reconciliation"
                    )
                progress = self._set_state("reconciling", reset_cursor=True)

            if progress.state == "failed":
                progress = self._resume_failed(progress)

            if not self.config.dual_write_enabled:
                raise RuntimeError(
                    "ENABLE_COMMENT_ROLLUP_DUAL_WRITE must be enabled before backfill"
                )

            if progress.state == "schema_ready":
                progress = self._set_state("dual_write_enabled")
            if progress.state == "dual_write_enabled":
                progress = self._set_state("backfill_running", reset_cursor=True)

            reconcile_pass = 1
            while not self.should_stop():
                self._phase = progress.state
                if progress.state not in {"backfill_running", "reconciling"}:
                    raise RuntimeError(f"unsupported rollup backfill state: {progress.state}")

                video_ids = self._fetch_video_batch(progress.cursor)
                if video_ids:
                    try:
                        self._apply_batch(video_ids, progress.state)
                    except errors.LockNotAvailable:
                        # The batch transaction and cursor both rolled back.  Retry
                        # the same UUID range after yielding to the collector.
                        LOGGER.warning("Timed out waiting for a video row; retrying the same batch")
                        self._wait()
                        progress = self._load_progress()
                        continue
                    progress = self._load_progress()
                    self._wait()
                    continue

                if progress.state == "backfill_running":
                    progress = self._set_state("reconciling", reset_cursor=True)
                    continue

                final_counts = self._count_mismatches()
                LOGGER.info(
                    "Rollup reconciliation pass=%s mismatched=%s missing=%s",
                    reconcile_pass,
                    final_counts.mismatched,
                    final_counts.missing,
                )
                if final_counts.clean:
                    progress = self._mark_ready()
                    return _result(progress, final_counts, started)
                if reconcile_pass >= self.config.max_reconcile_passes:
                    raise ReconciliationError(
                        "rollups did not converge after "
                        f"{reconcile_pass} pass(es): mismatched={final_counts.mismatched}, "
                        f"missing={final_counts.missing}"
                    )
                reconcile_pass += 1
                progress = self._set_state("reconciling", reset_cursor=True)

            raise GracefulStop
        except GracefulStop:
            progress = self._load_progress()
            LOGGER.info(
                "Rollup backfill stopped safely at state=%s cursor=%s processed=%s/%s",
                progress.state,
                progress.cursor,
                progress.processed,
                progress.total,
            )
            return _result(progress, final_counts, started)
        except Exception as exc:
            self._record_failure(self._phase, exc)
            raise
        finally:
            self._release_advisory_lock()


def _progress_from_row(row: Any) -> BackfillProgress:
    if row is None:
        raise RuntimeError(f"maintenance backfill row '{BACKFILL_NAME}' was not found")
    cursor = _row_value(row, "cursor", 1)
    if cursor is not None:
        cursor = str(UUID(str(cursor)))
    return BackfillProgress(
        state=str(_row_value(row, "state", 0)),
        cursor=cursor,
        processed=int(_row_value(row, "processed", 2) or 0),
        total=int(_row_value(row, "total", 3) or 0),
        last_error=_row_value(row, "last_error", 4),
    )


def _row_value(row: Any, key: str, index: int) -> Any:
    if isinstance(row, Mapping):
        return row.get(key)
    return row[index]


def _result(
    progress: BackfillProgress,
    counts: ReconciliationCounts | None,
    started: float,
) -> BackfillResult:
    return BackfillResult(
        state=progress.state,
        processed=progress.processed,
        total=progress.total,
        mismatched=counts.mismatched if counts else None,
        missing=counts.missing if counts else None,
        elapsed_seconds=max(0.0, monotonic() - started),
    )


_shutdown_requested = Event()


def _request_shutdown(_: int, __: object) -> None:
    _shutdown_requested.set()


def _sleep(seconds: float) -> bool:
    return _shutdown_requested.wait(timeout=seconds)


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    signal.signal(signal.SIGINT, _request_shutdown)
    signal.signal(signal.SIGTERM, _request_shutdown)

    settings = Settings.from_environment()
    if not settings.database_url:
        raise SystemExit("DATABASE_URL is required for the PostgreSQL rollup backfill")
    config = RollupBackfillConfig.from_environment()
    # The URL remains a bound libpq connection value and is never sent through a
    # shell or included in logs, so embedded production credentials stay private.
    with psycopg.connect(
        settings.database_url,
        row_factory=dict_row,
        application_name="monitube-rollup-backfill",
    ) as connection:
        result = RollupBackfill(
            connection,
            config=config,
            should_stop=_shutdown_requested.is_set,
        ).run()
    LOGGER.info(
        "Rollup command finished state=%s processed=%s/%s mismatched=%s missing=%s elapsed=%.1fs",
        result.state,
        result.processed,
        result.total,
        result.mismatched,
        result.missing,
        result.elapsed_seconds,
    )
    if result.state not in {"ready", "backfill_running", "reconciling"}:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
