//! Lease-fenced `PostgreSQL` coordination for the Rust collection worker.

#![allow(clippy::missing_errors_doc)]

use chrono::{DateTime, Utc};
use serde_json::{Value, json};
use sqlx::{FromRow, PgPool};
use thiserror::Error;
use uuid::Uuid;

#[derive(Debug, Clone, FromRow)]
pub struct ClaimedJob {
    pub id: Uuid,
    pub source_id: Uuid,
    pub target_id: Option<Uuid>,
    pub parent_job_id: Option<Uuid>,
    pub runtime_config_id: Uuid,
    pub current_stage: String,
    pub include_comments: bool,
    pub max_videos: Option<i32>,
    pub max_comments_per_video: Option<i32>,
    pub checkpoint: Value,
    pub partial_errors: Value,
    pub retry_count: i32,
    pub lease_owner: String,
    pub lease_expires_at: DateTime<Utc>,
}

#[derive(Debug, Clone, Copy)]
pub struct FailureMetadata<'a> {
    pub code: &'a str,
    pub provider: Option<&'a str>,
    pub operation: Option<&'a str>,
    pub retryable: bool,
    pub http_status: Option<u16>,
}

#[derive(Debug, Clone, FromRow)]
pub struct CollectionSource {
    pub id: Uuid,
    pub source_type: String,
    pub config: Value,
    pub target_id: Option<Uuid>,
    pub coverage: Value,
}

#[derive(Debug, Clone, FromRow)]
pub struct ChildSummary {
    pub total: i64,
    pub terminal: i64,
    pub failed: i64,
    pub warnings: i64,
    pub waiting_quota: i64,
    pub completed_videos: i64,
}

#[derive(Debug, FromRow)]
struct TerminalJob {
    id: Uuid,
    source_id: Uuid,
    target_id: Option<Uuid>,
    parent_job_id: Option<Uuid>,
    runtime_config_id: Uuid,
    include_comments: bool,
    max_videos: Option<i32>,
    max_comments_per_video: Option<i32>,
    checkpoint: Value,
    partial_errors: Value,
    source_type: String,
    source_config: Value,
}

#[derive(Debug, Clone)]
pub struct CollectionStore {
    pool: PgPool,
}

impl CollectionStore {
    #[must_use]
    pub const fn new(pool: PgPool) -> Self {
        Self { pool }
    }

    pub async fn claim_next(
        &self,
        worker_id: &str,
        lease_seconds: i64,
    ) -> Result<Option<ClaimedJob>, StoreError> {
        if worker_id.is_empty() || !(30..=3_600).contains(&lease_seconds) {
            return Err(StoreError::InvalidLease);
        }
        sqlx::query_as::<_, ClaimedJob>(
            r"
            WITH candidate AS (
              SELECT id
              FROM sync_jobs
              WHERE (
                state = 'queued'
                OR (state IN ('waiting_retry', 'waiting_quota')
                    AND resume_at IS NOT NULL AND resume_at <= now())
                OR (state = 'running' AND lease_expires_at IS NOT NULL
                    AND lease_expires_at <= now())
              )
                AND (lease_expires_at IS NULL OR lease_expires_at <= now())
              ORDER BY created_at
              FOR UPDATE SKIP LOCKED
              LIMIT 1
            )
            UPDATE sync_jobs AS job
            SET state = 'running',
                current_stage = CASE WHEN job.state = 'running' THEN 'reclaimed' ELSE 'claimed' END,
                pause_reason = NULL,
                quota_bucket = NULL,
                resume_at = NULL,
                resume_is_automatic = FALSE,
                lease_owner = $1,
                lease_expires_at = now() + ($2 * interval '1 second'),
                updated_at = now()
            FROM candidate
            WHERE job.id = candidate.id
            RETURNING job.id, job.source_id, job.target_id, job.parent_job_id,
                      job.runtime_config_id,
                      job.current_stage, job.include_comments, job.max_videos,
                      job.max_comments_per_video, job.checkpoint, job.partial_errors,
                      job.retry_count,
                      job.lease_owner, job.lease_expires_at
            ",
        )
        .bind(worker_id)
        .bind(lease_seconds)
        .fetch_optional(&self.pool)
        .await
        .map_err(StoreError::Database)
    }

    pub async fn renew(
        &self,
        job_id: Uuid,
        worker_id: &str,
        lease_seconds: i64,
    ) -> Result<(), StoreError> {
        let result = sqlx::query(
            r"
            UPDATE sync_jobs
            SET lease_expires_at = now() + ($1 * interval '1 second'),
                updated_at = now()
            WHERE id = $2 AND state = 'running' AND lease_owner = $3
            ",
        )
        .bind(lease_seconds)
        .bind(job_id)
        .bind(worker_id)
        .execute(&self.pool)
        .await?;
        if result.rows_affected() == 1 {
            Ok(())
        } else {
            Err(StoreError::LeaseLost)
        }
    }

    pub async fn load_runtime_keys(
        &self,
        runtime_config_id: Uuid,
        encryption_key: &str,
    ) -> Result<Vec<String>, StoreError> {
        if encryption_key.is_empty() {
            return Err(StoreError::InvalidEncryptionKey);
        }
        sqlx::query_scalar::<_, String>(
            r"
            SELECT pgp_sym_decrypt(encrypted_key, $1)::text
            FROM youtube_runtime_keys
            WHERE runtime_config_id = $2 AND status <> 'disabled'
              AND (unavailable_until IS NULL OR unavailable_until <= now())
            ORDER BY created_at, id
            ",
        )
        .bind(encryption_key)
        .bind(runtime_config_id)
        .fetch_all(&self.pool)
        .await
        .map_err(StoreError::Database)
    }

    pub async fn record_runtime_key_state(
        &self,
        runtime_config_id: Uuid,
        fingerprint: &str,
        error_reason: Option<&str>,
    ) -> Result<(), StoreError> {
        if fingerprint.len() != 24 {
            return Err(StoreError::InvalidKeyFingerprint);
        }
        if let Some(error_reason) = error_reason {
            sqlx::query(
                r"
                UPDATE youtube_runtime_keys
                SET status = 'cooling_down', failure_count = failure_count + 1,
                    last_error_reason = $1,
                    unavailable_until = now() +
                      (LEAST(3, failure_count + 1) * interval '1 hour'),
                    updated_at = now()
                WHERE runtime_config_id = $2 AND key_fingerprint = $3
                ",
            )
            .bind(safe_reason(error_reason, 200))
            .bind(runtime_config_id)
            .bind(fingerprint)
            .execute(&self.pool)
            .await?;
        } else {
            sqlx::query(
                r"
                UPDATE youtube_runtime_keys
                SET status = 'active', failure_count = 0, last_error_reason = NULL,
                    unavailable_until = NULL, last_used_at = now(), updated_at = now()
                WHERE runtime_config_id = $1 AND key_fingerprint = $2
                ",
            )
            .bind(runtime_config_id)
            .bind(fingerprint)
            .execute(&self.pool)
            .await?;
        }
        Ok(())
    }

