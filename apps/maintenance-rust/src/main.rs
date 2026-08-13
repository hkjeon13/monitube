//! Resumable bounded production maintenance commands.

use monitube_postgres::PoolConfig;
use sqlx::{Acquire, FromRow, PgConnection, PgPool};
use std::{
    collections::HashSet,
    env,
    error::Error,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, Instant},
};
use thiserror::Error;
use uuid::Uuid;

const BACKFILL_NAME: &str = "video_comment_rollups";

#[derive(Debug, Clone)]
struct Config {
    batch_size: i64,
    sleep: Duration,
    lock_timeout_ms: i64,
    max_reconcile_passes: u32,
    dual_write_enabled: bool,
}

impl Config {
    fn from_environment() -> Result<Self, ConfigError> {
        Ok(Self {
            batch_size: parse_i64("ROLLUP_BACKFILL_BATCH_SIZE", 100, 1, 10_000)?,
            sleep: Duration::from_millis(parse_u64(
                "ROLLUP_BACKFILL_SLEEP_MILLIS",
                100,
                0,
                60_000,
            )?),
            lock_timeout_ms: parse_i64("ROLLUP_BACKFILL_LOCK_TIMEOUT_MS", 1_000, 1, 60_000)?,
            max_reconcile_passes: u32::try_from(parse_u64(
                "ROLLUP_BACKFILL_MAX_RECONCILE_PASSES",
                3,
                1,
                20,
            )?)
            .map_err(|_| ConfigError::Invalid("ROLLUP_BACKFILL_MAX_RECONCILE_PASSES"))?,
            dual_write_enabled: enabled("ENABLE_COMMENT_ROLLUP_DUAL_WRITE"),
        })
    }
}

#[derive(Debug, Clone, FromRow)]
struct Progress {
    state: String,
    cursor: Option<String>,
    processed: i64,
    total: i64,
    last_error: Option<String>,
}

#[derive(Debug, Clone, FromRow)]
struct Counts {
    missing: i64,
    mismatched: i64,
}

