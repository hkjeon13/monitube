from __future__ import annotations

from dataclasses import replace
from typing import Any, Sequence

import pytest

from monitube_worker.rollup_backfill import (
    BackfillProgress,
    ReconciliationCounts,
    ReconciliationError,
    RollupBackfill,
    RollupBackfillConfig,
)


def _video_id(number: int) -> str:
    return f"00000000-0000-0000-0000-{number:012d}"


class _MemoryBackfill(RollupBackfill):
    def __init__(
        self,
        *,
        videos: Sequence[str],
        progress: BackfillProgress,
        config: RollupBackfillConfig | None = None,
        should_stop=None,
        forced_counts: list[ReconciliationCounts] | None = None,
    ) -> None:
        super().__init__(
            object(),
            config=config
            or RollupBackfillConfig(
                batch_size=2,
                sleep_seconds=0,
                lock_timeout_ms=100,
                dual_write_enabled=True,
            ),
            should_stop=should_stop,
            sleeper=lambda _: False,
        )
        self.videos = sorted(videos)
        self.progress = progress
        self.actual = {video_id: (1, 1, 0, None) for video_id in self.videos}
        self.rollups: dict[str, tuple[int, int, int, object | None]] = {}
        self.applied: list[tuple[str, list[str]]] = []
        self.forced_counts = list(forced_counts or [])
        self.mark_ready_counts: list[ReconciliationCounts] = []
        self.locked = False

    def _acquire_advisory_lock(self) -> None:
        self.locked = True

    def _release_advisory_lock(self) -> None:
        self.locked = False

    def _require_schema(self) -> None:
        pass

    def _ensure_progress(self) -> BackfillProgress:
        return self.progress

    def _load_progress(self) -> BackfillProgress:
        return self.progress

    def _set_state(
        self,
        state: str,
        *,
        reset_cursor: bool = False,
        clear_error: bool = True,
    ) -> BackfillProgress:
        self.progress = replace(
            self.progress,
            state=state,
            cursor=None if reset_cursor else self.progress.cursor,
            processed=0 if reset_cursor else self.progress.processed,
            total=len(self.videos),
            last_error=None if clear_error else self.progress.last_error,
        )
        return self.progress

    def _fetch_video_batch(self, after: str | None) -> list[str]:
        candidates = [video_id for video_id in self.videos if after is None or video_id > after]
        return candidates[: self.config.batch_size]

    def _apply_batch(self, video_ids: Sequence[str], phase: str) -> None:
        batch = list(video_ids)
        self.applied.append((phase, batch))
        for video_id in batch:
            self.rollups[video_id] = self.actual[video_id]
        self.progress = replace(
            self.progress,
            cursor=batch[-1],
            processed=self.progress.processed + len(batch),
        )

    def _count_mismatches(self) -> ReconciliationCounts:
        if self.forced_counts:
            return self.forced_counts.pop(0)
        missing = sum(video_id not in self.rollups for video_id in self.videos)
        mismatched = sum(
            video_id in self.rollups and self.rollups[video_id] != self.actual[video_id]
            for video_id in self.videos
        )
        return ReconciliationCounts(mismatched=mismatched, missing=missing)

    def _mark_ready(self) -> BackfillProgress:
        counts = self._count_mismatches()
        self.mark_ready_counts.append(counts)
        assert counts.clean
        self.progress = replace(
            self.progress,
            state="ready",
            cursor=None,
            processed=len(self.videos),
            total=len(self.videos),
        )
        return self.progress

    def _record_failure(self, phase: str, error: BaseException) -> None:
        self.progress = replace(
            self.progress,
            state="failed",
            last_error=f"{phase} | {type(error).__name__}: {error}",
        )


def test_backfill_resumes_after_durable_uuid_cursor_then_reconciles() -> None:
    videos = [_video_id(number) for number in range(1, 5)]
    backfill = _MemoryBackfill(
        videos=videos,
        progress=BackfillProgress(
            state="backfill_running",
            cursor=videos[1],
            processed=2,
            total=4,
        ),
    )

    result = backfill.run()

    assert result.state == "ready"
    assert result.mismatched == 0
    assert result.missing == 0
    assert backfill.applied[0] == ("backfill_running", videos[2:])
    assert ("reconciling", videos[:2]) in backfill.applied
    assert backfill.mark_ready_counts == [ReconciliationCounts(mismatched=0, missing=0)]
    assert backfill.locked is False