    pub async fn record_api_request(
        &self,
        job: &ClaimedJob,
        bucket: &str,
        endpoint: &str,
        status_code: u16,
        error_reason: Option<&str>,
    ) -> Result<(), StoreError> {
        if !matches!(bucket, "core" | "search_queries") || endpoint.is_empty() {
            return Err(StoreError::InvalidApiRequest);
        }
        let cost = if endpoint == "search" { 100_i32 } else { 1_i32 };
        let mut transaction = self.pool.begin().await?;
        sqlx::query(
            r"
            INSERT INTO api_request_logs (
              job_id, runtime_config_id, bucket, endpoint, parameter_hash,
              expected_cost, actual_cost, http_status, error_reason
            ) VALUES ($1, $2, $3, $4, 'server-managed', $5, $5, $6, $7)
            ",
        )
        .bind(job.id)
        .bind(job.runtime_config_id)
        .bind(bucket)
        .bind(safe_reason(endpoint, 100))
        .bind(cost)
        .bind(i32::from(status_code))
        .bind(error_reason.map(|value| safe_reason(value, 200)))
        .execute(&mut *transaction)
        .await?;
        sqlx::query(
            r"
            UPDATE sync_jobs
            SET actual_quota = jsonb_set(
                  COALESCE(actual_quota, '{}'::jsonb), ARRAY[$1::text],
                  to_jsonb(COALESCE((actual_quota ->> $1)::integer, 0) + $2), TRUE
                ),
                updated_at = now()
            WHERE id = $3
            ",
        )
        .bind(bucket)
        .bind(cost)
        .bind(job.id)
        .execute(&mut *transaction)
        .await?;
        sqlx::query(
            r"
            INSERT INTO quota_ledger (
              runtime_config_id, job_id, bucket, endpoint, entry_type,
              estimated_cost, actual_cost
            ) VALUES ($1, $2, $3, $4, 'consumed', $5, $5)
            ",
        )
        .bind(job.runtime_config_id)
        .bind(job.id)
        .bind(bucket)
        .bind(safe_reason(endpoint, 100))
        .bind(cost)
        .execute(&mut *transaction)
        .await?;
        transaction.commit().await?;
        Ok(())
    }

    pub async fn source(&self, source_id: Uuid) -> Result<CollectionSource, StoreError> {
        sqlx::query_as::<_, CollectionSource>(
            r"
            SELECT source.id, source.type::text AS source_type, source.config,
                   source.target_id, COALESCE(target.coverage, '{}'::jsonb) AS coverage
            FROM collection_sources AS source
            LEFT JOIN collection_targets AS target ON target.id = source.target_id
            WHERE source.id = $1
            ",
        )
        .bind(source_id)
        .fetch_optional(&self.pool)
        .await?
        .ok_or(StoreError::SourceNotFound)
    }