impl Counts {
    const fn clean(&self) -> bool {
        self.missing == 0 && self.mismatched == 0
    }
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn Error>> {
    monitube_observability::init("monitube-maintenance-rust")?;
    let arguments = env::args().collect::<Vec<_>>();
    let command = arguments.get(1).map_or("rollup-backfill", String::as_str);
    if !matches!(command, "rollup-backfill" | "collection-retry") {
        return Err(ConfigError::UnknownCommand(command.to_owned()).into());
    }
    let database_url = required("DATABASE_URL")?;
    let pool = monitube_postgres::connect(
        &database_url,
        PoolConfig {
            min_connections: 0,
            max_connections: 1,
            acquire_timeout: Duration::from_secs(10),
            connect_timeout: Duration::from_secs(10),
        },
    )
    .await?;
    if command == "collection-retry" {
        let request = RecoveryRequest::parse(&arguments[2..])?;
        run_collection_retry(&pool, &request).await?;
        return Ok(());
    }
    let config = Config::from_environment()?;
    let stopping = Arc::new(AtomicBool::new(false));
    let signal_flag = Arc::clone(&stopping);
    tokio::spawn(async move {
        if tokio::signal::ctrl_c().await.is_ok() {
            signal_flag.store(true, Ordering::Release);
        }
    });
    let mut maintenance = RollupBackfill::new(pool, config, stopping);
    let started = Instant::now();
    let progress = maintenance.run().await?;
    tracing::info!(
        state = %progress.state,
        processed = progress.processed,
        total = progress.total,
        elapsed_seconds = started.elapsed().as_secs_f64(),
        "rollup maintenance finished"
    );
    if !matches!(
        progress.state.as_str(),
        "ready" | "backfill_running" | "reconciling"
    ) {
        return Err(MaintenanceError::UnexpectedState(progress.state).into());
    }
    Ok(())
}

#[derive(Debug)]
struct RecoveryRequest {
    apply: bool,
    job_ids: Vec<Uuid>,
}

impl RecoveryRequest {
    fn parse(arguments: &[String]) -> Result<Self, ConfigError> {
        let mut apply = false;
        let mut job_ids = Vec::new();
        for argument in arguments {
            if argument == "--apply" {
                apply = true;
            } else {
                job_ids.push(
                    Uuid::parse_str(argument)
                        .map_err(|_| ConfigError::InvalidArgument(argument.clone()))?,
                );
            }
        }
        let unique = job_ids.iter().copied().collect::<HashSet<_>>();
        if job_ids.is_empty() || job_ids.len() > 100 || unique.len() != job_ids.len() {
            return Err(ConfigError::InvalidRecoverySet);
        }
        job_ids.sort_unstable();
        Ok(Self { apply, job_ids })
    }
}

#[derive(Debug, FromRow)]
struct RecoveryCandidate {
    id: Uuid,
    parent_job_id: Option<Uuid>,
    state: String,
    pause_reason: Option<String>,
    error_code: Option<String>,
    youtube_video_id: Option<String>,
}

#[allow(clippy::too_many_lines)]
async fn run_collection_retry(
    pool: &PgPool,
    request: &RecoveryRequest,
) -> Result<(), MaintenanceError> {
    let mut transaction = pool.begin().await?;
    let candidates = sqlx::query_as::<_, RecoveryCandidate>(
        r"
        SELECT id, parent_job_id, state::text AS state, pause_reason,
               last_error_code AS error_code,
               checkpoint ->> 'youtubeVideoId' AS youtube_video_id
        FROM sync_jobs
        WHERE id = ANY($1::uuid[])
        ORDER BY id
        FOR UPDATE
        ",
    )
    .bind(&request.job_ids)
    .fetch_all(&mut *transaction)
    .await?;
    if candidates.len() != request.job_ids.len() {
        return Err(MaintenanceError::RecoverySetMismatch {
            requested: request.job_ids.len(),
            found: candidates.len(),
        });
    }
    for candidate in &candidates {
        let known_database_failure = candidate.error_code.as_deref()
            == Some("database_operation_failed")
            || candidate.pause_reason.as_deref() == Some("collection database operation failed");
        if candidate.state != "failed"
            || candidate.parent_job_id.is_none()
            || candidate
                .youtube_video_id
                .as_deref()
                .is_none_or(str::is_empty)
            || !known_database_failure
        {
            return Err(MaintenanceError::UnsafeRecoveryCandidate(candidate.id));
        }
    }
    let parent_ids = candidates
        .iter()
        .filter_map(|candidate| candidate.parent_job_id)
        .collect::<HashSet<_>>()
        .into_iter()
        .collect::<Vec<_>>();
    let (parent_count, eligible_parent_count) = sqlx::query_as::<_, (i64, i64)>(
        r"
        SELECT count(*)::bigint,
               count(*) FILTER (WHERE state IN (
                 'failed', 'waiting_retry', 'waiting_quota', 'queued', 'running'
               ))::bigint
        FROM sync_jobs
        WHERE id = ANY($1::uuid[])
        ",
    )
    .bind(&parent_ids)
    .fetch_one(&mut *transaction)
    .await?;
    let expected_parent_count =
        i64::try_from(parent_ids.len()).map_err(|_| MaintenanceError::UnsafeRecoveryParents)?;
    if parent_count != expected_parent_count || eligible_parent_count != expected_parent_count {
        return Err(MaintenanceError::UnsafeRecoveryParents);
    }
    let unselected_failure_count = sqlx::query_scalar::<_, i64>(
        r"
        SELECT count(*)::bigint
        FROM sync_jobs
        WHERE parent_job_id = ANY($1::uuid[])
          AND state = 'failed'
          AND NOT (id = ANY($2::uuid[]))
        ",
    )
    .bind(&parent_ids)
    .bind(&request.job_ids)
    .fetch_one(&mut *transaction)
    .await?;
    let restorable_parent_ids = sqlx::query_scalar::<_, Uuid>(
        r"
        SELECT parent.id
        FROM sync_jobs AS parent
        WHERE parent.id = ANY($1::uuid[])
          AND parent.state = 'failed'
          AND (
            parent.target_id IS NULL
            OR NOT EXISTS (
              SELECT 1
              FROM sync_jobs AS active
              WHERE active.target_id = parent.target_id
                AND active.id <> parent.id
                AND active.state IN (
                  'queued', 'running', 'waiting_retry', 'waiting_quota'
                )
            )
          )
          AND NOT EXISTS (
            SELECT 1
            FROM sync_jobs AS child
            WHERE child.parent_job_id = parent.id
              AND child.state = 'failed'
              AND NOT (child.id = ANY($2::uuid[]))
          )
        ORDER BY parent.id
        ",
    )
    .bind(&parent_ids)
    .bind(&request.job_ids)
    .fetch_all(&mut *transaction)
    .await?;

    for candidate in &candidates {
        tracing::info!(
            mode = if request.apply { "apply" } else { "dry-run" },
            job_id = %candidate.id,
            parent_job_id = ?candidate.parent_job_id,
            youtube_video_id = ?candidate.youtube_video_id,
            "validated collection recovery candidate"
        );
    }
    tracing::info!(
        selected_children = candidates.len(),
        restorable_parents = restorable_parent_ids.len(),
        unselected_failed_children = unselected_failure_count,
        "validated collection recovery scope"
    );
    if !request.apply {
        transaction.rollback().await?;
        tracing::info!(
            count = candidates.len(),
            "collection recovery dry-run completed"
        );
        return Ok(());
    }

    let updated_children = sqlx::query(
        r"
        UPDATE sync_jobs
        SET state = 'queued', current_stage = 'queued_recovery', pause_reason = NULL,
            quota_bucket = NULL, resume_at = NULL, resume_is_automatic = FALSE,
            retry_count = 0, lease_owner = NULL, lease_expires_at = NULL,
            updated_at = now()
        WHERE id = ANY($1::uuid[]) AND state = 'failed'
        ",
    )
    .bind(&request.job_ids)
    .execute(&mut *transaction)
    .await?;
    if usize::try_from(updated_children.rows_affected()).ok() != Some(candidates.len()) {
        return Err(MaintenanceError::RecoveryStateChanged);
    }
    sqlx::query(
        r"
        UPDATE sync_jobs
        SET state = 'waiting_retry', current_stage = 'waiting_for_video_jobs',
            pause_reason = 'Waiting for recovered video collection jobs',
            resume_at = now(), resume_is_automatic = TRUE, retry_count = 0,
            lease_owner = NULL, lease_expires_at = NULL, updated_at = now()
        WHERE id = ANY($1::uuid[]) AND state = 'failed'
        ",
    )
    .bind(&restorable_parent_ids)
    .execute(&mut *transaction)
    .await?;
    sqlx::query(
        r"
        UPDATE collection_requests
        SET status = 'waiting_retry', updated_at = now()
        WHERE job_id = ANY($1::uuid[]) AND status = 'failed'
        ",
    )
    .bind(&restorable_parent_ids)
    .execute(&mut *transaction)
    .await?;
    transaction.commit().await?;
    tracing::info!(count = candidates.len(), "collection recovery applied");
    Ok(())
}

struct RollupBackfill {
    pool: PgPool,
    config: Config,
    stopping: Arc<AtomicBool>,
    phase: String,
}

impl RollupBackfill {
    fn new(pool: PgPool, config: Config, stopping: Arc<AtomicBool>) -> Self {
        Self {
            pool,
            config,
            stopping,
            phase: "schema_ready".to_owned(),
        }
    }

