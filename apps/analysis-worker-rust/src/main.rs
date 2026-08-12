//! Lease-based deterministic summary worker backed by persisted sparse `BoW`.

use chrono::{DateTime, Utc};
use monitube_postgres::PoolConfig;
use serde_json::{Value, json};
use sqlx::{FromRow, PgPool};
use std::{env, error::Error, time::Duration};
use thiserror::Error;
use uuid::Uuid;

const PIPELINE_VERSION: &str = "deterministic-v3";
const MAX_COMMENTS: i64 = 50_000;
const MAX_PER_VIDEO: i64 = 1_000;
const TOP_WORD_LIMIT: i64 = 10;
const STOP_WORDS: &[&str] = &[
    "the",
    "and",
    "this",
    "that",
    "with",
    "for",
    "from",
    "are",
    "was",
    "have",
    "has",
    "you",
    "your",
    "영상",
    "정말",
    "너무",
    "합니다",
];

#[derive(Debug, FromRow)]
struct AnalysisRun {
    id: Uuid,
    source_id: Option<Uuid>,
    target_id: Option<Uuid>,
    data_version: i64,
    coverage: Value,
    lease_owner: String,
}

#[derive(Debug, FromRow)]
struct ScopeAggregate {
    video_count: i64,
    comment_count: i64,
    latest_video_published_at: Option<DateTime<Utc>>,
    latest_comment_published_at: Option<DateTime<Utc>>,
}

#[derive(Debug, FromRow)]
struct TermAggregate {
    term: String,
    term_count: i64,
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn Error>> {
    monitube_observability::init("monitube-analysis-worker-rust")?;
    let database_url = required("DATABASE_URL")?;
    let worker_id = optional("ANALYSIS_WORKER_ID").unwrap_or_else(|| {
        format!(
            "rust-analysis-{}-{}",
            optional("HOSTNAME").unwrap_or_else(|| "local".to_owned()),
            std::process::id()
        )
    });
    let poll_millis = parse_u64("ANALYSIS_WORKER_POLL_MILLIS", 1_000, 100, 60_000)?;
    let lease_seconds = parse_i64("ANALYSIS_WORKER_LEASE_SECONDS", 900, 300, 3_600)?;
    let pool = monitube_postgres::connect(
        &database_url,
        PoolConfig {
            min_connections: 1,
            max_connections: u32::try_from(parse_u64("ANALYSIS_DB_POOL_MAX_SIZE", 2, 1, 16)?)?,
            acquire_timeout: Duration::from_millis(parse_u64(
                "ANALYSIS_DB_POOL_TIMEOUT_MILLIS",
                10_000,
                100,
                60_000,
            )?),
            connect_timeout: Duration::from_secs(10),
        },
    )
    .await?;
    let worker = AnalysisWorker::new(pool);
    let mut shutdown = Box::pin(tokio::signal::ctrl_c());
    let mut seed_counter = 0_u32;
    tracing::info!(%worker_id, "Rust analysis worker started");
    loop {
        tokio::select! {
            signal = &mut shutdown => {
                if let Err(error) = signal {
                    tracing::warn!(%error, "shutdown signal failed");
                }
                break;
            }
            result = worker.run_once(&worker_id, lease_seconds, seed_counter % 20 == 0) => {
                seed_counter = seed_counter.wrapping_add(1);
                match result {
                    Ok(true) => {}
                    Ok(false) => tokio::time::sleep(Duration::from_millis(poll_millis)).await,
                    Err(error) => {
                        tracing::error!(%error, "analysis worker loop failed");
                        tokio::time::sleep(Duration::from_millis(poll_millis)).await;
                    }
                }
            }
        }
    }
    tracing::info!(%worker_id, "Rust analysis worker stopped");
    Ok(())
}

struct AnalysisWorker {
    pool: PgPool,
}

impl AnalysisWorker {
    const fn new(pool: PgPool) -> Self {
        Self { pool }
    }

    async fn run_once(
        &self,
        worker_id: &str,
        lease_seconds: i64,
        seed: bool,
    ) -> Result<bool, AnalysisError> {
        if seed {
            let queued = self.seed_missing(100).await?;
            if queued > 0 {
                tracing::info!(queued, "queued missing analysis runs");
            }
        }
        let Some(run) = self.claim(worker_id, lease_seconds).await? else {
            return Ok(false);
        };
        let run_id = run.id;
        match self.complete(&run).await {
            Ok(summary) => {
                tracing::info!(
                    %run_id,
                    data_version = run.data_version,
                    video_count = summary.video_count,
                    comment_count = summary.comment_count,
                    "analysis run completed"
                );
            }
            Err(AnalysisError::NlpNotReady) => {
                self.wait_for_nlp(&run, 30).await?;
                tracing::info!(%run_id, "analysis run is waiting for NLP indexing");
            }
            Err(error) => {
                let state = self.fail(&run, &error.to_string(), 3).await?;
                tracing::warn!(%run_id, %state, %error, "analysis run failed");
            }
        }
        Ok(true)
    }