    #[allow(clippy::too_many_lines)]
    pub async fn promote_channel_target(
        &self,
        source_id: Uuid,
        youtube_channel_id: &str,
        handle: Option<&str>,
    ) -> Result<Option<Uuid>, StoreError> {
        if youtube_channel_id.len() != 24 || !youtube_channel_id.starts_with("UC") {
            return Err(StoreError::InvalidChannelId);
        }
        let mut transaction = self.pool.begin().await?;
        let current_target = sqlx::query_scalar::<_, Uuid>(
            r"
            SELECT target_id FROM collection_sources
            WHERE id = $1 AND type = 'channel' AND target_id IS NOT NULL
            ",
        )
        .bind(source_id)
        .fetch_optional(&mut *transaction)
        .await?;
        let Some(current_target) = current_target else {
            transaction.commit().await?;
            return Ok(None);
        };
        let canonical_key = format!("channel:{youtube_channel_id}");
        let existing_target = sqlx::query_scalar::<_, Uuid>(
            "SELECT id FROM collection_targets WHERE type = 'channel' AND canonical_key = $1",
        )
        .bind(&canonical_key)
        .fetch_optional(&mut *transaction)
        .await?;
        let mut locks = vec![current_target];
        if let Some(existing) = existing_target {
            locks.push(existing);
        }
        locks.sort_unstable();
        locks.dedup();
        sqlx::query_scalar::<_, Uuid>(
            "SELECT id FROM collection_targets WHERE id = ANY($1::uuid[]) ORDER BY id FOR UPDATE",
        )
        .bind(&locks)
        .fetch_all(&mut *transaction)
        .await?;

        let target_id = if let Some(existing) = existing_target.filter(|id| *id != current_target) {
            let canonical_has_active = sqlx::query_scalar::<_, bool>(
                r"
                SELECT EXISTS (
                  SELECT 1 FROM sync_jobs WHERE target_id = $1
                    AND state IN ('queued', 'running', 'waiting_retry', 'waiting_quota')
                )
                ",
            )
            .bind(existing)
            .fetch_one(&mut *transaction)
            .await?;
            if canonical_has_active {
                sqlx::query(
                    r"
                    UPDATE sync_jobs SET target_id = NULL, updated_at = now()
                    WHERE target_id = $1
                      AND state IN ('queued', 'running', 'waiting_retry', 'waiting_quota')
                    ",
                )
                .bind(current_target)
                .execute(&mut *transaction)
                .await?;
            }
            sqlx::query(
                r"
                UPDATE collection_requests AS request
                SET subscription_id = destination.id
                FROM collection_subscriptions AS provisional
                JOIN collection_subscriptions AS destination
                  ON destination.user_id = provisional.user_id
                 AND destination.target_id = $1
                WHERE provisional.target_id = $2
                  AND request.subscription_id = provisional.id
                ",
            )
            .bind(existing)
            .bind(current_target)
            .execute(&mut *transaction)
            .await?;
            sqlx::query(
                r"
                UPDATE collection_subscriptions AS destination
                SET enabled = destination.enabled OR provisional.enabled, updated_at = now()
                FROM collection_subscriptions AS provisional
                WHERE provisional.target_id = $1 AND destination.target_id = $2
                  AND destination.user_id = provisional.user_id
                ",
            )
            .bind(current_target)
            .bind(existing)
            .execute(&mut *transaction)
            .await?;
            sqlx::query(
                r"
                DELETE FROM collection_subscriptions AS provisional
                USING collection_subscriptions AS destination
                WHERE provisional.target_id = $1 AND destination.target_id = $2
                  AND destination.user_id = provisional.user_id
                ",
            )
            .bind(current_target)
            .bind(existing)
            .execute(&mut *transaction)
            .await?;
            sqlx::query(
                "UPDATE collection_subscriptions SET target_id = $1, updated_at = now() WHERE target_id = $2",
            )
            .bind(existing)
            .bind(current_target)
            .execute(&mut *transaction)
            .await?;
            sqlx::query(
                r"
                UPDATE collection_requests AS provisional
                SET idempotency_key = NULL, updated_at = now()
                FROM collection_requests AS canonical
                WHERE provisional.target_id = $1 AND canonical.target_id = $2
                  AND provisional.user_id IS NOT DISTINCT FROM canonical.user_id
                  AND provisional.idempotency_key = canonical.idempotency_key
                  AND provisional.idempotency_key IS NOT NULL
                ",
            )
            .bind(current_target)
            .bind(existing)
            .execute(&mut *transaction)
            .await?;
            sqlx::query(
                r"
                INSERT INTO collection_target_pins (
                  target_id, enabled, interval_minutes, next_run_at, last_dispatched_at
                )
                SELECT $1, enabled, interval_minutes, next_run_at, last_dispatched_at
                FROM collection_target_pins WHERE target_id = $2
                ON CONFLICT (target_id) DO UPDATE
                SET enabled = collection_target_pins.enabled OR EXCLUDED.enabled,
                    next_run_at = LEAST(collection_target_pins.next_run_at, EXCLUDED.next_run_at),
                    updated_at = now()
                ",
            )
            .bind(existing)
            .bind(current_target)
            .execute(&mut *transaction)
            .await?;
            sqlx::query("UPDATE collection_sources SET target_id = $1 WHERE target_id = $2")
                .bind(existing)
                .bind(current_target)
                .execute(&mut *transaction)
                .await?;
            sqlx::query("UPDATE collection_requests SET target_id = $1 WHERE target_id = $2")
                .bind(existing)
                .bind(current_target)
                .execute(&mut *transaction)
                .await?;
            sqlx::query("UPDATE sync_jobs SET target_id = $1 WHERE target_id = $2")
                .bind(existing)
                .bind(current_target)
                .execute(&mut *transaction)
                .await?;
            sqlx::query(
                r"
                INSERT INTO collection_target_videos (
                  target_id, video_id, first_seen_at, last_seen_at
                )
                SELECT $1, video_id, first_seen_at, last_seen_at
                FROM collection_target_videos WHERE target_id = $2
                ON CONFLICT (target_id, video_id) DO UPDATE
                SET first_seen_at = LEAST(collection_target_videos.first_seen_at,
                                          EXCLUDED.first_seen_at),
                    last_seen_at = GREATEST(collection_target_videos.last_seen_at,
                                           EXCLUDED.last_seen_at)
                ",
            )
            .bind(existing)
            .bind(current_target)
            .execute(&mut *transaction)
            .await?;
            sqlx::query(
                r"
                INSERT INTO collection_target_aliases (
                  target_id, target_type, alias_kind, alias_value
                )
                SELECT $1, target_type, alias_kind, alias_value
                FROM collection_target_aliases WHERE target_id = $2
                ON CONFLICT (target_type, alias_kind, alias_value) DO NOTHING
                ",
            )
            .bind(existing)
            .bind(current_target)
            .execute(&mut *transaction)
            .await?;
            sqlx::query("DELETE FROM collection_target_aliases WHERE target_id = $1")
                .bind(current_target)
                .execute(&mut *transaction)
                .await?;
            sqlx::query("DELETE FROM collection_targets WHERE id = $1")
                .bind(current_target)
                .execute(&mut *transaction)
                .await?;
            existing
        } else {
            sqlx::query(
                r"
                UPDATE collection_targets AS target
                SET canonical_key = $1, resolved_channel_id = channel.id, updated_at = now()
                FROM channels AS channel
                WHERE target.id = $2 AND channel.youtube_channel_id = $3
                ",
            )
            .bind(&canonical_key)
            .bind(current_target)
            .bind(youtube_channel_id)
            .execute(&mut *transaction)
            .await?;
            current_target
        };
        for (kind, value) in [("channel_id", Some(youtube_channel_id)), ("handle", handle)] {
            if let Some(value) = value.filter(|value| !value.is_empty()) {
                sqlx::query(
                    r"
                    INSERT INTO collection_target_aliases (
                      target_id, target_type, alias_kind, alias_value
                    ) VALUES ($1, 'channel', $2, $3)
                    ON CONFLICT (target_type, alias_kind, alias_value)
                    DO UPDATE SET target_id = EXCLUDED.target_id
                    ",
                )
                .bind(target_id)
                .bind(kind)
                .bind(if kind == "handle" {
                    value.to_lowercase()
                } else {
                    value.to_owned()
                })
                .execute(&mut *transaction)
                .await?;
            }
        }
        sync_target_pin(&mut transaction, target_id).await?;
        transaction.commit().await?;
        Ok(Some(target_id))
    }

    pub async fn checkpoint(
        &self,
        job: &ClaimedJob,
        current_stage: &str,
        checkpoint: &Value,
        completed: i32,
        total: Option<i32>,
        unit: &str,
    ) -> Result<(), StoreError> {
        if !matches!(unit, "sources" | "pages" | "videos" | "comments")
            || current_stage.is_empty()
            || completed < 0
            || total.is_some_and(|value| value < completed)
        {
            return Err(StoreError::InvalidCheckpoint);
        }
        let result = sqlx::query(
            r"
            UPDATE sync_jobs
            SET current_stage = $1, checkpoint = $2,
                progress_completed = $3, progress_total = $4,
                progress_unit = $5, retry_count = 0, updated_at = now()
            WHERE id = $6 AND state = 'running' AND lease_owner = $7
            ",
        )
        .bind(current_stage)
        .bind(checkpoint)
        .bind(completed)
        .bind(total)
        .bind(unit)
        .bind(job.id)
        .bind(&job.lease_owner)
        .execute(&self.pool)
        .await?;
        fenced(result.rows_affected())
    }

    pub async fn current_checkpoint(&self, job: &ClaimedJob) -> Result<Value, StoreError> {
        sqlx::query_scalar::<_, Value>(
            r"
            SELECT checkpoint FROM sync_jobs
            WHERE id = $1 AND state = 'running' AND lease_owner = $2
            ",
        )
        .bind(job.id)
        .bind(&job.lease_owner)
        .fetch_optional(&self.pool)
        .await?
        .ok_or(StoreError::LeaseLost)
    }