    async fn run(&mut self) -> Result<Progress, MaintenanceError> {
        let mut connection = self.pool.acquire().await?;
        let acquired =
            sqlx::query_scalar::<_, bool>("SELECT pg_try_advisory_lock(hashtextextended($1, 0))")
                .bind(BACKFILL_NAME)
                .fetch_one(&mut *connection)
                .await?;
        if !acquired {
            return Err(MaintenanceError::Concurrent);
        }
        let result = self.run_locked(&mut connection).await;
        if let Err(error) = &result {
            self.record_failure(&mut connection, error).await;
        }
        if let Err(error) = sqlx::query("SELECT pg_advisory_unlock(hashtextextended($1, 0))")
            .bind(BACKFILL_NAME)
            .execute(&mut *connection)
            .await
        {
            tracing::warn!(%error, "could not explicitly release maintenance advisory lock");
        }
        result
    }

    async fn run_locked(
        &mut self,
        connection: &mut PgConnection,
    ) -> Result<Progress, MaintenanceError> {
        self.require_schema(connection).await?;
        let mut progress = self.ensure_progress(connection).await?;
        self.phase.clone_from(&progress.state);

        if progress.state == "ready" {
            let counts = self.count_mismatches(connection).await?;
            if counts.clean() {
                return Ok(progress);
            }
            self.require_dual_write()?;
            progress = self
                .set_state(connection, "reconciling", true, true)
                .await?;
        }
        if progress.state == "failed" {
            let previous = resumable_phase(progress.last_error.as_deref());
            progress = self.set_state(connection, previous, false, true).await?;
        }
        self.require_dual_write()?;
        if progress.state == "schema_ready" {
            progress = self
                .set_state(connection, "dual_write_enabled", false, true)
                .await?;
        }
        if progress.state == "dual_write_enabled" {
            progress = self
                .set_state(connection, "backfill_running", true, true)
                .await?;
        }

        let mut reconciliation_pass = 1_u32;
        loop {
            if self.stopping.load(Ordering::Acquire) {
                tracing::info!(
                    state = %progress.state,
                    cursor = ?progress.cursor,
                    processed = progress.processed,
                    total = progress.total,
                    "rollup maintenance stopped at a resumable checkpoint"
                );
                return Ok(progress);
            }
            self.phase.clone_from(&progress.state);
            if !matches!(progress.state.as_str(), "backfill_running" | "reconciling") {
                return Err(MaintenanceError::UnexpectedState(progress.state));
            }
            let video_ids = self
                .fetch_batch(connection, progress.cursor.as_deref())
                .await?;
            if !video_ids.is_empty() {
                match self
                    .apply_batch(connection, &video_ids, &progress.state)
                    .await
                {
                    Ok(()) => {}
                    Err(MaintenanceError::Database(error)) if is_lock_timeout(&error) => {
                        tracing::warn!("video row lock timed out; retrying the same batch");
                    }
                    Err(error) => return Err(error),
                }
                progress = self.load_progress(connection).await?;
                self.yield_between_batches().await;
                continue;
            }
            if progress.state == "backfill_running" {
                progress = self
                    .set_state(connection, "reconciling", true, true)
                    .await?;
                continue;
            }
            let counts = self.count_mismatches(connection).await?;
            tracing::info!(
                reconciliation_pass,
                missing = counts.missing,
                mismatched = counts.mismatched,
                "rollup reconciliation completed"
            );
            if counts.clean() {
                return self.mark_ready(connection).await;
            }
            if reconciliation_pass >= self.config.max_reconcile_passes {
                return Err(MaintenanceError::DidNotConverge {
                    passes: reconciliation_pass,
                    missing: counts.missing,
                    mismatched: counts.mismatched,
                });
            }
            reconciliation_pass = reconciliation_pass.saturating_add(1);
            progress = self
                .set_state(connection, "reconciling", true, true)
                .await?;
        }
    }

