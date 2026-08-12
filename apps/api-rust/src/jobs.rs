//! Owner-scoped collection job read endpoints.

use crate::{
    AppState,
    auth::AuthUser,
    sources::{job_contract, job_contract_for_source, source_scope},
};
use axum::Json;
use axum::extract::{Extension, Path, Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use sqlx::FromRow;
use thiserror::Error;
use uuid::Uuid;

#[derive(Debug, Deserialize)]
pub struct JobListQuery {
    limit: Option<usize>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct JobCreate {
    #[serde(default)]
    include_comments: bool,
    max_videos: Option<i32>,
    max_comments_per_video: Option<i32>,
}

#[derive(Debug, FromRow)]
struct RecentFailureRow {
    public_source_id: Uuid,
    public_target_id: Option<Uuid>,
    public_source_type: String,
    public_source_config: Value,
    public_canonical_key: Option<String>,
    failed_at: DateTime<Utc>,
    job: Value,
    failed_child_count: i64,
    representative_child_pause_reason: Option<String>,
    representative_child_partial_errors: Value,
}

pub async fn get_job(
    State(state): State<AppState>,
    Extension(user): Extension<AuthUser>,
    Path(job_id): Path<String>,
) -> Result<Json<Value>, JobError> {
    let job_id = Uuid::parse_str(&job_id).map_err(|_| JobError::NotFound)?;
    let job = sqlx::query_scalar::<_, Value>(
        r"
        SELECT to_jsonb(job)
        FROM sync_jobs AS job
        LEFT JOIN collection_sources AS source ON source.id = job.source_id
        WHERE job.id = $1
          AND (
            EXISTS (
              SELECT 1 FROM collection_subscriptions AS subscription
              WHERE subscription.target_id = job.target_id
                AND subscription.user_id = $2
            )
            OR (source.target_id IS NULL AND source.owner_id = $2)
          )
        ",
    )
    .bind(job_id)
    .bind(user.id)
    .fetch_optional(&state.pool)
    .await?
    .ok_or(JobError::NotFound)?;
    Ok(Json(job_contract(&job)))
}

pub async fn create_job(
    State(state): State<AppState>,
    Extension(user): Extension<AuthUser>,
    Path(source_id): Path<String>,
    Json(payload): Json<JobCreate>,
) -> Result<(StatusCode, Json<Value>), JobError> {
    if payload
        .max_videos
        .is_some_and(|value| !(1..=5_000).contains(&value))
        || payload
            .max_comments_per_video
            .is_some_and(|value| !(1..=100).contains(&value))
    {
        return Err(JobError::InvalidJobRequest);
    }
    let public_source_id = Uuid::parse_str(&source_id).map_err(|_| JobError::SourceNotFound)?;
    let (target_id, legacy_source_id) = source_scope(&state.pool, user.id, public_source_id)
        .await
        .map_err(|_| JobError::SourceNotFound)?;
    let mut transaction = state.pool.begin().await?;
    let worker_source_id = if let Some(target_id) = target_id {
        let worker_source_id = sqlx::query_scalar::<_, Uuid>(
            r"
            SELECT source.id
            FROM collection_sources AS source
            WHERE source.target_id = $1
            ORDER BY (COALESCE(source.config ->> 'includeComments', 'false') = 'true') DESC,
                     COALESCE((source.config ->> 'maxVideos')::integer, 0) DESC,
                     COALESCE((source.config ->> 'maxPagesPerRun')::integer, 0) DESC,
                     COALESCE((source.config ->> 'maxCommentPagesPerVideo')::integer, 0) DESC,
                     source.created_at,
                     source.id
            LIMIT 1
            FOR UPDATE
            ",
        )
        .bind(target_id)
        .fetch_optional(&mut *transaction)
        .await?
        .ok_or(JobError::WorkerSourceMissing)?;
        let active = sqlx::query_scalar::<_, Value>(
            r"
            SELECT to_jsonb(job)
            FROM sync_jobs AS job
            WHERE job.target_id = $1
              AND job.state IN ('queued', 'running', 'waiting_retry', 'waiting_quota')
            ORDER BY job.created_at
            LIMIT 1
            FOR UPDATE
            ",
        )
        .bind(target_id)
        .fetch_optional(&mut *transaction)
        .await?;
        if let Some(active) = active {
            transaction.commit().await?;
            return Ok((StatusCode::CREATED, Json(job_contract(&active))));
        }
        worker_source_id
    } else {
        legacy_source_id.ok_or(JobError::SourceNotFound)?
    };
    let runtime_config_id = sqlx::query_scalar::<_, Uuid>(
        r"
        SELECT id FROM youtube_runtime_configs
        WHERE status = 'active'
        ORDER BY activated_at DESC
        LIMIT 1
        ",
    )
    .fetch_optional(&mut *transaction)
    .await?
    .ok_or(JobError::RuntimeConfigMissing)?;
    let job = sqlx::query_scalar::<_, Value>(
        r"
        INSERT INTO sync_jobs (
          source_id, target_id, runtime_config_id, state, current_stage,
          idempotency_key, include_comments, max_videos, max_comments_per_video
        )
        VALUES ($1, $2, $3, 'queued', 'queued', $4, $5, $6, $7)
        RETURNING to_jsonb(sync_jobs)
        ",
    )
    .bind(worker_source_id)
    .bind(target_id)
    .bind(runtime_config_id)
    .bind(Uuid::new_v4().to_string())
    .bind(payload.include_comments)
    .bind(payload.max_videos)
    .bind(payload.max_comments_per_video)
    .fetch_one(&mut *transaction)
    .await?;
    transaction.commit().await?;
    Ok((StatusCode::CREATED, Json(job_contract(&job))))
}

pub async fn list_active_jobs(
    State(state): State<AppState>,
    Extension(user): Extension<AuthUser>,
) -> Result<Json<Value>, JobError> {
    let rows = sqlx::query_as::<_, (Uuid, Option<Uuid>, Value)>(
        r"
        WITH active AS (
          SELECT subscription.id AS public_source_id,
                 job.target_id, to_jsonb(job) AS job,
                 job.created_at
          FROM collection_subscriptions AS subscription
          JOIN sync_jobs AS job ON job.target_id = subscription.target_id
          WHERE subscription.user_id = $1
            AND job.parent_job_id IS NULL
            AND job.state NOT IN ('completed', 'completed_with_warnings', 'failed', 'cancelled')

          UNION ALL

          SELECT source.id AS public_source_id,
                 NULL::uuid AS target_id, to_jsonb(job) AS job,
                 job.created_at
          FROM collection_sources AS source
          JOIN sync_jobs AS job ON job.source_id = source.id
          WHERE source.owner_id = $1
            AND source.target_id IS NULL
            AND job.target_id IS NULL
            AND job.parent_job_id IS NULL
            AND job.state NOT IN ('completed', 'completed_with_warnings', 'failed', 'cancelled')
        )
        SELECT public_source_id, target_id, job
        FROM active
        ORDER BY created_at DESC
        ",
    )
    .bind(user.id)
    .fetch_all(&state.pool)
    .await?;
    Ok(Json(json!({
        "jobs": rows.into_iter().map(|(source_id, target_id, job)| json!({
            "sourceId": source_id,
            "targetId": target_id,
            "job": job_contract(&job),
        })).collect::<Vec<_>>()
    })))
}

pub async fn list_recent_failures(
    State(state): State<AppState>,
    Extension(user): Extension<AuthUser>,
    Query(query): Query<JobListQuery>,
) -> Result<Json<Value>, JobError> {
    let limit = validated_limit(query.limit.unwrap_or(10))?;
    let rows = sqlx::query_as::<_, RecentFailureRow>(
        r"
        WITH visible_parent_failures AS (
          SELECT subscription.id AS public_source_id,
                 target.id AS public_target_id,
                 target.type::text AS public_source_type,
                 COALESCE(NULLIF(subscription.display_config, '{}'::jsonb), target.config)
                   AS public_source_config,
                 target.canonical_key AS public_canonical_key,
                 job.updated_at AS failed_at,
                 job.id AS job_id
          FROM collection_subscriptions AS subscription
          JOIN collection_targets AS target ON target.id = subscription.target_id
          JOIN sync_jobs AS job ON job.target_id = target.id
          WHERE subscription.user_id = $1
            AND job.updated_at >= subscription.created_at
            AND job.parent_job_id IS NULL
            AND job.state = 'failed'

          UNION ALL

          SELECT source.id AS public_source_id,
                 NULL::uuid AS public_target_id,
                 source.type::text AS public_source_type,
                 source.config AS public_source_config,
                 NULL::text AS public_canonical_key,
                 job.updated_at AS failed_at,
                 job.id AS job_id
          FROM collection_sources AS source
          JOIN sync_jobs AS job ON job.source_id = source.id
          WHERE source.owner_id = $1
            AND source.target_id IS NULL
            AND job.target_id IS NULL
            AND job.parent_job_id IS NULL
            AND job.state = 'failed'
        ),
        bounded_parent_failures AS MATERIALIZED (
          SELECT *
          FROM visible_parent_failures
          ORDER BY failed_at DESC, job_id DESC
          LIMIT $2
        ),
        ranked_failed_children AS (
          SELECT child.parent_job_id,
                 child.pause_reason AS representative_child_pause_reason,
                 child.partial_errors AS representative_child_partial_errors,
                 count(*) OVER (PARTITION BY child.parent_job_id)::bigint
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
          FROM sync_jobs AS child
          JOIN bounded_parent_failures AS parent
            ON parent.job_id = child.parent_job_id
          WHERE child.state = 'failed'
        )
        SELECT parent.public_source_id,
               parent.public_target_id,
               parent.public_source_type,
               parent.public_source_config,
               parent.public_canonical_key,
               parent.failed_at,
               to_jsonb(job) AS job,
               COALESCE(child.failed_child_count, 0)::bigint AS failed_child_count,
               child.representative_child_pause_reason,
               COALESCE(child.representative_child_partial_errors, '[]'::jsonb)
                 AS representative_child_partial_errors
        FROM bounded_parent_failures AS parent
        JOIN sync_jobs AS job ON job.id = parent.job_id
        LEFT JOIN ranked_failed_children AS child
          ON child.parent_job_id = parent.job_id AND child.failure_rank = 1
        ORDER BY parent.failed_at DESC, parent.job_id DESC
        ",
    )
    .bind(user.id)
    .bind(limit)
    .fetch_all(&state.pool)
    .await?;

    let failures = rows.iter().map(recent_failure_contract).collect::<Vec<_>>();
    Ok(Json(json!({ "failures": failures })))
}

pub async fn list_source_jobs(
    State(state): State<AppState>,
    Extension(user): Extension<AuthUser>,
    Path(source_id): Path<String>,
    Query(query): Query<JobListQuery>,
) -> Result<Json<Vec<Value>>, JobError> {
    let source_id = Uuid::parse_str(&source_id).map_err(|_| JobError::SourceNotFound)?;
    let limit = query.limit.unwrap_or(20);
    if !(1..=50).contains(&limit) {
        return Err(JobError::InvalidLimit);
    }
    let limit = validated_limit(limit)?;
    let jobs = sqlx::query_scalar::<_, Value>(
        r"
        WITH selected_source AS MATERIALIZED (
          SELECT subscription.target_id, NULL::uuid AS legacy_source_id
          FROM collection_subscriptions AS subscription
          WHERE subscription.id = $1 AND subscription.user_id = $2
          UNION ALL
          SELECT NULL::uuid AS target_id, source.id AS legacy_source_id
          FROM collection_sources AS source
          WHERE source.id = $1 AND source.owner_id = $2 AND source.target_id IS NULL
        )
        SELECT to_jsonb(job)
        FROM selected_source AS selected
        JOIN sync_jobs AS job
          ON (selected.target_id IS NOT NULL AND job.target_id = selected.target_id)
          OR (selected.legacy_source_id IS NOT NULL AND job.source_id = selected.legacy_source_id)
        ORDER BY job.updated_at DESC, job.id DESC
        LIMIT $3
        ",
    )
    .bind(source_id)
    .bind(user.id)
    .bind(limit)
    .fetch_all(&state.pool)
    .await?;
    let exists = sqlx::query_scalar::<_, bool>(
        r"
        SELECT EXISTS (
          SELECT 1 FROM collection_subscriptions
          WHERE id = $1 AND user_id = $2
          UNION ALL
          SELECT 1 FROM collection_sources
          WHERE id = $1 AND owner_id = $2 AND target_id IS NULL
        )
        ",
    )
    .bind(source_id)
    .bind(user.id)
    .fetch_one(&state.pool)
    .await?;
    if !exists {
        return Err(JobError::SourceNotFound);
    }
    Ok(Json(jobs.iter().map(job_contract).collect()))
}

fn validated_limit(limit: usize) -> Result<i64, JobError> {
    if !(1..=50).contains(&limit) {
        return Err(JobError::InvalidLimit);
    }
    i64::try_from(limit).map_err(|_| JobError::InvalidLimit)
}

fn recent_failure_contract(row: &RecentFailureRow) -> Value {
    let (reason, error_code, retryable) = failure_details(row);
    json!({
        "sourceId": row.public_source_id,
        "targetId": row.public_target_id,
        "sourceType": row.public_source_type,
        "sourceLabel": source_label(row),
        "failedAt": row.failed_at,
        "reason": reason,
        "errorCode": error_code,
        "retryable": retryable,
        "failedChildCount": row.failed_child_count.max(0),
        "job": job_contract_for_source(&row.job, row.public_source_id),
    })
}

fn source_label(row: &RecentFailureRow) -> &str {
    let key = if row.public_source_type == "keyword" {
        "query"
    } else {
        "input"
    };
    row.public_source_config
        .get(key)
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .or_else(|| {
            row.public_canonical_key
                .as_deref()
                .map(str::trim)
                .filter(|value| !value.is_empty())
        })
        .unwrap_or(&row.public_source_type)
}

fn failure_details(row: &RecentFailureRow) -> (String, Option<String>, Option<bool>) {
    if let Some(reason) = safe_text(row.representative_child_pause_reason.as_deref()) {
        return (reason.to_owned(), None, None);
    }
    let child = structured_failure(&row.representative_child_partial_errors);
    if child.0.is_some() || child.1.is_some() {
        let reason = child
            .0
            .clone()
            .or_else(|| child.1.clone())
            .unwrap_or_else(|| "Collection child failed.".to_owned());
        return (reason, child.1, child.2);
    }
    if let Some(reason) = row
        .job
        .get("pause_reason")
        .and_then(Value::as_str)
        .and_then(|value| safe_text(Some(value)))
    {
        return (reason.to_owned(), None, None);
    }
    let parent = structured_failure(
        row.job
            .get("partial_errors")
            .unwrap_or(&Value::Array(Vec::new())),
    );
    let reason = parent
        .0
        .clone()
        .or_else(|| parent.1.clone())
        .unwrap_or_else(|| "Collection failed without a recorded reason.".to_owned());
    (reason, parent.1, parent.2)
}

fn structured_failure(errors: &Value) -> (Option<String>, Option<String>, Option<bool>) {
    let Some(items) = errors.as_array() else {
        return (None, None, None);
    };
    for item in items {
        let Some(object) = item.as_object() else {
            continue;
        };
        let message = text_field(object, "message");
        let code = text_field(object, "code");
        if message.is_some() || code.is_some() {
            return (
                message.map(str::to_owned),
                code.map(str::to_owned),
                object.get("retryable").and_then(Value::as_bool),
            );
        }
    }
    (None, None, None)
}

fn text_field<'a>(object: &'a Map<String, Value>, key: &str) -> Option<&'a str> {
    object
        .get(key)
        .and_then(Value::as_str)
        .and_then(|value| safe_text(Some(value)))
}