    pub async fn complete(&self, job: &ClaimedJob) -> Result<(), StoreError> {
        self.finish_terminal(job, None, None).await
    }

    pub async fn fail(&self, job: &ClaimedJob, reason: &str) -> Result<(), StoreError> {
        self.finish_terminal(job, Some(reason), None).await
    }

    pub async fn fail_classified(
        &self,
        job: &ClaimedJob,
        reason: &str,
        failure: FailureMetadata<'_>,
    ) -> Result<(), StoreError> {
        self.finish_terminal(job, Some(reason), Some(failure)).await
    }

    pub async fn add_partial_error(
        &self,
        job: &ClaimedJob,
        code: &str,
        message: &str,
        scope: Option<&str>,
    ) -> Result<(), StoreError> {
        if code.is_empty() || message.is_empty() {
            return Err(StoreError::InvalidPartialError);
        }
        let warning = json!({
            "code": safe_reason(code, 100),
            "message": safe_reason(message, 1_000),
            "scope": scope.map(|value| safe_reason(value, 200)),
        });
        let result = sqlx::query(
            r"
            UPDATE sync_jobs
            SET partial_errors = CASE
                  WHEN EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements(COALESCE(partial_errors, '[]'::jsonb)) AS item
                    WHERE item ->> 'code' = $4
                      AND item ->> 'scope' IS NOT DISTINCT FROM $5
                  ) THEN partial_errors
                  ELSE COALESCE(partial_errors, '[]'::jsonb) || $1::jsonb
                END,
                updated_at = now()
            WHERE id = $2 AND state = 'running' AND lease_owner = $3
            ",
        )
        .bind(json!([warning]))
        .bind(job.id)
        .bind(&job.lease_owner)
        .bind(safe_reason(code, 100))
        .bind(scope.map(|value| safe_reason(value, 200)))
        .execute(&self.pool)
        .await?;
        fenced(result.rows_affected())
    }

    pub async fn wait_retry_classified(
        &self,
        job: &ClaimedJob,
        reason: &str,
        failure: FailureMetadata<'_>,
    ) -> Result<(), StoreError> {
        if !failure.retryable || job.retry_count >= 4 {
            return self.finish_terminal(job, Some(reason), Some(failure)).await;
        }
        let next_retry = job.retry_count.saturating_add(1);
        let delay_seconds = match next_retry {
            1 => 60_i64,
            2 => 120,
            3 => 300,
            _ => 600,
        };
        let result = sqlx::query(
            r"
            UPDATE sync_jobs
            SET state = 'waiting_retry', current_stage = 'waiting_to_retry',
                pause_reason = $1, quota_bucket = NULL,
                resume_at = now() + ($2 * interval '1 second'),
                resume_is_automatic = TRUE, retry_count = $3,
                last_error_code = $4, last_error_provider = $5,
                last_error_operation = $6, last_error_retryable = $7,
                last_error_http_status = $8, last_error_at = now(),
                lease_owner = NULL, lease_expires_at = NULL, updated_at = now()
            WHERE id = $9 AND state = 'running' AND lease_owner = $10
            ",
        )
        .bind(safe_reason(reason, 1_000))
        .bind(delay_seconds)
        .bind(next_retry)
        .bind(safe_reason(failure.code, 100))
        .bind(failure.provider.map(|value| safe_reason(value, 100)))
        .bind(failure.operation.map(|value| safe_reason(value, 100)))
        .bind(failure.retryable)
        .bind(failure.http_status.map(i32::from))
        .bind(job.id)
        .bind(&job.lease_owner)
        .execute(&self.pool)
        .await?;
        fenced(result.rows_affected())
    }

    pub async fn wait_children(
        &self,
        job: &ClaimedJob,
        waiting_quota: bool,
    ) -> Result<(), StoreError> {
        self.finish(
            job,
            "waiting_retry",
            if waiting_quota {
                "waiting_for_quota"
            } else {
                "waiting_for_video_jobs"
            },
            Some("Waiting for video collection jobs"),
            Some(5),
            None,
        )
        .await
    }

    pub async fn wait_quota(
        &self,
        job: &ClaimedJob,
        reason: &str,
        bucket: &str,
        seconds: i64,
    ) -> Result<(), StoreError> {
        if !matches!(bucket, "core" | "search_queries") {
            return Err(StoreError::InvalidQuotaBucket);
        }
        self.finish(
            job,
            "waiting_quota",
            "waiting_for_quota",
            Some(reason),
            Some(seconds.clamp(60, 86_400)),
            Some(bucket),
        )
        .await
    }

    #[allow(clippy::too_many_lines)]
    async fn finish_terminal(
        &self,
        job: &ClaimedJob,
        failure_reason: Option<&str>,
        failure: Option<FailureMetadata<'_>>,
    ) -> Result<(), StoreError> {
        let mut transaction = self.pool.begin().await?;

        // Every target touched by a terminal parent is locked in one UUID order.
        // Shared videos can make two jobs affect overlapping target sets.
        if job.parent_job_id.is_none() {
            sqlx::query_scalar::<_, Uuid>(
                r"
                WITH touched_video_ids AS (
                  SELECT video.id
                  FROM sync_jobs AS child
                  JOIN videos AS video
                    ON video.youtube_video_id = child.checkpoint ->> 'youtubeVideoId'
                  WHERE child.parent_job_id = $1
                  UNION
                  SELECT video.id
                  FROM videos AS video
                  WHERE video.youtube_video_id = $2
                ), affected AS (
                  SELECT DISTINCT membership.target_id
                  FROM collection_target_videos AS membership
                  JOIN touched_video_ids AS touched ON touched.id = membership.video_id
                  UNION
                  SELECT $3::uuid WHERE $3::uuid IS NOT NULL
                )
                SELECT target.id
                FROM collection_targets AS target
                JOIN affected ON affected.target_id = target.id
                ORDER BY target.id
                FOR UPDATE
                ",
            )
            .bind(job.id)
            .bind(
                job.checkpoint
                    .get("youtubeVideoId")
                    .and_then(Value::as_str)
                    .unwrap_or(""),
            )
            .bind(job.target_id)
            .fetch_all(&mut *transaction)
            .await?;
        } else if let Some(target_id) = job.target_id {
            sqlx::query("SELECT id FROM collection_targets WHERE id = $1 FOR UPDATE")
                .bind(target_id)
                .execute(&mut *transaction)
                .await?;
        }

        let current = sqlx::query_as::<_, TerminalJob>(
            r"
            SELECT job.id, job.source_id, job.target_id, job.parent_job_id,
                   job.runtime_config_id, job.include_comments, job.max_videos,
                   job.max_comments_per_video, job.checkpoint, job.partial_errors,
                   source.type::text AS source_type, source.config AS source_config
            FROM sync_jobs AS job
            JOIN collection_sources AS source ON source.id = job.source_id
            WHERE job.id = $1 AND job.state = 'running' AND job.lease_owner = $2
            FOR UPDATE OF job
            ",
        )
        .bind(job.id)
        .bind(&job.lease_owner)
        .fetch_optional(&mut *transaction)
        .await?
        .ok_or(StoreError::LeaseLost)?;

        let successful = failure_reason.is_none();
        let terminal_state = if !successful {
            "failed"
        } else if current
            .partial_errors
            .as_array()
            .is_some_and(|errors| !errors.is_empty())
        {
            "completed_with_warnings"
        } else {
            "completed"
        };
        let transition_stage = if successful { "completed" } else { "failed" };
        let updated = sqlx::query(
            r"
            UPDATE sync_jobs
            SET state = $1::job_state, current_stage = $2, pause_reason = $3,
                quota_bucket = NULL, resume_at = NULL, resume_is_automatic = FALSE,
                retry_count = 0,
                last_error_code = COALESCE($4, last_error_code),
                last_error_provider = COALESCE($5, last_error_provider),
                last_error_operation = COALESCE($6, last_error_operation),
                last_error_retryable = COALESCE($7, last_error_retryable),
                last_error_http_status = COALESCE($8, last_error_http_status),
                last_error_at = CASE WHEN $4::text IS NULL THEN last_error_at ELSE now() END,
                lease_owner = NULL, lease_expires_at = NULL, updated_at = now()
            WHERE id = $9 AND state = 'running' AND lease_owner = $10
            ",
        )
        .bind(terminal_state)
        .bind(transition_stage)
        .bind(failure_reason.map(|value| safe_reason(value, 1_000)))
        .bind(failure.map(|value| safe_reason(value.code, 100)))
        .bind(failure.and_then(|value| value.provider.map(|item| safe_reason(item, 100))))
        .bind(failure.and_then(|value| value.operation.map(|item| safe_reason(item, 100))))
        .bind(failure.map(|value| value.retryable))
        .bind(failure.and_then(|value| value.http_status.map(i32::from)))
        .bind(current.id)
        .bind(&job.lease_owner)
        .execute(&mut *transaction)
        .await?;
        fenced(updated.rows_affected())?;

        sqlx::query(
            "UPDATE collection_requests SET status = $1, updated_at = now() WHERE job_id = $2",
        )
        .bind(terminal_state)
        .bind(current.id)
        .execute(&mut *transaction)
        .await?;

        if let Some(target_id) = current.target_id {
            if successful {
                let coverage = completed_coverage(&current)?;
                sqlx::query(
                    r"
                    UPDATE collection_targets
                    SET coverage = $1, last_completed_at = now(), updated_at = now()
                    WHERE id = $2
                    ",
                )
                .bind(coverage)
                .bind(target_id)
                .execute(&mut *transaction)
                .await?;
            }
            self.enqueue_successor_if_requested(&mut transaction, &current)
                .await?;
        }

        if current.parent_job_id.is_none() {
            self.advance_data_version_and_enqueue_analysis(
                &mut transaction,
                &current,
                terminal_state,
                successful,
            )
            .await?;
        }

        transaction.commit().await?;
        Ok(())
    }

    async fn enqueue_successor_if_requested(
        &self,
        transaction: &mut sqlx::Transaction<'_, sqlx::Postgres>,
        current: &TerminalJob,
    ) -> Result<(), StoreError> {
        let Some(target_id) = current.target_id else {
            return Ok(());
        };
        let pending = sqlx::query_scalar::<_, Uuid>(
            r"
            SELECT id FROM collection_requests
            WHERE target_id = $1 AND job_id IS NULL AND status = 'queued'
            ORDER BY created_at, id
            FOR UPDATE
            ",
        )
        .bind(target_id)
        .fetch_all(&mut **transaction)
        .await?;
        if pending.is_empty() {
            return Ok(());
        }

        let successor = sqlx::query_scalar::<_, Uuid>(
            r"
            WITH selected_source AS (
              SELECT source.id, source.config, source.type::text AS source_type
              FROM collection_sources AS source
              WHERE source.target_id = $1
              ORDER BY
                (COALESCE(source.config ->> 'includeComments', 'false') = 'true') DESC,
                COALESCE((source.config ->> 'maxVideos')::integer, 0) DESC,
                COALESCE((source.config ->> 'maxPagesPerRun')::integer, 0) DESC,
                COALESCE((source.config ->> 'maxCommentPagesPerVideo')::integer, 0) DESC,
                source.created_at, source.id
              LIMIT 1
              FOR UPDATE
            )
            INSERT INTO sync_jobs (
              source_id, target_id, runtime_config_id, state, current_stage,
              idempotency_key, include_comments, max_videos,
              max_comments_per_video
            )
            SELECT source.id, $1, $2, 'queued', 'queued', $3,
                   COALESCE((source.config ->> 'includeComments')::boolean, FALSE),
                   CASE WHEN source.source_type = 'channel'
                     THEN COALESCE((source.config ->> 'maxVideos')::integer, 50) END,
                   COALESCE((source.config ->> 'maxCommentPagesPerVideo')::integer, 1)
            FROM selected_source AS source
            RETURNING id
            ",
        )
        .bind(target_id)
        .bind(current.runtime_config_id)
        .bind(Uuid::new_v4().to_string())
        .fetch_optional(&mut **transaction)
        .await?;
        if let Some(successor_id) = successor {
            sqlx::query(
                r"
                UPDATE collection_requests
                SET job_id = $1, status = 'queued', updated_at = now()
                WHERE id = ANY($2::uuid[])
                ",
            )
            .bind(successor_id)
            .bind(&pending)
            .execute(&mut **transaction)
            .await?;
        }
        Ok(())
    }

    #[allow(clippy::too_many_lines)]
    async fn advance_data_version_and_enqueue_analysis(
        &self,
        transaction: &mut sqlx::Transaction<'_, sqlx::Postgres>,
        current: &TerminalJob,
        terminal_state: &str,
        successful: bool,
    ) -> Result<(), StoreError> {
        let analysis_coverage = json!({
            "partial": terminal_state == "completed_with_warnings"
        });
        let sample_plan = json!({
            "strategy": "per-video-recent",
            "maxComments": 50_000,
            "maxPerVideo": 1_000
        });
        if let Some(target_id) = current.target_id {
            let versions = sqlx::query_as::<_, (Uuid, i64)>(
                r"
                WITH touched_video_ids AS (
                  SELECT video.id
                  FROM sync_jobs AS child
                  JOIN videos AS video
                    ON video.youtube_video_id = child.checkpoint ->> 'youtubeVideoId'
                  WHERE child.parent_job_id = $1
                  UNION
                  SELECT video.id
                  FROM videos AS video
                  WHERE video.youtube_video_id = $2
                ), affected AS (
                  SELECT DISTINCT membership.target_id
                  FROM collection_target_videos AS membership
                  JOIN touched_video_ids AS touched ON touched.id = membership.video_id
                  UNION SELECT $3::uuid
                )
                UPDATE collection_targets AS target
                SET data_version = target.data_version + 1, updated_at = now()
                FROM affected
                WHERE target.id = affected.target_id
                RETURNING target.id, target.data_version
                ",
            )
            .bind(current.id)
            .bind(
                current
                    .checkpoint
                    .get("youtubeVideoId")
                    .and_then(Value::as_str)
                    .unwrap_or(""),
            )
            .bind(target_id)
            .fetch_all(&mut **transaction)
            .await?;
            if successful {
                for (affected_target_id, data_version) in versions {
                    sqlx::query(
                        r"
                        INSERT INTO analysis_runs (
                          source_id, target_id, job_id, data_version, state,
                          pipeline_version, policy_gate_version, sample_plan, coverage
                        )
                        SELECT $1, $2, $3, $4, 'queued', 'deterministic-v3',
                               'server-managed', $5, $6
                        WHERE NOT EXISTS (
                          SELECT 1 FROM analysis_runs
                          WHERE target_id = $2 AND data_version = $4
                            AND pipeline_version = 'deterministic-v3'
                        )
                        ",
                    )
                    .bind((affected_target_id == target_id).then_some(current.source_id))
                    .bind(affected_target_id)
                    .bind(current.id)
                    .bind(data_version)
                    .bind(&sample_plan)
                    .bind(&analysis_coverage)
                    .execute(&mut **transaction)
                    .await?;
                }
            }
        } else {
            let data_version = sqlx::query_scalar::<_, i64>(
                r"
                UPDATE collection_sources
                SET data_version = data_version + 1, updated_at = now()
                WHERE id = $1
                RETURNING data_version
                ",
            )
            .bind(current.source_id)
            .fetch_optional(&mut **transaction)
            .await?;
            if let (true, Some(data_version)) = (successful, data_version) {
                sqlx::query(
                    r"
                    INSERT INTO analysis_runs (
                      source_id, job_id, data_version, state, pipeline_version,
                      policy_gate_version, sample_plan, coverage
                    )
                    SELECT $1, $2, $3, 'queued', 'deterministic-v3',
                           'server-managed', $4, $5
                    WHERE NOT EXISTS (
                      SELECT 1 FROM analysis_runs
                      WHERE target_id IS NULL AND source_id = $1
                        AND data_version = $3
                        AND pipeline_version = 'deterministic-v3'
                    )
                    ",
                )
                .bind(current.source_id)
                .bind(current.id)
                .bind(data_version)
                .bind(sample_plan)
                .bind(analysis_coverage)
                .execute(&mut **transaction)
                .await?;
            }
        }
        Ok(())
    }

    async fn finish(
        &self,
        job: &ClaimedJob,
        state: &str,
        transition_stage: &str,
        reason: Option<&str>,
        resume_seconds: Option<i64>,
        quota_bucket: Option<&str>,
    ) -> Result<(), StoreError> {
        let result = sqlx::query(
            r"
            UPDATE sync_jobs
            SET state = $1::job_state, current_stage = $2,
                pause_reason = $3, quota_bucket = $4,
                resume_at = CASE WHEN $5::bigint IS NULL THEN NULL
                  ELSE now() + ($5 * interval '1 second') END,
                resume_is_automatic = $5::bigint IS NOT NULL,
                lease_owner = NULL, lease_expires_at = NULL, updated_at = now()
            WHERE id = $6 AND state = 'running' AND lease_owner = $7
            ",
        )
        .bind(state)
        .bind(transition_stage)
        .bind(reason.map(|value| safe_reason(value, 1_000)))
        .bind(quota_bucket)
        .bind(resume_seconds)
        .bind(job.id)
        .bind(&job.lease_owner)
        .execute(&self.pool)
        .await?;
        fenced(result.rows_affected())
    }

    pub async fn dispatch_due_pins(&self, limit: i64) -> Result<u64, StoreError> {
        if !(1..=100).contains(&limit) {
            return Err(StoreError::InvalidDispatchLimit);
        }
        let mut transaction = self.pool.begin().await?;
        let targets = sqlx::query_as::<_, (Uuid, i32)>(
            r"
            SELECT pin.target_id, pin.interval_minutes
            FROM collection_target_pins AS pin
            WHERE pin.enabled = TRUE AND pin.next_run_at <= now()
              AND EXISTS (
                SELECT 1 FROM collection_subscriptions AS subscription
                WHERE subscription.target_id = pin.target_id AND subscription.enabled = TRUE
              )
            ORDER BY pin.next_run_at
            FOR UPDATE SKIP LOCKED
            LIMIT $1
            ",
        )
        .bind(limit)
        .fetch_all(&mut *transaction)
        .await?;
        let mut dispatched = 0_u64;
        for (target_id, interval_minutes) in targets {
            let active = sqlx::query_scalar::<_, bool>(
                r"
                SELECT EXISTS (
                  SELECT 1 FROM sync_jobs WHERE target_id = $1
                    AND state IN ('queued', 'running', 'waiting_retry', 'waiting_quota')
                )
                ",
            )
            .bind(target_id)
            .fetch_one(&mut *transaction)
            .await?;
            if !active && self.enqueue_target(&mut transaction, target_id).await? {
                dispatched = dispatched.saturating_add(1);
            }
            sqlx::query(
                r"
                UPDATE collection_target_pins
                SET last_dispatched_at = CASE WHEN $2 THEN now() ELSE last_dispatched_at END,
                    next_run_at = now() + ($3 * interval '1 minute'), updated_at = now()
                WHERE target_id = $1
                ",
            )
            .bind(target_id)
            .bind(!active)
            .bind(interval_minutes)
            .execute(&mut *transaction)
            .await?;
        }
        transaction.commit().await?;
        Ok(dispatched)
    }

    pub async fn enqueue_video_batches(
        &self,
        parent: &ClaimedJob,
        youtube_video_ids: &[String],
    ) -> Result<u64, StoreError> {
        if youtube_video_ids.len() > 5_000 {
            return Err(StoreError::TooManyVideos);
        }
        let mut unique = youtube_video_ids
            .iter()
            .filter(|value| !value.is_empty())
            .cloned()
            .collect::<Vec<_>>();
        unique.sort();
        unique.dedup();
        let mut transaction = self.pool.begin().await?;
        let owned = sqlx::query_scalar::<_, bool>(
            r"
            SELECT EXISTS (
              SELECT 1 FROM sync_jobs
              WHERE id = $1 AND state = 'running' AND lease_owner = $2
              FOR UPDATE
            )
            ",
        )
        .bind(parent.id)
        .bind(&parent.lease_owner)
        .fetch_one(&mut *transaction)
        .await?;
        if !owned {
            return Err(StoreError::LeaseLost);
        }
        let mut inserted = 0_u64;
        for (batch_number, batch) in unique.chunks(50).enumerate() {
            let batch_key = stable_batch_key(batch);
            let checkpoint = serde_json::json!({
                "jobKind": "video_batch",
                "batchNumber": batch_number,
                "batchKey": batch_key,
                "youtubeVideoIds": batch,
            });
            let result = sqlx::query(
                r"
                INSERT INTO sync_jobs (
                  source_id, runtime_config_id, parent_job_id, state, current_stage,
                  idempotency_key, include_comments, max_videos,
                  max_comments_per_video, progress_total, progress_unit, checkpoint
                ) VALUES (
                  $1, $2, $3, 'queued', 'queued_video_batch', $4, $5, $6, $7,
                  $6, 'videos', $8
                )
                ON CONFLICT (source_id, idempotency_key) DO NOTHING
                ",
            )
            .bind(parent.source_id)
            .bind(parent.runtime_config_id)
            .bind(parent.id)
            .bind(format!("video-batch:{}:{batch_key}", parent.id))
            .bind(parent.include_comments)
            .bind(i32::try_from(batch.len()).map_err(|_| StoreError::TooManyVideos)?)
            .bind(parent.max_comments_per_video)
            .bind(checkpoint)
            .execute(&mut *transaction)
            .await?;
            inserted = inserted.saturating_add(result.rows_affected());
        }
        transaction.commit().await?;
        Ok(inserted)
    }

    pub async fn child_summary(&self, parent_id: Uuid) -> Result<ChildSummary, StoreError> {
        sqlx::query_as::<_, ChildSummary>(
            r"
            SELECT count(*)::bigint AS total,
                   count(*) FILTER (WHERE state IN (
                     'completed', 'completed_with_warnings', 'failed', 'cancelled'
                   ))::bigint AS terminal,
                   count(*) FILTER (WHERE state IN ('failed', 'cancelled'))::bigint AS failed,
                   count(*) FILTER (WHERE state = 'completed_with_warnings')::bigint AS warnings,
                   count(*) FILTER (WHERE state = 'waiting_quota')::bigint AS waiting_quota,
                   COALESCE(sum(progress_completed), 0)::bigint AS completed_videos
            FROM sync_jobs WHERE parent_job_id = $1
            ",
        )
        .bind(parent_id)
        .fetch_one(&self.pool)
        .await
        .map_err(StoreError::Database)
    }

    pub async fn enqueue_comment_jobs(
        &self,
        video_batch_job: &ClaimedJob,
        youtube_video_ids: &[String],
    ) -> Result<u64, StoreError> {
        // Video-batch jobs attach comment jobs to their top-level parent so that
        // one child summary covers both phases. A direct-video job is itself the
        // parent and must wait for its comment children before becoming terminal.
        let parent_id = video_batch_job.parent_job_id.unwrap_or(video_batch_job.id);
        let mut unique = youtube_video_ids
            .iter()
            .filter(|value| !value.is_empty())
            .cloned()
            .collect::<Vec<_>>();
        unique.sort();
        unique.dedup();
        let mut transaction = self.pool.begin().await?;
        let mut inserted = 0_u64;
        for video_id in unique {
            let result = sqlx::query(
                r"
                INSERT INTO sync_jobs (
                  source_id, runtime_config_id, parent_job_id, state, current_stage,
                  idempotency_key, include_comments, max_videos,
                  max_comments_per_video, progress_total, progress_unit, checkpoint
                ) VALUES (
                  $1, $2, $3, 'queued', 'queued_comment', $4, TRUE, 1, $5,
                  1, 'comments', $6
                )
                ON CONFLICT (source_id, idempotency_key) DO NOTHING
                ",
            )
            .bind(video_batch_job.source_id)
            .bind(video_batch_job.runtime_config_id)
            .bind(parent_id)
            .bind(format!("comment:{parent_id}:{video_id}"))
            .bind(video_batch_job.max_comments_per_video)
            .bind(serde_json::json!({"jobKind": "comment", "youtubeVideoId": video_id}))
            .execute(&mut *transaction)
            .await?;
            inserted = inserted.saturating_add(result.rows_affected());
        }
        transaction.commit().await?;
        Ok(inserted)
    }

    async fn enqueue_target(
        &self,
        transaction: &mut sqlx::Transaction<'_, sqlx::Postgres>,
        target_id: Uuid,
    ) -> Result<bool, StoreError> {
        let row = sqlx::query_as::<_, (Uuid, Uuid, bool, Option<i32>, Option<i32>)>(
            r"
            SELECT source.id, runtime.id,
                   COALESCE((target.config ->> 'includeComments')::boolean, FALSE),
                   CASE WHEN target.type = 'channel'
                     THEN COALESCE((target.config ->> 'maxVideos')::integer, 50) END,
                   COALESCE((target.config ->> 'maxCommentPagesPerVideo')::integer, 1)
            FROM collection_targets AS target
            JOIN LATERAL (
              SELECT candidate.id FROM collection_sources AS candidate
              WHERE candidate.target_id = target.id ORDER BY candidate.created_at LIMIT 1
            ) AS source ON TRUE
            JOIN LATERAL (
              SELECT config.id FROM youtube_runtime_configs AS config
              WHERE config.status = 'active' ORDER BY config.activated_at DESC LIMIT 1
            ) AS runtime ON TRUE
            WHERE target.id = $1
            ",
        )
        .bind(target_id)
        .fetch_optional(&mut **transaction)
        .await?;
        let Some((source_id, runtime_id, comments, max_videos, max_comment_pages)) = row else {
            return Ok(false);
        };
        sqlx::query(
            r"
            INSERT INTO sync_jobs (
              source_id, target_id, runtime_config_id, state, current_stage,
              idempotency_key, include_comments, max_videos, max_comments_per_video
            ) VALUES ($1, $2, $3, 'queued', 'queued', $4, $5, $6, $7)
            ",
        )
        .bind(source_id)
        .bind(target_id)
        .bind(runtime_id)
        .bind(Uuid::new_v4().to_string())
        .bind(comments)
        .bind(max_videos)
        .bind(max_comment_pages)
        .execute(&mut **transaction)
        .await?;
        Ok(true)
    }
}