    fn require_dual_write(&self) -> Result<(), MaintenanceError> {
        if self.config.dual_write_enabled {
            Ok(())
        } else {
            Err(MaintenanceError::DualWriteRequired)
        }
    }

    async fn yield_between_batches(&self) {
        if !self.config.sleep.is_zero() {
            tokio::time::sleep(self.config.sleep).await;
        }
    }

    async fn require_schema(&self, connection: &mut PgConnection) -> Result<(), MaintenanceError> {
        let (rollups, progress) = sqlx::query_as::<_, (bool, bool)>(
            r"
            SELECT to_regclass('public.video_comment_rollups') IS NOT NULL,
                   to_regclass('public.maintenance_backfills') IS NOT NULL
            ",
        )
        .fetch_one(connection)
        .await?;
        if rollups && progress {
            Ok(())
        } else {
            Err(MaintenanceError::MissingSchema)
        }
    }

    async fn ensure_progress(
        &self,
        connection: &mut PgConnection,
    ) -> Result<Progress, MaintenanceError> {
        let mut transaction = connection.begin().await?;
        sqlx::query(
            r"
            INSERT INTO maintenance_backfills (name, state, total)
            VALUES ($1, 'schema_ready', (SELECT count(*) FROM videos))
            ON CONFLICT (name) DO NOTHING
            ",
        )
        .bind(BACKFILL_NAME)
        .execute(&mut *transaction)
        .await?;
        let progress = progress_query()
            .bind(BACKFILL_NAME)
            .fetch_optional(&mut *transaction)
            .await?
            .ok_or(MaintenanceError::MissingProgress)?;
        transaction.commit().await?;
        Ok(progress)
    }

