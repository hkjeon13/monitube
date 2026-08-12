//! Atomic shared-target collection submission and refresh coordination.

use crate::AppState;
use crate::auth::AuthUser;
use crate::sources::{
    SourceError, canonical_source_config, job_contract, load_source_contract, sync_target_pin,
};
use axum::Json;
use axum::extract::{Extension, Path, State};
use axum::http::{HeaderMap, StatusCode, header::HeaderName};
use axum::response::{IntoResponse, Response};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use sqlx::{FromRow, Postgres, Transaction};
use thiserror::Error;
use uuid::Uuid;

const IDEMPOTENCY_KEY: HeaderName = HeaderName::from_static("idempotency-key");

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SourceCreate {
    #[serde(rename = "type")]
    source_type: String,
    config: Value,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
pub struct CollectionRequestCreate {
    #[serde(rename = "type")]
    source_type: String,
    config: Value,
    #[serde(default)]
    force_refresh: bool,
}

#[derive(Debug, FromRow)]
struct TargetRow {
    id: Uuid,
    source_type: String,
    config: Value,
    coverage: Value,
}

#[derive(Debug, FromRow)]
struct RequestRow {
    id: Uuid,
    target_id: Uuid,
    subscription_id: Option<Uuid>,
    job_id: Option<Uuid>,
    status: String,
}

struct SubmissionInput {
    source_type: String,
    config: Value,
    force_refresh: bool,
    idempotency_key: Option<String>,
}

pub async fn create_source(
    State(state): State<AppState>,
    Extension(user): Extension<AuthUser>,
    Json(payload): Json<SourceCreate>,
) -> Result<(StatusCode, Json<Value>), CollectionError> {
    let response = submit(
        &state,
        user.id,
        SubmissionInput {
            source_type: payload.source_type,
            config: payload.config,
            force_refresh: false,
            idempotency_key: None,
        },
    )
    .await?;
    Ok((StatusCode::CREATED, Json(response["source"].clone())))
}

pub async fn submit_request(
    State(state): State<AppState>,
    Extension(user): Extension<AuthUser>,
    headers: HeaderMap,
    Json(payload): Json<CollectionRequestCreate>,
) -> Result<(StatusCode, Json<Value>), CollectionError> {
    let idempotency_key = read_idempotency_key(&headers)?;
    let response = submit(
        &state,
        user.id,
        SubmissionInput {
            source_type: payload.source_type,
            config: payload.config,
            force_refresh: payload.force_refresh,
            idempotency_key,
        },
    )
    .await?;
    Ok((StatusCode::CREATED, Json(response)))
}

pub async fn refresh_source(
    State(state): State<AppState>,
    Extension(user): Extension<AuthUser>,
    Path(source_id): Path<String>,
    headers: HeaderMap,
) -> Result<(StatusCode, Json<Value>), CollectionError> {
    let source_id = Uuid::parse_str(&source_id).map_err(|_| CollectionError::SourceNotFound)?;
    let source = load_source_contract(&state.pool, user.id, source_id)
        .await
        .map_err(CollectionError::Source)?;
    let source_type = source
        .get("type")
        .and_then(Value::as_str)
        .ok_or(CollectionError::InvalidSourceType)?;
    if source_type == "video" {
        return Err(CollectionError::VideoRefreshUnsupported);
    }
    let config = source
        .get("config")
        .cloned()
        .ok_or(CollectionError::InvalidConfig)?;
    let response = submit(
        &state,
        user.id,
        SubmissionInput {
            source_type: source_type.to_owned(),
            config,
            force_refresh: true,
            idempotency_key: read_idempotency_key(&headers)?,
        },
    )
    .await?;
    if matches!(
        response.get("disposition").and_then(Value::as_str),
        Some("queued" | "successor_queued")
    ) {
        let target_id = response
            .get("targetId")
            .and_then(Value::as_str)
            .and_then(|value| Uuid::parse_str(value).ok())
            .ok_or(CollectionError::TargetTypeMismatch)?;
        sqlx::query(
            r"
            UPDATE collection_target_pins
            SET last_dispatched_at = now(),
                next_run_at = now() + (interval_minutes * interval '1 minute'),
                updated_at = now()
            WHERE target_id = $1 AND enabled = TRUE
            ",
        )
        .bind(target_id)
        .execute(&state.pool)
        .await?;
    }
    Ok((StatusCode::ACCEPTED, Json(response)))
}

#[allow(clippy::too_many_lines)]
async fn submit(
    state: &AppState,
    user_id: Uuid,
    input: SubmissionInput,
) -> Result<Value, CollectionError> {
    validate_source_type(&input.source_type)?;
    let config = canonical_source_config(&input.source_type, input.config)
        .map_err(CollectionError::Source)?;
    let (mut canonical_key, aliases) = canonical_identity(&input.source_type, &config)?;
    let mut transaction = state.pool.begin().await?;
    let mut target = None;
    for (kind, value) in &aliases {
        target = sqlx::query_as::<_, TargetRow>(
            r"
            SELECT target.id,
                   target.type::text AS source_type,
                   target.config,
                   target.coverage
            FROM collection_target_aliases AS alias
            JOIN collection_targets AS target ON target.id = alias.target_id
            WHERE alias.target_type = $1::collection_source_type
              AND alias.alias_kind = $2
              AND alias.alias_value = $3
            FOR UPDATE OF target
            ",
        )
        .bind(&input.source_type)
        .bind(kind)
        .bind(value)
        .fetch_optional(&mut *transaction)
        .await?;
        if target.is_some() {
            break;
        }
    }
    if target.is_none() && input.source_type == "channel" {
        if let Some((_, handle)) = aliases.iter().find(|(kind, _)| kind == "handle") {
            if let Some(channel_id) = sqlx::query_scalar::<_, String>(
                r"
                SELECT youtube_channel_id FROM channels
                WHERE lower(handle) = lower($1)
                ORDER BY source_fetched_at DESC NULLS LAST
                LIMIT 1
                ",
            )
            .bind(handle)
            .fetch_optional(&mut *transaction)
            .await?
            {
                canonical_key = format!("channel:{channel_id}");
            }
        }
    }
    let mut target = if let Some(target) = target {
        target
    } else {
        sqlx::query_as::<_, TargetRow>(
            r"
            INSERT INTO collection_targets (type, canonical_key, config)
            VALUES ($1::collection_source_type, $2, $3)
            ON CONFLICT (type, canonical_key) DO UPDATE SET updated_at = now()
            RETURNING id, type::text AS source_type, config, coverage
            ",
        )
        .bind(&input.source_type)
        .bind(&canonical_key)
        .bind(&config)
        .fetch_one(&mut *transaction)
        .await?
    };
    if target.source_type != input.source_type {
        return Err(CollectionError::TargetTypeMismatch);
    }

    if let Some(key) = input.idempotency_key.as_deref() {
        let replay = sqlx::query_as::<_, RequestRow>(
            r"
            SELECT id, target_id, subscription_id, job_id, status
            FROM collection_requests
            WHERE target_id = $1
              AND user_id = $2
              AND idempotency_key = $3
            ORDER BY created_at
            LIMIT 1
            FOR UPDATE
            ",
        )
        .bind(target.id)
        .bind(user_id)
        .bind(key)
        .fetch_optional(&mut *transaction)
        .await?;
        if let Some(replay) = replay {
            transaction.commit().await?;
            return submission_contract(state, user_id, replay).await;
        }
    }

    for (kind, value) in &aliases {
        sqlx::query(
            r"
            INSERT INTO collection_target_aliases (
              target_id, target_type, alias_kind, alias_value
            )
            VALUES ($1, $2::collection_source_type, $3, $4)
            ON CONFLICT (target_type, alias_kind, alias_value) DO NOTHING
            ",
        )
        .bind(target.id)
        .bind(&input.source_type)
        .bind(kind)
        .bind(value)
        .execute(&mut *transaction)
        .await?;
    }

    let worker = sqlx::query_as::<_, (Uuid, Value)>(
        r"
        SELECT source.id, source.config
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
    .bind(target.id)
    .fetch_optional(&mut *transaction)
    .await?;
    let source_is_new = worker.is_none();
    let (worker_source_id, prior_config) = if let Some(worker) = worker {
        worker
    } else {
        sqlx::query_as::<_, (Uuid, Value)>(
            r"
            INSERT INTO collection_sources (type, config, target_id)
            VALUES ($1::collection_source_type, $2, $3)
            RETURNING id, config
            ",
        )
        .bind(&input.source_type)
        .bind(&config)
        .bind(target.id)
        .fetch_one(&mut *transaction)
        .await?
    };
    let merged = merge_config(
        &input.source_type,
        &merge_config(&input.source_type, &prior_config, &target.config),
        &config,
    );
    sqlx::query("UPDATE collection_sources SET config = $1, updated_at = now() WHERE id = $2")
        .bind(&merged)
        .bind(worker_source_id)
        .execute(&mut *transaction)
        .await?;
    target = sqlx::query_as::<_, TargetRow>(
        r"
        UPDATE collection_targets
        SET config = $1, updated_at = now()
        WHERE id = $2
        RETURNING id, type::text AS source_type, config, coverage
        ",
    )
    .bind(&merged)
    .bind(target.id)
    .fetch_one(&mut *transaction)
    .await?;

    let subscription_id = sqlx::query_scalar::<_, Uuid>(
        r"
        INSERT INTO collection_subscriptions (
          user_id, target_id, display_config, enabled
        )
        VALUES ($1, $2, $3, TRUE)
        ON CONFLICT (user_id, target_id) DO UPDATE
        SET display_config = CASE
              WHEN EXCLUDED.display_config = '{}'::jsonb
                THEN collection_subscriptions.display_config
              ELSE EXCLUDED.display_config
            END,
            enabled = TRUE,
            updated_at = now()
        RETURNING id
        ",
    )
    .bind(user_id)
    .bind(target.id)
    .bind(&config)
    .fetch_one(&mut *transaction)
    .await?;
    sync_target_pin(&mut transaction, target.id).await?;

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
    .bind(target.id)
    .fetch_optional(&mut *transaction)
    .await?;
    let desired = desired_coverage(&input.source_type, &config);
    let mut request_status = "queued";
    let mut request_job_id = None;
    if !input.force_refresh && coverage_satisfies(&target.coverage, &desired) {
        request_status = "completed";
    } else if active.as_ref().is_some_and(|job| {
        coverage_satisfies(
            &job_coverage(job, &input.source_type, &prior_config),
            &desired,
        )
    }) {
        request_status = "joined";
        request_job_id = active.as_ref().and_then(job_id);
    } else if active
        .as_ref()
        .and_then(|job| job.get("state"))
        .and_then(Value::as_str)
        == Some("queued")
    {
        let widened = desired_coverage(&input.source_type, &merged);
        let active_id = active
            .as_ref()
            .and_then(job_id)
            .ok_or(CollectionError::InvalidActiveJob)?;
        sqlx::query(
            r"
            UPDATE sync_jobs
            SET include_comments = $1,
                max_videos = $2,
                max_comments_per_video = $3,
                updated_at = now()
            WHERE id = $4
            ",
        )
        .bind(bool_field(&widened, "includeComments"))
        .bind(integer_field(&widened, "maxVideos"))
        .bind(integer_field(&widened, "maxCommentPagesPerVideo"))
        .bind(active_id)
        .execute(&mut *transaction)
        .await?;
        request_job_id = Some(active_id);
    } else if active.is_none() {
        request_job_id = Some(
            create_target_job(
                &mut transaction,
                target.id,
                worker_source_id,
                &input.source_type,
                &merged,
            )
            .await?,
        );
    }

    let request = sqlx::query_as::<_, RequestRow>(
        r"
        INSERT INTO collection_requests (
          target_id, source_id, request_config, idempotency_key, job_id,
          status, user_id, subscription_id
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        RETURNING id, target_id, subscription_id, job_id, status
        ",
    )
    .bind(target.id)
    .bind(source_is_new.then_some(worker_source_id))
    .bind(&config)
    .bind(input.idempotency_key)
    .bind(request_job_id)
    .bind(request_status)
    .bind(user_id)
    .bind(subscription_id)
    .fetch_one(&mut *transaction)
    .await?;
    transaction.commit().await?;
    submission_contract(state, user_id, request).await
}

async fn create_target_job(
    transaction: &mut Transaction<'_, Postgres>,
    target_id: Uuid,
    worker_source_id: Uuid,
    source_type: &str,
    config: &Value,
) -> Result<Uuid, CollectionError> {
    let runtime_config_id = sqlx::query_scalar::<_, Uuid>(
        r"
        SELECT id FROM youtube_runtime_configs
        WHERE status = 'active'
        ORDER BY activated_at DESC
        LIMIT 1
        ",
    )
    .fetch_optional(&mut **transaction)
    .await?
    .ok_or(CollectionError::RuntimeConfigMissing)?;
    let desired = desired_coverage(source_type, config);
    sqlx::query_scalar::<_, Uuid>(
        r"
        INSERT INTO sync_jobs (
          source_id, target_id, runtime_config_id, state, current_stage,
          idempotency_key, include_comments, max_videos, max_comments_per_video
        )
        VALUES ($1, $2, $3, 'queued', 'queued', $4, $5, $6, $7)
        RETURNING id
        ",
    )
    .bind(worker_source_id)
    .bind(target_id)
    .bind(runtime_config_id)
    .bind(Uuid::new_v4().to_string())
    .bind(bool_field(&desired, "includeComments"))
    .bind(integer_field(&desired, "maxVideos"))
    .bind(integer_field(&desired, "maxCommentPagesPerVideo"))
    .fetch_one(&mut **transaction)
    .await
    .map_err(CollectionError::Database)
}

async fn submission_contract(
    state: &AppState,
    user_id: Uuid,
    request: RequestRow,
) -> Result<Value, CollectionError> {
    let subscription_id = if let Some(subscription_id) = request.subscription_id {
        subscription_id
    } else {
        sqlx::query_scalar::<_, Uuid>(
            r"
            SELECT id FROM collection_subscriptions
            WHERE user_id = $1 AND target_id = $2
            ",
        )
        .bind(user_id)
        .bind(request.target_id)
        .fetch_optional(&state.pool)
        .await?
        .ok_or(CollectionError::SubscriptionMissing)?
    };
    let source = load_source_contract(&state.pool, user_id, subscription_id)
        .await
        .map_err(CollectionError::Source)?;
    let job = if let Some(job_id) = request.job_id {
        sqlx::query_scalar::<_, Value>("SELECT to_jsonb(job) FROM sync_jobs AS job WHERE id = $1")
            .bind(job_id)
            .fetch_optional(&state.pool)
            .await?
            .map(|job| job_contract(&job))
    } else {
        None
    };
    let disposition = if request.job_id.is_none() && request.status == "queued" {
        "successor_queued"
    } else if request.status == "completed" {
        "cached"
    } else if request.status == "joined" {
        "joined"
    } else {
        "queued"
    };
    Ok(json!({
        "id": request.id,
        "disposition": disposition,
        "targetId": request.target_id,
        "source": source,
        "job": job,
    }))
}

fn canonical_identity(
    source_type: &str,
    config: &Value,
) -> Result<(String, Vec<(String, String)>), CollectionError> {
    match source_type {
        "channel" => {
            let input = text(config, "input").ok_or(CollectionError::InvalidConfig)?;
            let (kind, normalized) = crate::resolution::channel_identity(input)
                .map_err(|_| CollectionError::InvalidConfig)?;
            if kind == "channel_id" {
                Ok((
                    format!("channel:{normalized}"),
                    vec![
                        ("channel_id".to_owned(), normalized.clone()),
                        ("input".to_owned(), normalized),
                    ],
                ))
            } else {
                let lowered = normalized.to_lowercase();
                Ok((
                    format!("channel:{kind}:{lowered}"),
                    vec![
                        (kind.to_owned(), lowered.clone()),
                        ("input".to_owned(), lowered),
                    ],
                ))
            }
        }
        "video" => {
            let video_id = text(config, "input")
                .ok_or(CollectionError::InvalidConfig)?
                .to_owned();
            Ok((
                format!("video:{video_id}"),
                vec![
                    ("video_id".to_owned(), video_id.clone()),
                    ("input".to_owned(), video_id),
                ],
            ))
        }
        "keyword" => {
            let material = [
                config
                    .get("query")
                    .and_then(Value::as_str)
                    .map(|value| {
                        value
                            .split_whitespace()
                            .collect::<Vec<_>>()
                            .join(" ")
                            .to_lowercase()
                    })
                    .unwrap_or_default(),
                text(config, "publishedAfter")
                    .unwrap_or_default()
                    .to_owned(),
                text(config, "publishedBefore")
                    .unwrap_or_default()
                    .to_owned(),
                text(config, "regionCode")
                    .unwrap_or_default()
                    .to_uppercase(),
                text(config, "relevanceLanguage")
                    .unwrap_or_default()
                    .to_lowercase(),
                text(config, "order").unwrap_or("date").to_owned(),
            ]
            .join("\u{1f}");
            let fingerprint = hex::encode(Sha256::digest(material.as_bytes()));
            Ok((
                format!("keyword:{fingerprint}"),
                vec![("keyword".to_owned(), fingerprint)],
            ))
        }
        _ => Err(CollectionError::InvalidSourceType),
    }
}

fn merge_config(source_type: &str, current: &Value, incoming: &Value) -> Value {
    let mut merged = current.as_object().cloned().unwrap_or_default();
    if let Some(incoming) = incoming.as_object() {
        for (key, value) in incoming {
            merged.entry(key.clone()).or_insert_with(|| value.clone());
        }
    }
    let current = current.as_object().cloned().unwrap_or_default();
    let incoming = incoming.as_object().cloned().unwrap_or_default();
    merged.insert(
        "includeComments".to_owned(),
        Value::Bool(
            map_bool(&current, "includeComments") || map_bool(&incoming, "includeComments"),
        ),
    );
    merged.insert(
        "collectAllComments".to_owned(),
        Value::Bool(
            map_bool(&current, "collectAllComments") || map_bool(&incoming, "collectAllComments"),
        ),
    );
    merged.insert(
        "maxCommentPagesPerVideo".to_owned(),
        Value::from(
            map_integer(&current, "maxCommentPagesPerVideo", 1).max(map_integer(
                &incoming,
                "maxCommentPagesPerVideo",
                1,
            )),
        ),
    );
    if source_type == "channel" {
        merged.insert(
            "collectAllVideos".to_owned(),
            Value::Bool(
                map_bool(&current, "collectAllVideos") || map_bool(&incoming, "collectAllVideos"),
            ),
        );
        merged.insert(
            "maxVideos".to_owned(),
            Value::from(map_integer(&current, "maxVideos", 1).max(map_integer(
                &incoming,
                "maxVideos",
                1,
            ))),
        );
    } else if source_type == "keyword" {
        merged.insert(
            "maxPagesPerRun".to_owned(),
            Value::from(map_integer(&current, "maxPagesPerRun", 1).max(map_integer(
                &incoming,
                "maxPagesPerRun",
                1,
            ))),
        );
    }
    Value::Object(merged)
}

fn desired_coverage(source_type: &str, config: &Value) -> Value {
    let include_comments = bool_field(config, "includeComments");
    let mut desired = Map::from_iter([
        ("complete".to_owned(), Value::Bool(false)),
        ("includeComments".to_owned(), Value::Bool(include_comments)),
        (
            "collectAllComments".to_owned(),
            Value::Bool(include_comments && bool_field(config, "collectAllComments")),
        ),
        (
            "maxCommentPagesPerVideo".to_owned(),
            Value::from(integer_field(config, "maxCommentPagesPerVideo").unwrap_or(1)),
        ),
    ]);
    if source_type == "channel" {
        desired.insert(
            "collectAllVideos".to_owned(),
            Value::Bool(bool_field(config, "collectAllVideos")),
        );
        desired.insert(
            "maxVideos".to_owned(),
            Value::from(integer_field(config, "maxVideos").unwrap_or(50)),
        );
    } else if source_type == "keyword" {
        desired.insert(
            "maxPagesPerRun".to_owned(),
            Value::from(integer_field(config, "maxPagesPerRun").unwrap_or(1)),
        );
        desired.insert("historicalBackfillComplete".to_owned(), Value::Bool(true));
    }
    Value::Object(desired)
}

fn job_coverage(job: &Value, source_type: &str, source_config: &Value) -> Value {
    let include_comments = bool_field(job, "include_comments");
    let mut coverage = Map::from_iter([
        ("complete".to_owned(), Value::Bool(false)),
        ("includeComments".to_owned(), Value::Bool(include_comments)),
        (
            "collectAllComments".to_owned(),
            Value::Bool(include_comments && bool_field(source_config, "collectAllComments")),
        ),
        (
            "maxCommentPagesPerVideo".to_owned(),
            Value::from(
                integer_field(job, "max_comments_per_video")
                    .or_else(|| integer_field(source_config, "maxCommentPagesPerVideo"))
                    .unwrap_or(1),
            ),
        ),
    ]);
    if source_type == "channel" {
        coverage.insert(
            "collectAllVideos".to_owned(),
            Value::Bool(bool_field(source_config, "collectAllVideos")),
        );
        coverage.insert(
            "maxVideos".to_owned(),
            Value::from(
                integer_field(job, "max_videos")
                    .or_else(|| integer_field(source_config, "maxVideos"))
                    .unwrap_or(50),
            ),
        );
    } else if source_type == "keyword" {
        coverage.insert(
            "maxPagesPerRun".to_owned(),
            Value::from(integer_field(source_config, "maxPagesPerRun").unwrap_or(1)),
        );
        coverage.insert(
            "historicalBackfillComplete".to_owned(),
            Value::Bool(job.get("checkpoint").is_some_and(|checkpoint| {
                bool_field(checkpoint, "keywordHistoricalBackfillComplete")
            })),
        );
    }
    Value::Object(coverage)
}

fn coverage_satisfies(coverage: &Value, desired: &Value) -> bool {
    if !bool_field(coverage, "complete") {
        return false;
    }
    for field in [
        "includeComments",
        "collectAllComments",
        "collectAllVideos",
        "historicalBackfillComplete",
    ] {
        if bool_field(desired, field) && !bool_field(coverage, field) {
            return false;
        }
    }
    for field in ["maxVideos", "maxPagesPerRun"] {
        if let Some(required) = integer_field(desired, field) {
            if integer_field(coverage, field).unwrap_or(0) < required {
                return false;
            }
        }
    }
    !bool_field(desired, "includeComments")
        || integer_field(coverage, "maxCommentPagesPerVideo").unwrap_or(0)
            >= integer_field(desired, "maxCommentPagesPerVideo").unwrap_or(1)
}

fn validate_source_type(source_type: &str) -> Result<(), CollectionError> {
    if matches!(source_type, "channel" | "keyword" | "video") {
        Ok(())
    } else {
        Err(CollectionError::InvalidSourceType)
    }
}

fn read_idempotency_key(headers: &HeaderMap) -> Result<Option<String>, CollectionError> {
    headers.get(&IDEMPOTENCY_KEY).map_or(Ok(None), |value| {
        let value = value
            .to_str()
            .map_err(|_| CollectionError::InvalidIdempotencyKey)?
            .trim()
            .to_owned();
        if value.len() > 255 {
            return Err(CollectionError::InvalidIdempotencyKey);
        }
        Ok(Some(value))
    })
}

fn job_id(job: &Value) -> Option<Uuid> {
    job.get("id")
        .and_then(Value::as_str)
        .and_then(|value| Uuid::parse_str(value).ok())
}

fn text<'a>(value: &'a Value, field: &str) -> Option<&'a str> {
    value.get(field).and_then(Value::as_str)
}

fn bool_field(value: &Value, field: &str) -> bool {
    value.get(field).and_then(Value::as_bool).unwrap_or(false)
}

fn integer_field(value: &Value, field: &str) -> Option<i64> {
    value.get(field).and_then(Value::as_i64)
}

fn map_bool(value: &Map<String, Value>, field: &str) -> bool {
    value.get(field).and_then(Value::as_bool).unwrap_or(false)
}

fn map_integer(value: &Map<String, Value>, field: &str, default: i64) -> i64 {
    value.get(field).and_then(Value::as_i64).unwrap_or(default)
}

#[derive(Debug, Error)]
pub enum CollectionError {
    #[error("source was not found")]
    SourceNotFound,
    #[error("source operation failed")]
    Source(#[from] SourceError),
    #[error("source type is invalid")]
    InvalidSourceType,
    #[error("source config is invalid")]
    InvalidConfig,
    #[error("idempotency key is invalid")]
    InvalidIdempotencyKey,
    #[error("manual video refresh is unsupported")]
    VideoRefreshUnsupported,
    #[error("target type mismatch")]
    TargetTypeMismatch,
    #[error("collection subscription is missing")]
    SubscriptionMissing,
    #[error("active job is invalid")]
    InvalidActiveJob,
    #[error("active runtime configuration is missing")]
    RuntimeConfigMissing,
    #[error("collection database operation failed")]
    Database(#[from] sqlx::Error),
}

impl IntoResponse for CollectionError {
    fn into_response(self) -> Response {
        if let Self::Source(error) = self {
            return error.into_response();
        }
        let (status, detail, retryable) = match self {
            Self::Source(_) => unreachable!(),
            Self::SourceNotFound => (StatusCode::NOT_FOUND, "Source was not found", false),
            Self::InvalidSourceType => (
                StatusCode::UNPROCESSABLE_ENTITY,
                "type must be channel, keyword, or video",
                false,
            ),
            Self::InvalidConfig => (
                StatusCode::UNPROCESSABLE_ENTITY,
                "Source configuration is invalid",
                false,
            ),
            Self::InvalidIdempotencyKey => (
                StatusCode::UNPROCESSABLE_ENTITY,
                "Idempotency-Key must be at most 255 characters",
                false,
            ),
            Self::VideoRefreshUnsupported => (
                StatusCode::UNPROCESSABLE_ENTITY,
                "Manual refresh is available for channel and keyword sources",
                false,
            ),
            Self::TargetTypeMismatch | Self::SubscriptionMissing | Self::InvalidActiveJob => (
                StatusCode::CONFLICT,
                "Collection target state is inconsistent",
                false,
            ),
            Self::RuntimeConfigMissing => (
                StatusCode::SERVICE_UNAVAILABLE,
                "No active YouTube runtime configuration is available",
                true,
            ),
            Self::Database(error) => {
                let failure = crate::db_error::classify(&error, "collection");
                (failure.status, failure.detail, failure.retryable)
            }
        };
        (status, Json(CollectionErrorResponse { detail, retryable })).into_response()
    }
}

#[derive(Serialize)]
struct CollectionErrorResponse {
    detail: &'static str,
    #[serde(skip_serializing_if = "crate::is_false")]
    retryable: bool,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn config_merge_only_widens_collection_coverage() {
        let current = json!({
            "input": "UC123",
            "includeComments": false,
            "maxVideos": 20,
            "maxCommentPagesPerVideo": 1
        });
        let incoming = json!({
            "input": "UC123",
            "includeComments": true,
            "maxVideos": 10,
            "maxCommentPagesPerVideo": 4
        });
        let merged = merge_config("channel", &current, &incoming);
        assert_eq!(merged["includeComments"], true);
        assert_eq!(merged["maxVideos"], 20);
        assert_eq!(merged["maxCommentPagesPerVideo"], 4);
    }

    #[test]
    fn complete_coverage_must_meet_requested_frequency_independent_breadth() {
        let coverage = json!({
            "complete": true,
            "includeComments": true,
            "maxCommentPagesPerVideo": 2,
            "maxVideos": 100
        });
        let desired = json!({
            "complete": false,
            "includeComments": true,
            "maxCommentPagesPerVideo": 2,
            "maxVideos": 50
        });
        assert!(coverage_satisfies(&coverage, &desired));
    }
}