def test_reconciliation_repeats_and_marks_ready_only_after_zero_mismatch() -> None:
    videos = [_video_id(1), _video_id(2)]
    backfill = _MemoryBackfill(
        videos=videos,
        progress=BackfillProgress(
            state="reconciling",
            cursor=None,
            processed=0,
            total=2,
        ),
        forced_counts=[ReconciliationCounts(mismatched=1, missing=1)],
    )

    result = backfill.run()

    assert result.state == "ready"
    assert result.mismatched == 0
    assert result.missing == 0
    assert [phase for phase, _ in backfill.applied].count("reconciling") == 2
    assert backfill.mark_ready_counts[0].clean


def test_nonconvergent_reconciliation_is_failed_and_never_ready() -> None:
    videos = [_video_id(1)]
    backfill = _MemoryBackfill(
        videos=videos,
        progress=BackfillProgress(
            state="reconciling",
            cursor=None,
            processed=0,
            total=1,
        ),
        config=RollupBackfillConfig(
            batch_size=1,
            sleep_seconds=0,
            lock_timeout_ms=100,
            max_reconcile_passes=1,
            dual_write_enabled=True,
        ),
        forced_counts=[ReconciliationCounts(mismatched=1, missing=0)],
    )

    with pytest.raises(ReconciliationError):
        backfill.run()

    assert backfill.progress.state == "failed"
    assert backfill.mark_ready_counts == []
    assert "reconciling | ReconciliationError" in (backfill.progress.last_error or "")


def test_environment_config_bounds_invalid_values_and_allows_zero_sleep() -> None:
    config = RollupBackfillConfig.from_environment(
        {
            "ROLLUP_BACKFILL_BATCH_SIZE": "25",
            "ROLLUP_BACKFILL_SLEEP_SECONDS": "0",
            "ROLLUP_BACKFILL_LOCK_TIMEOUT_MS": "250",
            "ROLLUP_BACKFILL_MAX_RECONCILE_PASSES": "0",
            "ENABLE_COMMENT_ROLLUP_DUAL_WRITE": "yes",
        }
    )

    assert config.batch_size == 25
    assert config.sleep_seconds == 0
    assert config.lock_timeout_ms == 250
    assert config.max_reconcile_passes == 3
    assert config.dual_write_enabled is True


class _RecordingCursor:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.rowcount = 1
        self._next_row: dict[str, Any] | None = None

    def __enter__(self):
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def execute(self, statement: str, params: object = None) -> None:
        normalized = " ".join(statement.split())
        self.statements.append(normalized)
        if "FROM videos WHERE id" in normalized and "FOR UPDATE" in normalized:
            self._next_row = {"id": params[0] if params else None}
        else:
            self._next_row = None

    def fetchone(self):
        row = self._next_row
        self._next_row = None
        return row


class _RecordingConnection:
    def __init__(self) -> None:
        self.db_cursor = _RecordingCursor()
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> _RecordingCursor:
        return self.db_cursor

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def test_batch_locks_video_before_absolute_upsert_and_checkpoints_last() -> None:
    connection = _RecordingConnection()
    backfill = RollupBackfill(
        connection,
        config=RollupBackfillConfig(
            batch_size=1,
            sleep_seconds=0,
            lock_timeout_ms=321,
            dual_write_enabled=True,
        ),
    )

    backfill._apply_batch([_video_id(1)], "backfill_running")

    statements = connection.db_cursor.statements
    lock_position = next(index for index, sql in enumerate(statements) if "FOR UPDATE" in sql)
    rollup_position = next(
        index for index, sql in enumerate(statements) if sql.startswith("INSERT INTO video_comment_rollups")
    )
    checkpoint_position = next(
        index for index, sql in enumerate(statements) if sql.startswith("UPDATE maintenance_backfills")
    )
    assert lock_position < rollup_position < checkpoint_position
    assert connection.commits == 1
    assert connection.rollbacks == 0