    async fn load_progress(
        &self,
        connection: &mut PgConnection,
    ) -> Result<Progress, MaintenanceError> {
        progress_query()
            .bind(BACKFILL_NAME)
            .fetch_optional(connection)
            .await?
            .ok_or(MaintenanceError::MissingProgress)
    }

    async fn set_state(
        &self,
        connection: &mut PgConnection,
        state: &str,
        reset_cursor: bool,
        clear_error: bool,
    ) -> Result<Progress, MaintenanceError> {
        sqlx::query_as::<_, Progress>(
            r"
            UPDATE maintenance_backfills
            SET state = $1,
                cursor = CASE WHEN $2 THEN NULL ELSE cursor END,
                processed = CASE WHEN $2 THEN 0 ELSE processed END,
                total = (SELECT count(*) FROM videos),
                last_error = CASE WHEN $3 THEN NULL ELSE last_error END,
                started_at = COALESCE(started_at, now()), completed_at = NULL,
                updated_at = now()
            WHERE name = $4
            RETURNING state, cursor, processed, COALESCE(total, 0) AS total, last_error
            ",
        )
        .bind(state)
        .bind(reset_cursor)
        .bind(clear_error)
        .bind(BACKFILL_NAME)
        .fetch_optional(connection)
        .await?
        .ok_or(MaintenanceError::MissingProgress)
    }

    async fn fetch_batch(
        &self,
        connection: &mut PgConnection,
        after: Option<&str>,
    ) -> Result<Vec<Uuid>, MaintenanceError> {
        let cursor = after
            .map(Uuid::parse_str)
            .transpose()
            .map_err(|_| MaintenanceError::InvalidCursor)?;
        sqlx::query_scalar::<_, Uuid>(
            "SELECT id FROM videos WHERE ($1::uuid IS NULL OR id > $1) ORDER BY id LIMIT $2",
        )
        .bind(cursor)
        .bind(self.config.batch_size)
        .fetch_all(connection)
        .await
        .map_err(MaintenanceError::Database)
    }