    async fn seed_missing(&self, limit: i64) -> Result<u64, AnalysisError> {
        if !(1..=1_000).contains(&limit) {
            return Err(AnalysisError::InvalidLimit);
        }
        let mut transaction = self.pool.begin().await?;
        let target_result = sqlx::query(
            r"
            WITH candidate AS (
              SELECT target.id AS target_id, target.data_version,
                     source.id AS source_id, latest_job.id AS job_id
              FROM collection_targets AS target
              LEFT JOIN LATERAL (
                SELECT id FROM collection_sources
                WHERE target_id = target.id ORDER BY created_at, id LIMIT 1
              ) AS source ON TRUE
              LEFT JOIN LATERAL (
                SELECT id FROM sync_jobs
                WHERE target_id = target.id AND parent_job_id IS NULL
                  AND state IN ('completed', 'completed_with_warnings')
                ORDER BY created_at DESC, id DESC LIMIT 1
              ) AS latest_job ON TRUE
              WHERE NOT EXISTS (
                SELECT 1 FROM analysis_runs AS run
                WHERE run.target_id = target.id
                  AND run.data_version = target.data_version
                  AND run.pipeline_version = $1
              )
              ORDER BY target.updated_at DESC, target.id
              LIMIT $2
            )
            INSERT INTO analysis_runs (
              source_id, target_id, job_id, data_version, state,
              pipeline_version, policy_gate_version, sample_plan
            )
            SELECT source_id, target_id, job_id, data_version, 'queued', $1,
                   'server-managed', $3
            FROM candidate ON CONFLICT DO NOTHING
            ",
        )
        .bind(PIPELINE_VERSION)
        .bind(limit)
        .bind(sample_plan())
        .execute(&mut *transaction)
        .await?;
        let inserted = i64::try_from(target_result.rows_affected()).unwrap_or(i64::MAX);
        let source_result = sqlx::query(
            r"
            WITH candidate AS (
              SELECT source.id AS source_id, source.data_version,
                     latest_job.id AS job_id
              FROM collection_sources AS source
              LEFT JOIN LATERAL (
                SELECT id FROM sync_jobs
                WHERE source_id = source.id AND target_id IS NULL
                  AND parent_job_id IS NULL
                  AND state IN ('completed', 'completed_with_warnings')
                ORDER BY created_at DESC, id DESC LIMIT 1
              ) AS latest_job ON TRUE
              WHERE source.target_id IS NULL
                AND NOT EXISTS (
                  SELECT 1 FROM analysis_runs AS run
                  WHERE run.target_id IS NULL AND run.source_id = source.id
                    AND run.data_version = source.data_version
                    AND run.pipeline_version = $1
                )
              ORDER BY source.updated_at DESC, source.id
              LIMIT $2
            )
            INSERT INTO analysis_runs (
              source_id, job_id, data_version, state,
              pipeline_version, policy_gate_version, sample_plan
            )
            SELECT source_id, job_id, data_version, 'queued', $1,
                   'server-managed', $3
            FROM candidate ON CONFLICT DO NOTHING
            ",
        )
        .bind(PIPELINE_VERSION)
        .bind((limit - inserted).max(0))
        .bind(sample_plan())
        .execute(&mut *transaction)
        .await?;
        transaction.commit().await?;
        Ok(target_result
            .rows_affected()
            .saturating_add(source_result.rows_affected()))
    }

    async fn claim(
        &self,
        worker_id: &str,
        lease_seconds: i64,
    ) -> Result<Option<AnalysisRun>, AnalysisError> {
        sqlx::query_as::<_, AnalysisRun>(
            r"
            WITH candidate AS (
              SELECT id FROM analysis_runs
              WHERE (
                (state = 'queued' AND (resume_at IS NULL OR resume_at <= now()))
                OR (state = 'running' AND lease_expires_at <= now())
              )
                AND pipeline_version = $1
              ORDER BY created_at, id
              FOR UPDATE SKIP LOCKED LIMIT 1
            )
            UPDATE analysis_runs AS run
            SET state = 'running', lease_owner = $2,
                lease_expires_at = now() + ($3 * interval '1 second'),
                started_at = COALESCE(started_at, now()), resume_at = NULL,
                last_error = NULL
            FROM candidate
            WHERE run.id = candidate.id
            RETURNING run.id, run.source_id, run.target_id, run.data_version,
                      run.coverage, run.lease_owner
            ",
        )
        .bind(PIPELINE_VERSION)
        .bind(worker_id)
        .bind(lease_seconds)
        .fetch_optional(&self.pool)
        .await
        .map_err(AnalysisError::Database)
    }