async fn sync_target_pin(
    transaction: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    target_id: Uuid,
) -> Result<(), sqlx::Error> {
    let target_type = sqlx::query_scalar::<_, String>(
        "SELECT type::text FROM collection_targets WHERE id = $1 FOR UPDATE",
    )
    .bind(target_id)
    .fetch_optional(&mut **transaction)
    .await?;
    let Some(target_type) = target_type else {
        return Ok(());
    };
    let has_enabled = sqlx::query_scalar::<_, bool>(
        r"
        SELECT EXISTS (
          SELECT 1 FROM collection_subscriptions
          WHERE target_id = $1 AND enabled = TRUE
        )
        ",
    )
    .bind(target_id)
    .fetch_one(&mut **transaction)
    .await?;
    if !has_enabled {
        sqlx::query(
            "UPDATE collection_target_pins SET enabled = FALSE, updated_at = now() WHERE target_id = $1",
        )
        .bind(target_id)
        .execute(&mut **transaction)
        .await?;
    } else if matches!(target_type.as_str(), "channel" | "keyword") {
        sqlx::query(
            r"
            INSERT INTO collection_target_pins (
              target_id, enabled, interval_minutes, next_run_at
            ) VALUES ($1, TRUE, 360, now())
            ON CONFLICT (target_id) DO UPDATE
            SET enabled = TRUE, next_run_at = now(), updated_at = now()
            ",
        )
        .bind(target_id)
        .execute(&mut **transaction)
        .await?;
    }
    Ok(())
}