    async fn apply_batch(
        &self,
        connection: &mut PgConnection,
        video_ids: &[Uuid],
        phase: &str,
    ) -> Result<(), MaintenanceError> {
        let Some(last) = video_ids.last() else {
            return Ok(());
        };
        let mut transaction = connection.begin().await?;
        sqlx::query("SELECT set_config('lock_timeout', $1, true)")
            .bind(format!("{}ms", self.config.lock_timeout_ms))
            .execute(&mut *transaction)
            .await?;
        let mut applied = 0_u64;
        for video_id in video_ids {
            let exists =
                sqlx::query_scalar::<_, Uuid>("SELECT id FROM videos WHERE id = $1 FOR UPDATE")
                    .bind(video_id)
                    .fetch_optional(&mut *transaction)
                    .await?;
            if exists.is_none() {
                continue;
            }
            upsert_rollup(&mut transaction, *video_id).await?;
            applied = applied.saturating_add(1);
        }
        let result = sqlx::query(
            r"
            UPDATE maintenance_backfills
            SET cursor = $1, processed = processed + $2,
                updated_at = now(), last_error = NULL
            WHERE name = $3 AND state = $4
            ",
        )
        .bind(last.to_string())
        .bind(i64::try_from(video_ids.len()).unwrap_or(i64::MAX))
        .bind(BACKFILL_NAME)
        .bind(phase)
        .execute(&mut *transaction)
        .await?;
        if result.rows_affected() != 1 {
            return Err(MaintenanceError::StateChanged);
        }
        transaction.commit().await?;
        tracing::info!(scanned = video_ids.len(), applied, through = %last, %phase,
            "rollup batch committed");
        Ok(())
    }

    async fn count_mismatches(
        &self,
        connection: &mut PgConnection,
    ) -> Result<Counts, MaintenanceError> {
        sqlx::query_as::<_, Counts>(reconciliation_sql())
            .fetch_one(connection)
            .await
            .map_err(MaintenanceError::Database)
    }

    async fn mark_ready(
        &self,
        connection: &mut PgConnection,
    ) -> Result<Progress, MaintenanceError> {
        let mut transaction = connection.begin().await?;
        sqlx::query("ANALYZE video_comment_rollups")
            .execute(&mut *transaction)
            .await?;
        let ready = sqlx::query_as::<_, Progress>(mark_ready_sql())
            .bind(BACKFILL_NAME)
            .fetch_optional(&mut *transaction)
            .await?
            .ok_or(MaintenanceError::ReconciliationChanged)?;
        transaction.commit().await?;
        Ok(ready)
    }

    async fn record_failure(&self, connection: &mut PgConnection, error: &MaintenanceError) {
        let detail = format!("{} | {}", self.phase, error);
        let detail = detail.chars().take(2_000).collect::<String>();
        if let Err(record_error) = sqlx::query(
            r"
            UPDATE maintenance_backfills
            SET state = 'failed', last_error = $1, updated_at = now()
            WHERE name = $2
            ",
        )
        .bind(detail)
        .bind(BACKFILL_NAME)
        .execute(connection)
        .await
        {
            tracing::error!(%record_error, "could not persist maintenance failure state");
        }
    }
}

async fn upsert_rollup(
    transaction: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    video_id: Uuid,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        r"
        INSERT INTO video_comment_rollups (
          video_id, stored_count, top_level_count, reply_count,
          latest_published_at, updated_at, last_reconciled_at
        )
        SELECT $1, count(*)::bigint,
               count(*) FILTER (WHERE youtube_parent_comment_id IS NULL)::bigint,
               count(*) FILTER (WHERE youtube_parent_comment_id IS NOT NULL)::bigint,
               max(COALESCE(published_at, source_fetched_at)), now(), now()
        FROM comments WHERE video_id = $1
        ON CONFLICT (video_id) DO UPDATE
        SET stored_count = EXCLUDED.stored_count,
            top_level_count = EXCLUDED.top_level_count,
            reply_count = EXCLUDED.reply_count,
            latest_published_at = EXCLUDED.latest_published_at,
            updated_at = EXCLUDED.updated_at,
            last_reconciled_at = EXCLUDED.last_reconciled_at
        ",
    )
    .bind(video_id)
    .execute(&mut **transaction)
    .await?;
    Ok(())
}