fn safe_text(value: Option<&str>) -> Option<&str> {
    value.map(str::trim).filter(|value| !value.is_empty())
}

#[derive(Debug, Error)]
pub enum JobError {
    #[error("job was not found")]
    NotFound,
    #[error("source was not found")]
    SourceNotFound,
    #[error("job list limit is invalid")]
    InvalidLimit,
    #[error("job request is invalid")]
    InvalidJobRequest,
    #[error("target has no worker source")]
    WorkerSourceMissing,
    #[error("active runtime configuration is missing")]
    RuntimeConfigMissing,
    #[error("job database operation failed")]
    Database(#[from] sqlx::Error),
}

impl IntoResponse for JobError {
    fn into_response(self) -> Response {
        let (status, detail, retryable) = match self {
            Self::NotFound => (StatusCode::NOT_FOUND, "Job was not found", false),
            Self::SourceNotFound => (StatusCode::NOT_FOUND, "Source was not found", false),
            Self::InvalidLimit => (
                StatusCode::UNPROCESSABLE_ENTITY,
                "limit must be between 1 and 50",
                false,
            ),
            Self::InvalidJobRequest => (
                StatusCode::UNPROCESSABLE_ENTITY,
                "Job limits are invalid",
                false,
            ),
            Self::WorkerSourceMissing => (
                StatusCode::CONFLICT,
                "Collection target has no worker source",
                false,
            ),
            Self::RuntimeConfigMissing => (
                StatusCode::SERVICE_UNAVAILABLE,
                "No active YouTube runtime configuration is available",
                true,
            ),
            Self::Database(error) => {
                let failure = crate::db_error::classify(&error, "job");
                (failure.status, failure.detail, failure.retryable)
            }
        };
        (status, Json(JobErrorResponse { detail, retryable })).into_response()
    }
}

#[derive(Serialize)]
struct JobErrorResponse {
    detail: &'static str,
    #[serde(skip_serializing_if = "crate::is_false")]
    retryable: bool,
}

#[cfg(test)]
mod tests {
    use super::{RecentFailureRow, failure_details, recent_failure_contract};
    use chrono::Utc;
    use serde_json::json;
    use uuid::Uuid;