fn completed_coverage(job: &TerminalJob) -> Result<Value, StoreError> {
    let config = job
        .source_config
        .as_object()
        .ok_or(StoreError::InvalidSourceConfig)?;
    let config_bool = |key: &str| config.get(key).and_then(Value::as_bool).unwrap_or(false);
    let config_i64 =
        |key: &str, default: i64| config.get(key).and_then(Value::as_i64).unwrap_or(default);
    let mut coverage = serde_json::Map::from_iter([
        ("complete".to_owned(), Value::Bool(true)),
        (
            "includeComments".to_owned(),
            Value::Bool(job.include_comments),
        ),
        (
            "collectAllComments".to_owned(),
            Value::Bool(job.include_comments && config_bool("collectAllComments")),
        ),
        (
            "maxCommentPagesPerVideo".to_owned(),
            Value::from(i64::from(
                job.max_comments_per_video
                    .unwrap_or_else(|| {
                        i32::try_from(config_i64("maxCommentPagesPerVideo", 1)).unwrap_or(1)
                    })
                    .max(1),
            )),
        ),
    ]);
    match job.source_type.as_str() {
        "channel" => {
            let collect_all_videos = config_bool("collectAllVideos");
            let reconciliation_complete = job
                .checkpoint
                .get("channelReconciliationComplete")
                .and_then(Value::as_bool)
                .unwrap_or(false);
            coverage.insert(
                "complete".to_owned(),
                Value::Bool(!collect_all_videos || reconciliation_complete),
            );
            coverage.insert(
                "collectAllVideos".to_owned(),
                Value::Bool(collect_all_videos),
            );
            coverage.insert(
                "maxVideos".to_owned(),
                Value::from(i64::from(
                    job.max_videos
                        .unwrap_or_else(|| i32::try_from(config_i64("maxVideos", 50)).unwrap_or(50))
                        .max(1),
                )),
            );
            for key in [
                "channelReconciliationNextPageToken",
                "channelReconciliationComplete",
                "channelReportedVideoCount",
                "channelStoredVideoCount",
            ] {
                coverage.insert(
                    key.to_owned(),
                    job.checkpoint.get(key).cloned().unwrap_or_else(|| {
                        if key.ends_with("Complete") {
                            Value::Bool(false)
                        } else if key.ends_with("Count") {
                            Value::from(0)
                        } else {
                            Value::Null
                        }
                    }),
                );
            }
        }
        "keyword" => {
            let historical_complete = job
                .checkpoint
                .get("keywordHistoricalBackfillComplete")
                .and_then(Value::as_bool)
                .unwrap_or(false);
            coverage.insert("complete".to_owned(), Value::Bool(historical_complete));
            coverage.insert(
                "maxPagesPerRun".to_owned(),
                Value::from(config_i64("maxPagesPerRun", 1).max(1)),
            );
            coverage.insert(
                "historicalBackfillComplete".to_owned(),
                Value::Bool(historical_complete),
            );
            coverage.insert(
                "keywordNextPageToken".to_owned(),
                job.checkpoint
                    .get("keywordNextPageToken")
                    .cloned()
                    .unwrap_or(Value::Null),
            );
        }
        "video" => {}
        _ => return Err(StoreError::InvalidSourceConfig),
    }
    Ok(Value::Object(coverage))
}