fn progress_query<'query>()
-> sqlx::query::QueryAs<'query, sqlx::Postgres, Progress, sqlx::postgres::PgArguments> {
    sqlx::query_as(
        r"
        SELECT state, cursor, processed, COALESCE(total, 0) AS total, last_error
        FROM maintenance_backfills WHERE name = $1
        ",
    )
}

fn reconciliation_sql() -> &'static str {
    r"
    WITH actual AS (
      SELECT video.id AS video_id, count(comment.id)::bigint AS stored_count,
             count(comment.id) FILTER (
               WHERE comment.youtube_parent_comment_id IS NULL
             )::bigint AS top_level_count,
             count(comment.id) FILTER (
               WHERE comment.youtube_parent_comment_id IS NOT NULL
             )::bigint AS reply_count,
             max(COALESCE(comment.published_at, comment.source_fetched_at))
               AS latest_published_at
      FROM videos AS video
      LEFT JOIN comments AS comment ON comment.video_id = video.id
      GROUP BY video.id
    )
    SELECT count(*) FILTER (WHERE rollup.video_id IS NULL)::bigint AS missing,
           count(*) FILTER (
             WHERE rollup.video_id IS NOT NULL AND (
               rollup.stored_count IS DISTINCT FROM actual.stored_count
               OR rollup.top_level_count IS DISTINCT FROM actual.top_level_count
               OR rollup.reply_count IS DISTINCT FROM actual.reply_count
               OR rollup.latest_published_at IS DISTINCT FROM actual.latest_published_at
             )
           )::bigint AS mismatched
    FROM actual
    LEFT JOIN video_comment_rollups AS rollup ON rollup.video_id = actual.video_id
    "
}

fn mark_ready_sql() -> &'static str {
    r"
    WITH actual AS (
      SELECT video.id AS video_id, count(comment.id)::bigint AS stored_count,
             count(comment.id) FILTER (
               WHERE comment.youtube_parent_comment_id IS NULL
             )::bigint AS top_level_count,
             count(comment.id) FILTER (
               WHERE comment.youtube_parent_comment_id IS NOT NULL
             )::bigint AS reply_count,
             max(COALESCE(comment.published_at, comment.source_fetched_at))
               AS latest_published_at
      FROM videos AS video
      LEFT JOIN comments AS comment ON comment.video_id = video.id
      GROUP BY video.id
    ), mismatch AS (
      SELECT 1 FROM actual
      LEFT JOIN video_comment_rollups AS rollup ON rollup.video_id = actual.video_id
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
        total = (SELECT count(*) FROM videos), last_error = NULL,
        completed_at = now(), updated_at = now()
    WHERE name = $1 AND state = 'reconciling'
      AND NOT EXISTS (SELECT 1 FROM mismatch)
    RETURNING state, cursor, processed, COALESCE(total, 0) AS total, last_error
    "
}

fn resumable_phase(last_error: Option<&str>) -> &'static str {
    match last_error.and_then(|value| value.split(" | ").next()) {
        Some("schema_ready") => "schema_ready",
        Some("dual_write_enabled") => "dual_write_enabled",
        Some("reconciling") => "reconciling",
        _ => "backfill_running",
    }
}

fn is_lock_timeout(error: &sqlx::Error) -> bool {
    error
        .as_database_error()
        .and_then(sqlx::error::DatabaseError::code)
        .is_some_and(|code| code == "55P03")
}

fn optional(name: &'static str) -> Option<String> {
    env::var(name)
        .ok()
        .map(|value| value.trim().to_owned())
        .filter(|value| !value.is_empty())
}

fn required(name: &'static str) -> Result<String, ConfigError> {
    optional(name).ok_or(ConfigError::Missing(name))
}