    fn failure_row() -> RecentFailureRow {
        RecentFailureRow {
            public_source_id: Uuid::new_v4(),
            public_target_id: Some(Uuid::new_v4()),
            public_source_type: "video".to_owned(),
            public_source_config: json!({"input": "video-id"}),
            public_canonical_key: Some("ignored".to_owned()),
            failed_at: Utc::now(),
            job: json!({
                "id": Uuid::new_v4(),
                "state": "failed",
                "current_stage": "failed",
                "progress_completed": 0,
                "progress_total": null,
                "progress_unit": "sources",
                "pause_reason": "parent fallback",
                "partial_errors": [{
                    "scope": "source",
                    "sourceId": "private-source-id",
                    "code": "parent_error",
                    "retryable": true
                }]
            }),
            failed_child_count: 1,
            representative_child_pause_reason: None,
            representative_child_partial_errors: json!([{
                "message": "child detail",
                "code": "child_error",
                "retryable": false
            }]),
        }
    }

    #[test]
    fn recent_failure_prefers_child_detail_and_redacts_worker_source_id() {
        let row = failure_row();
        let (reason, code, retryable) = failure_details(&row);
        assert_eq!(reason, "child detail");
        assert_eq!(code.as_deref(), Some("child_error"));
        assert_eq!(retryable, Some(false));
        let contract = recent_failure_contract(&row);
        assert_eq!(contract["sourceLabel"], "video-id");
        assert_eq!(
            contract["job"]["partialErrors"][0]["sourceId"],
            row.public_source_id.to_string()
        );
        assert!(!contract.to_string().contains("private-source-id"));
    }
}