    async fn complete(&self, run: &AnalysisRun) -> Result<ScopeAggregate, AnalysisError> {
        if run.target_id.is_none() && run.source_id.is_none() {
            return Err(AnalysisError::MissingScope);
        }
        if self.nlp_pending(run).await? {
            return Err(AnalysisError::NlpNotReady);
        }
        let aggregate = self.aggregate(run).await?;
        let terms = self.top_terms(run).await?;
        let sampled_comments = self.sampled_document_count(run).await?;
        let top_words = terms
            .into_iter()
            .map(|term| json!({"word": term.term, "count": term.term_count.max(0)}))
            .collect::<Vec<_>>();
        let payload = json!({
            "videoCount": aggregate.video_count.max(0),
            "commentCount": aggregate.comment_count.max(0),
            "latestVideoPublishedAt": aggregate.latest_video_published_at,
            "latestCommentPublishedAt": aggregate.latest_comment_published_at,
            "topWords": top_words,
            "generatedAt": Utc::now(),
        });
        let mut coverage = run.coverage.as_object().cloned().unwrap_or_default();
        coverage.insert("sampledComments".to_owned(), Value::from(sampled_comments));
        coverage.insert(
            "totalComments".to_owned(),
            Value::from(aggregate.comment_count.max(0)),
        );
        let ratio = sample_ratio(sampled_comments, aggregate.comment_count);
        coverage.insert("sampleRatio".to_owned(), Value::from(ratio));

        let mut transaction = self.pool.begin().await?;
        sqlx::query(
            r"
            INSERT INTO analysis_results (analysis_run_id, result_kind, payload)
            VALUES ($1, 'basic_summary', $2)
            ON CONFLICT (analysis_run_id, result_kind) WHERE deleted_at IS NULL
            DO UPDATE SET payload = EXCLUDED.payload, created_at = now()
            ",
        )
        .bind(run.id)
        .bind(payload)
        .execute(&mut *transaction)
        .await?;
        let result = sqlx::query(
            r"
            UPDATE analysis_runs
            SET state = 'completed', completed_at = now(), coverage = $1,
                lease_owner = NULL, lease_expires_at = NULL, last_error = NULL
            WHERE id = $2 AND state = 'running' AND lease_owner = $3
            ",
        )
        .bind(Value::Object(coverage))
        .bind(run.id)
        .bind(&run.lease_owner)
        .execute(&mut *transaction)
        .await?;
        if result.rows_affected() != 1 {
            return Err(AnalysisError::LeaseLost);
        }
        transaction.commit().await?;
        Ok(aggregate)
    }

    async fn nlp_pending(&self, run: &AnalysisRun) -> Result<bool, AnalysisError> {
        scope_exists::<bool>(
            &self.pool,
            r"
            SELECT EXISTS (
              SELECT 1
              FROM comments AS comment
              LEFT JOIN nlp_documents AS document
                ON document.source_kind = 'comment' AND document.source_id = comment.id
              WHERE NULLIF(btrim(comment.text_display), '') IS NOT NULL
                AND comment.deleted_at IS NULL
                AND (
                  ($1::uuid IS NOT NULL AND EXISTS (
                    SELECT 1 FROM collection_target_videos AS membership
                    WHERE membership.target_id = $1 AND membership.video_id = comment.video_id
                  )) OR ($1::uuid IS NULL AND EXISTS (
                    SELECT 1 FROM source_videos AS membership
                    WHERE membership.source_id = $2 AND membership.video_id = comment.video_id
                  ))
                )
                AND (document.source_id IS NULL OR document.state <> 'ready')
              LIMIT 1
            )
            ",
            run,
        )
        .await
    }