fn enabled(name: &'static str) -> bool {
    optional(name).is_some_and(|value| {
        matches!(
            value.to_ascii_lowercase().as_str(),
            "1" | "true" | "yes" | "on"
        )
    })
}

fn parse_i64(
    name: &'static str,
    default: i64,
    minimum: i64,
    maximum: i64,
) -> Result<i64, ConfigError> {
    let value = optional(name).map_or(Ok(default), |value| {
        value.parse().map_err(|_| ConfigError::Invalid(name))
    })?;
    if (minimum..=maximum).contains(&value) {
        Ok(value)
    } else {
        Err(ConfigError::Invalid(name))
    }
}

fn parse_u64(
    name: &'static str,
    default: u64,
    minimum: u64,
    maximum: u64,
) -> Result<u64, ConfigError> {
    let value = optional(name).map_or(Ok(default), |value| {
        value.parse().map_err(|_| ConfigError::Invalid(name))
    })?;
    if (minimum..=maximum).contains(&value) {
        Ok(value)
    } else {
        Err(ConfigError::Invalid(name))
    }
}

#[derive(Debug, Error)]
enum MaintenanceError {
    #[error("maintenance database operation failed")]
    Database(#[from] sqlx::Error),
    #[error("another rollup backfill owns the advisory lock")]
    Concurrent,
    #[error("migration 015 must be applied before rollup maintenance")]
    MissingSchema,
    #[error("rollup maintenance progress row is missing")]
    MissingProgress,
    #[error("ENABLE_COMMENT_ROLLUP_DUAL_WRITE must be enabled")]
    DualWriteRequired,
    #[error("rollup cursor is invalid")]
    InvalidCursor,
    #[error("rollup maintenance state changed during a batch")]
    StateChanged,
    #[error("rollup mismatch appeared before the ready marker")]
    ReconciliationChanged,
    #[error(
        "rollups did not converge after {passes} passes: missing={missing}, mismatched={mismatched}"
    )]
    DidNotConverge {
        passes: u32,
        missing: i64,
        mismatched: i64,
    },
    #[error("unsupported rollup maintenance state: {0}")]
    UnexpectedState(String),
    #[error("collection recovery set mismatch: requested={requested}, found={found}")]
    RecoverySetMismatch { requested: usize, found: usize },
    #[error("collection recovery candidate {0} is not a known failed child database job")]
    UnsafeRecoveryCandidate(Uuid),
    #[error("collection recovery parent set is missing or already terminal")]
    UnsafeRecoveryParents,
    #[error("collection recovery state changed while applying")]
    RecoveryStateChanged,
}

#[derive(Debug, Error)]
enum ConfigError {
    #[error("missing required environment variable {0}")]
    Missing(&'static str),
    #[error("environment variable {0} is invalid")]
    Invalid(&'static str),
    #[error("unknown maintenance command {0}")]
    UnknownCommand(String),
    #[error("invalid maintenance argument {0}")]
    InvalidArgument(String),
    #[error("collection-retry requires 1 to 100 unique child job UUIDs")]
    InvalidRecoverySet,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn failed_state_resumes_only_known_phase() {
        assert_eq!(resumable_phase(Some("reconciling | error")), "reconciling");
        assert_eq!(resumable_phase(Some("unknown | error")), "backfill_running");
    }

    #[test]
    fn boolean_flags_are_fail_closed() {
        assert!(!matches!("TRUE-ish".to_ascii_lowercase().as_str(), "true"));
    }

    #[test]
    fn collection_retry_is_dry_run_by_default() -> Result<(), ConfigError> {
        let id = Uuid::new_v4();
        let request = RecoveryRequest::parse(&[id.to_string()])?;
        assert!(!request.apply);
        assert_eq!(request.job_ids, vec![id]);
        Ok(())
    }

    #[test]
    fn collection_retry_rejects_duplicate_ids() {
        let id = Uuid::new_v4().to_string();
        assert!(RecoveryRequest::parse(&[id.clone(), id]).is_err());
    }
}