fn fenced(rows_affected: u64) -> Result<(), StoreError> {
    if rows_affected == 1 {
        Ok(())
    } else {
        Err(StoreError::LeaseLost)
    }
}

fn safe_reason(value: &str, maximum: usize) -> String {
    value.chars().take(maximum).collect()
}

fn stable_batch_key(values: &[String]) -> String {
    use sha2::{Digest, Sha256};
    hex::encode(Sha256::digest(values.join("\n").as_bytes()))[..20].to_owned()
}

#[derive(Debug, Error)]
pub enum StoreError {
    #[error("collection worker lease configuration is invalid")]
    InvalidLease,
    #[error("collection job lease was lost")]
    LeaseLost,
    #[error("collection source was not found")]
    SourceNotFound,
    #[error("collection source configuration is invalid")]
    InvalidSourceConfig,
    #[error("resolved YouTube channel ID is invalid")]
    InvalidChannelId,
    #[error("runtime key encryption key is invalid")]
    InvalidEncryptionKey,
    #[error("runtime key fingerprint is invalid")]
    InvalidKeyFingerprint,
    #[error("YouTube API request log is invalid")]
    InvalidApiRequest,
    #[error("collection checkpoint is invalid")]
    InvalidCheckpoint,
    #[error("quota bucket is invalid")]
    InvalidQuotaBucket,
    #[error("pin dispatch limit is invalid")]
    InvalidDispatchLimit,
    #[error("collection contains too many videos")]
    TooManyVideos,
    #[error("partial collection error is invalid")]
    InvalidPartialError,
    #[error("collection store database operation failed")]
    Database(#[from] sqlx::Error),
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn failure_reason_is_bounded_without_breaking_unicode() {
        assert_eq!(safe_reason("가나다라마바사", 3), "가나다");
    }
}