    async fn aggregate(&self, run: &AnalysisRun) -> Result<ScopeAggregate, AnalysisError> {
        sqlx::query_as::<_, ScopeAggregate>(
            r"
            WITH visible_video AS (
              SELECT video_id FROM collection_target_videos WHERE target_id = $1
              UNION
              SELECT video_id FROM source_videos
              WHERE $1::uuid IS NULL AND source_id = $2
            ), comment_aggregate AS (
              SELECT comment.video_id, count(*)::bigint AS stored_count,
                     max(COALESCE(comment.published_at, comment.source_fetched_at))
                       AS latest_published_at
              FROM comments AS comment
              JOIN visible_video ON visible_video.video_id = comment.video_id
              WHERE comment.deleted_at IS NULL
              GROUP BY comment.video_id
            )
            SELECT count(*)::bigint AS video_count,
                   COALESCE(sum(comment_aggregate.stored_count), 0)::bigint AS comment_count,
                   max(video.published_at) AS latest_video_published_at,
                   max(comment_aggregate.latest_published_at) AS latest_comment_published_at
            FROM visible_video
            JOIN videos AS video ON video.id = visible_video.video_id
            LEFT JOIN comment_aggregate ON comment_aggregate.video_id = video.id
            ",
        )
        .bind(run.target_id)
        .bind(run.source_id)
        .fetch_one(&self.pool)
        .await
        .map_err(AnalysisError::Database)
    }

    async fn top_terms(&self, run: &AnalysisRun) -> Result<Vec<TermAggregate>, AnalysisError> {
        sqlx::query_as::<_, TermAggregate>(
            r"
            WITH visible_video AS (
              SELECT video_id FROM collection_target_videos WHERE target_id = $1
              UNION
              SELECT video_id FROM source_videos
              WHERE $1::uuid IS NULL AND source_id = $2
            ), ranked AS (
              SELECT document.source_id, document.video_id, document.source_date,
                     row_number() OVER (
                       PARTITION BY document.video_id
                       ORDER BY document.source_date DESC NULLS LAST, document.source_id DESC
                     ) AS video_rank
              FROM nlp_documents AS document
              JOIN visible_video ON visible_video.video_id = document.video_id
              WHERE document.source_kind = 'comment' AND document.state = 'ready'
            ), sampled AS (
              SELECT source_id FROM ranked WHERE video_rank <= $3
              ORDER BY video_id, source_date DESC NULLS LAST, source_id DESC LIMIT $4
            )
            SELECT term.term, sum(term.term_frequency)::bigint AS term_count
            FROM sampled
            JOIN nlp_document_terms AS term
              ON term.source_kind = 'comment' AND term.source_id = sampled.source_id
            WHERE NOT (term.term = ANY($5::text[]))
            GROUP BY term.term
            ORDER BY term_count DESC, term.term
            LIMIT $6
            ",
        )
        .bind(run.target_id)
        .bind(run.source_id)
        .bind(MAX_PER_VIDEO)
        .bind(MAX_COMMENTS)
        .bind(STOP_WORDS)
        .bind(TOP_WORD_LIMIT)
        .fetch_all(&self.pool)
        .await
        .map_err(AnalysisError::Database)
    }

    async fn sampled_document_count(&self, run: &AnalysisRun) -> Result<i64, AnalysisError> {
        sqlx::query_scalar::<_, i64>(
            r"
            WITH visible_video AS (
              SELECT video_id FROM collection_target_videos WHERE target_id = $1
              UNION
              SELECT video_id FROM source_videos
              WHERE $1::uuid IS NULL AND source_id = $2
            ), ranked AS (
              SELECT document.source_id, document.video_id, document.source_date,
                     row_number() OVER (
                       PARTITION BY document.video_id
                       ORDER BY document.source_date DESC NULLS LAST, document.source_id DESC
                     ) AS video_rank
              FROM nlp_documents AS document
              JOIN visible_video ON visible_video.video_id = document.video_id
              WHERE document.source_kind = 'comment' AND document.state = 'ready'
            )
            SELECT count(*)::bigint FROM (
              SELECT source_id FROM ranked WHERE video_rank <= $3
              ORDER BY video_id, source_date DESC NULLS LAST, source_id DESC LIMIT $4
            ) AS sampled
            ",
        )
        .bind(run.target_id)
        .bind(run.source_id)
        .bind(MAX_PER_VIDEO)
        .bind(MAX_COMMENTS)
        .fetch_one(&self.pool)
        .await
        .map_err(AnalysisError::Database)
    }

    async fn wait_for_nlp(&self, run: &AnalysisRun, seconds: i64) -> Result<(), AnalysisError> {
        let result = sqlx::query(
            r"
            UPDATE analysis_runs
            SET state = 'queued', resume_at = now() + ($1 * interval '1 second'),
                lease_owner = NULL, lease_expires_at = NULL,
                last_error = 'nlp_indexing_pending'
            WHERE id = $2 AND state = 'running' AND lease_owner = $3
            ",
        )
        .bind(seconds.clamp(5, 300))
        .bind(run.id)
        .bind(&run.lease_owner)
        .execute(&self.pool)
        .await?;
        fenced(result.rows_affected())
    }

    async fn fail(
        &self,
        run: &AnalysisRun,
        error: &str,
        max_retries: i32,
    ) -> Result<String, AnalysisError> {
        sqlx::query_scalar::<_, String>(
            r"
            UPDATE analysis_runs
            SET retry_count = retry_count + 1,
                state = CASE WHEN retry_count + 1 >= $1 THEN 'failed' ELSE 'queued' END,
                resume_at = CASE WHEN retry_count + 1 >= $1 THEN NULL
                  ELSE now() + (power(2, retry_count) * interval '30 seconds') END,
                last_error = $2, lease_owner = NULL, lease_expires_at = NULL,
                completed_at = CASE WHEN retry_count + 1 >= $1 THEN now() ELSE NULL END
            WHERE id = $3 AND state = 'running' AND lease_owner = $4
            RETURNING state
            ",
        )
        .bind(max_retries.clamp(1, 20))
        .bind(error.chars().take(1_000).collect::<String>())
        .bind(run.id)
        .bind(&run.lease_owner)
        .fetch_optional(&self.pool)
        .await?
        .ok_or(AnalysisError::LeaseLost)
    }
}

async fn scope_exists<T>(pool: &PgPool, query: &str, run: &AnalysisRun) -> Result<T, AnalysisError>
where
    for<'row> T: sqlx::Decode<'row, sqlx::Postgres> + sqlx::Type<sqlx::Postgres> + Send + Unpin,
{
    sqlx::query_scalar::<_, T>(query)
        .bind(run.target_id)
        .bind(run.source_id)
        .fetch_one(pool)
        .await
        .map_err(AnalysisError::Database)
}

fn sample_plan() -> Value {
    json!({"strategy": "per-video-recent", "maxComments": MAX_COMMENTS,
           "maxPerVideo": MAX_PER_VIDEO})
}

fn sample_ratio(sampled: i64, total: i64) -> f64 {
    if total <= 0 {
        return 1.0;
    }
    let sampled = u128::try_from(sampled.max(0)).unwrap_or(0);
    let total = u128::try_from(total).unwrap_or(1);
    let basis_points = ((sampled.min(total) * 10_000) + total / 2) / total;
    f64::from(u32::try_from(basis_points).unwrap_or(10_000)) / 10_000.0
}

fn fenced(rows: u64) -> Result<(), AnalysisError> {
    if rows == 1 {
        Ok(())
    } else {
        Err(AnalysisError::LeaseLost)
    }
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

fn parse_u64(
    name: &'static str,
    default: u64,
    minimum: u64,
    maximum: u64,
) -> Result<u64, ConfigError> {
    let parsed = optional(name).map_or(Ok(default), |value| {
        value.parse().map_err(|_| ConfigError::Invalid(name))
    })?;
    if (minimum..=maximum).contains(&parsed) {
        Ok(parsed)
    } else {
        Err(ConfigError::Invalid(name))
    }
}

fn parse_i64(
    name: &'static str,
    default: i64,
    minimum: i64,
    maximum: i64,
) -> Result<i64, ConfigError> {
    let parsed = optional(name).map_or(Ok(default), |value| {
        value.parse().map_err(|_| ConfigError::Invalid(name))
    })?;
    if (minimum..=maximum).contains(&parsed) {
        Ok(parsed)
    } else {
        Err(ConfigError::Invalid(name))
    }
}

#[derive(Debug, Error)]
enum AnalysisError {
    #[error("analysis database operation failed")]
    Database(#[from] sqlx::Error),
    #[error("analysis run lease was lost")]
    LeaseLost,
    #[error("analysis run has no source or target scope")]
    MissingScope,
    #[error("NLP indexing for the analysis scope is not ready")]
    NlpNotReady,
    #[error("analysis seed limit is invalid")]
    InvalidLimit,
}

#[derive(Debug, Error)]
enum ConfigError {
    #[error("missing required environment variable {0}")]
    Missing(&'static str),
    #[error("environment variable {0} is invalid")]
    Invalid(&'static str),
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sample_plan_is_bounded() {
        let plan = sample_plan();
        assert_eq!(plan["maxComments"], MAX_COMMENTS);
        assert_eq!(plan["maxPerVideo"], MAX_PER_VIDEO);
    }

    #[test]
    fn sample_ratio_is_bounded_and_handles_empty_scopes() {
        assert!((sample_ratio(25, 100) - 0.25).abs() < f64::EPSILON);
        assert!((sample_ratio(0, 0) - 1.0).abs() < f64::EPSILON);
        assert!((sample_ratio(200, 100) - 1.0).abs() < f64::EPSILON);
    }
}
