//! User-visible collection source read models.

use crate::{AppState, auth::AuthUser};
use axum::Json;
use axum::extract::{Extension, Path, Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use base64::Engine;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use sqlx::{FromRow, PgPool, Postgres, Transaction};
use thiserror::Error;
use uuid::Uuid;

#[derive(Debug, FromRow)]
struct SourceRow {
    id: Uuid,
    source_type: String,
    config: Value,
    enabled: bool,
    next_run_at: Option<DateTime<Utc>>,
    target_id: Option<Uuid>,
    canonical_key: Option<String>,
    coverage: Value,
    last_completed_at: Option<DateTime<Utc>>,
    latest_job: Option<Value>,
    stored_video_count: i64,
    stored_comment_count: i64,
    reported_comment_count: i64,
}

#[derive(Debug, FromRow)]
struct VideoRow {
    youtube_video_id: String,
    youtube_channel_id: Option<String>,
    title: Option<String>,
    description: Option<String>,
    published_at: Option<DateTime<Utc>>,
    duration_seconds: Option<i32>,
    privacy_status: Option<String>,
    made_for_kids: Option<bool>,
    source_fetched_at: DateTime<Utc>,
    statistics: Value,
}

#[derive(Debug, Deserialize)]
pub struct SourceVideosQuery {
    cursor: Option<String>,
    limit: Option<usize>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
pub struct SourceUpdate {
    enabled: Option<bool>,
    config: Option<Value>,
    next_run_at: Option<DateTime<Utc>>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
struct ChannelSourceConfig {
    input: String,
    #[serde(default)]
    include_comments: bool,
    #[serde(default)]
    collect_all_videos: bool,
    #[serde(default)]
    collect_all_comments: bool,
    #[serde(default = "default_max_videos")]
    max_videos: i32,
    #[serde(default = "default_page_limit")]
    max_comment_pages_per_video: i32,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
struct KeywordSourceConfig {
    query: String,
    published_after: Option<String>,
    published_before: Option<String>,
    region_code: Option<String>,
    relevance_language: Option<String>,
    #[serde(default = "default_keyword_order")]
    order: String,
    #[serde(default = "default_page_limit")]
    max_pages_per_run: i32,
    #[serde(default)]
    include_comments: bool,
    #[serde(default)]
    collect_all_comments: bool,
    #[serde(default = "default_page_limit")]
    max_comment_pages_per_video: i32,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
struct VideoSourceConfig {
    input: String,
    #[serde(default)]
    include_comments: bool,
    #[serde(default)]
    collect_all_comments: bool,
    #[serde(default = "default_page_limit")]
    max_comment_pages_per_video: i32,
}

const fn default_max_videos() -> i32 {
    50
}

const fn default_page_limit() -> i32 {
    1
}

fn default_keyword_order() -> String {
    "date".to_owned()
}

#[derive(Debug, Serialize, Deserialize)]
struct SourceVideoCursor {
    v: u8,
    at: DateTime<Utc>,
    id: String,
    snapshot: DateTime<Utc>,
    scope: String,
    filter: String,
    sort: String,
}

const SOURCE_VIDEO_SORT: &str = "effective_published_desc";
const SOURCE_VIDEO_FILTER_HASH: &str =
    "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a";

pub async fn list_sources(
    State(state): State<AppState>,
    Extension(user): Extension<AuthUser>,
) -> Result<Json<Vec<Value>>, SourceError> {
    let rows = select_sources(&state.pool, user.id, None).await?;
    Ok(Json(rows.iter().map(source_contract).collect()))
}

pub async fn get_source(
    State(state): State<AppState>,
    Extension(user): Extension<AuthUser>,
    Path(source_id): Path<String>,
) -> Result<Json<Value>, SourceError> {
    let source_id = Uuid::parse_str(&source_id).map_err(|_| SourceError::NotFound)?;
    load_source_contract(&state.pool, user.id, source_id)
        .await
        .map(Json)
}

pub async fn update_source(
    State(state): State<AppState>,
    Extension(user): Extension<AuthUser>,
    Path(source_id): Path<String>,
    Json(payload): Json<SourceUpdate>,
) -> Result<Json<Value>, SourceError> {
    let source_id = Uuid::parse_str(&source_id).map_err(|_| SourceError::NotFound)?;
    let current = load_source_contract(&state.pool, user.id, source_id).await?;
    let source_type = current
        .get("type")
        .and_then(Value::as_str)
        .ok_or(SourceError::InvalidConfig)?;
    let config = payload
        .config
        .map(|value| canonical_source_config(source_type, value))
        .transpose()?;
    let (target_id, legacy_source_id) = source_scope(&state.pool, user.id, source_id).await?;
    let mut transaction = state.pool.begin().await?;
    if let Some(target_id) = target_id {
        let updated = sqlx::query(
            r"
            UPDATE collection_subscriptions
            SET enabled = COALESCE($1, enabled),
                display_config = COALESCE($2, display_config),
                updated_at = now()
            WHERE id = $3 AND user_id = $4 AND target_id = $5
            ",
        )
        .bind(payload.enabled)
        .bind(config.clone())
        .bind(source_id)
        .bind(user.id)
        .bind(target_id)
        .execute(&mut *transaction)
        .await?;
        if updated.rows_affected() != 1 {
            return Err(SourceError::NotFound);
        }
        sync_target_pin(&mut transaction, target_id).await?;
    } else if let Some(legacy_source_id) = legacy_source_id {
        let updated = sqlx::query(
            r"
            UPDATE collection_sources
            SET enabled = COALESCE($1, enabled),
                config = COALESCE($2, config),
                next_run_at = COALESCE($3, next_run_at),
                updated_at = now()
            WHERE id = $4 AND owner_id = $5 AND target_id IS NULL
            ",
        )
        .bind(payload.enabled)
        .bind(config)
        .bind(payload.next_run_at)
        .bind(legacy_source_id)
        .bind(user.id)
        .execute(&mut *transaction)
        .await?;
        if updated.rows_affected() != 1 {
            return Err(SourceError::NotFound);
        }
    }
    transaction.commit().await?;
    load_source_contract(&state.pool, user.id, source_id)
        .await
        .map(Json)
}

pub async fn delete_source(
    State(state): State<AppState>,
    Extension(user): Extension<AuthUser>,
    Path(source_id): Path<String>,
) -> Result<StatusCode, SourceError> {
    let source_id = Uuid::parse_str(&source_id).map_err(|_| SourceError::NotFound)?;
    let (target_id, legacy_source_id) = source_scope(&state.pool, user.id, source_id).await?;
    let mut transaction = state.pool.begin().await?;
    if let Some(target_id) = target_id {
        sqlx::query(
            r"
            UPDATE collection_requests
            SET idempotency_key = NULL, updated_at = now()
            WHERE subscription_id = $1
            ",
        )
        .bind(source_id)
        .execute(&mut *transaction)
        .await?;
        let deleted =
            sqlx::query("DELETE FROM collection_subscriptions WHERE id = $1 AND user_id = $2")
                .bind(source_id)
                .bind(user.id)
                .execute(&mut *transaction)
                .await?;
        if deleted.rows_affected() != 1 {
            return Err(SourceError::NotFound);
        }
        sync_target_pin(&mut transaction, target_id).await?;
    } else if let Some(legacy_source_id) = legacy_source_id {
        let deleted = sqlx::query(
            r"
            DELETE FROM collection_sources
            WHERE id = $1 AND owner_id = $2 AND target_id IS NULL
            ",
        )
        .bind(legacy_source_id)
        .bind(user.id)
        .execute(&mut *transaction)
        .await?;
        if deleted.rows_affected() != 1 {
            return Err(SourceError::NotFound);
        }
    }
    transaction.commit().await?;
    Ok(StatusCode::NO_CONTENT)
}

pub(crate) async fn load_source_contract(
    pool: &PgPool,
    owner_id: Uuid,
    source_id: Uuid,
) -> Result<Value, SourceError> {
    let mut rows = select_sources(pool, owner_id, Some(source_id)).await?;
    let row = rows.pop().ok_or(SourceError::NotFound)?;
    Ok(source_contract(&row))
}

#[allow(clippy::too_many_lines)]
pub async fn list_source_videos(
    State(state): State<AppState>,
    Extension(user): Extension<AuthUser>,
    Path(source_id): Path<String>,
    Query(query): Query<SourceVideosQuery>,
) -> Result<Json<Value>, SourceError> {
    let public_source_id = Uuid::parse_str(&source_id).map_err(|_| SourceError::NotFound)?;
    let limit = query.limit.unwrap_or(60);
    if !(1..=100).contains(&limit) {
        return Err(SourceError::InvalidLimit);
    }
    let cursor = decode_source_video_cursor(query.cursor.as_deref(), public_source_id)?;
    let snapshot_at = cursor
        .as_ref()
        .map_or_else(Utc::now, |value| value.snapshot);
    let (target_id, legacy_source_id) =
        source_scope(&state.pool, user.id, public_source_id).await?;

    let total = sqlx::query_scalar::<_, i64>(
        r"
        WITH visible_video AS (
          SELECT membership.video_id, membership.first_seen_at
          FROM collection_target_videos AS membership
          WHERE $1::uuid IS NOT NULL AND membership.target_id = $1
          UNION ALL
          SELECT membership.video_id, membership.first_seen_at
          FROM source_videos AS membership
          WHERE $2::uuid IS NOT NULL AND membership.source_id = $2
        )
        SELECT count(*)::bigint
        FROM visible_video
        WHERE first_seen_at <= $3
        ",
    )
    .bind(target_id)
    .bind(legacy_source_id)
    .bind(snapshot_at)
    .fetch_one(&state.pool)
    .await?;

    let effective_at = cursor.as_ref().map(|value| value.at);
    let after_id = cursor.as_ref().map(|value| value.id.as_str());
    let fetch_limit = i64::try_from(limit + 1).map_err(|_| SourceError::InvalidLimit)?;
    let mut videos = sqlx::query_as::<_, VideoRow>(
        r"
        WITH visible_video AS (
          SELECT membership.video_id, membership.first_seen_at
          FROM collection_target_videos AS membership
          WHERE $1::uuid IS NOT NULL AND membership.target_id = $1
          UNION ALL
          SELECT membership.video_id, membership.first_seen_at
          FROM source_videos AS membership
          WHERE $2::uuid IS NOT NULL AND membership.source_id = $2
        )
        SELECT video.youtube_video_id,
               channel.youtube_channel_id,
               video.title,
               video.description,
               video.published_at,
               video.duration_seconds,
               video.privacy_status,
               video.made_for_kids,
               COALESCE(video.source_fetched_at, 'epoch'::timestamptz)
                 AS source_fetched_at,
               jsonb_build_object(
                 'viewCount', COALESCE(stats.view_count, 0),
                 'likeCount', COALESCE(stats.like_count, 0),
                 'commentCount', COALESCE(stats.comment_count, 0)
               ) AS statistics
        FROM visible_video AS membership
        JOIN videos AS video ON video.id = membership.video_id
        LEFT JOIN channels AS channel ON channel.id = video.channel_id
        LEFT JOIN LATERAL (
          SELECT snapshot.view_count, snapshot.like_count, snapshot.comment_count
          FROM video_stat_snapshots AS snapshot
          WHERE snapshot.video_id = video.id
          ORDER BY snapshot.fetched_at DESC
          LIMIT 1
        ) AS stats ON TRUE
        WHERE membership.first_seen_at <= $3
          AND (
            $4::timestamptz IS NULL
            OR (
              COALESCE(video.published_at, video.source_fetched_at, 'epoch'::timestamptz),
              video.youtube_video_id
            ) < ($4, $5)
          )
        ORDER BY COALESCE(
                   video.published_at,
                   video.source_fetched_at,
                   'epoch'::timestamptz
                 ) DESC,
                 video.youtube_video_id DESC
        LIMIT $6
        ",
    )
    .bind(target_id)
    .bind(legacy_source_id)
    .bind(snapshot_at)
    .bind(effective_at)
    .bind(after_id)
    .bind(fetch_limit)
    .fetch_all(&state.pool)
    .await?;
    let has_more = videos.len() > limit;
    videos.truncate(limit);
    let next_cursor = if has_more {
        videos
            .last()
            .map(|video| encode_source_video_cursor(video, public_source_id, snapshot_at))
            .transpose()?
    } else {
        None
    };
    Ok(Json(json!({
        "videos": videos.iter().map(video_contract).collect::<Vec<_>>(),
        "nextCursor": next_cursor,
        "snapshotAt": snapshot_at,
        "total": total.max(0),
    })))
}

pub(crate) async fn source_scope(
    pool: &PgPool,
    owner_id: Uuid,
    source_id: Uuid,
) -> Result<(Option<Uuid>, Option<Uuid>), SourceError> {
    sqlx::query_as::<_, (Option<Uuid>, Option<Uuid>)>(
        r"
        SELECT subscription.target_id, NULL::uuid AS legacy_source_id
        FROM collection_subscriptions AS subscription
        WHERE subscription.id = $1 AND subscription.user_id = $2
        UNION ALL
        SELECT NULL::uuid AS target_id, source.id AS legacy_source_id
        FROM collection_sources AS source
        WHERE source.id = $1 AND source.owner_id = $2 AND source.target_id IS NULL
        ",
    )
    .bind(source_id)
    .bind(owner_id)
    .fetch_optional(pool)
    .await?
    .ok_or(SourceError::NotFound)
}

pub(crate) fn canonical_source_config(
    source_type: &str,
    value: Value,
) -> Result<Value, SourceError> {
    match source_type {
        "channel" => {
            let mut config: ChannelSourceConfig =
                serde_json::from_value(value).map_err(|_| SourceError::InvalidConfig)?;
            config.input = crate::resolution::normalize_channel_input(&config.input)
                .map_err(|_| SourceError::InvalidConfig)?;
            if !(1..=5_000).contains(&config.max_videos)
                || !(1..=100).contains(&config.max_comment_pages_per_video)
            {
                return Err(SourceError::InvalidConfig);
            }
            serde_json::to_value(config).map_err(SourceError::ConfigSerialization)
        }
        "keyword" => {
            let mut config: KeywordSourceConfig =
                serde_json::from_value(value).map_err(|_| SourceError::InvalidConfig)?;
            let normalized_query = config.query.trim().to_owned();
            config.query = normalized_query;
            config.region_code = config.region_code.map(|value| value.trim().to_owned());
            config.relevance_language = config
                .relevance_language
                .map(|value| value.trim().to_owned());
            if config.query.is_empty()
                || config.query.chars().count() > 500
                || !matches!(config.order.as_str(), "date" | "relevance" | "viewCount")
                || !(1..=100).contains(&config.max_pages_per_run)
                || !(1..=100).contains(&config.max_comment_pages_per_video)
                || config
                    .region_code
                    .as_ref()
                    .is_some_and(|value| value.chars().count() != 2)
                || config.relevance_language.as_ref().is_some_and(|value| {
                    let length = value.chars().count();
                    !(2..=16).contains(&length)
                })
            {
                return Err(SourceError::InvalidConfig);
            }
            let after = config
                .published_after
                .as_deref()
                .map(DateTime::parse_from_rfc3339)
                .transpose()
                .map_err(|_| SourceError::InvalidConfig)?;
            let before = config
                .published_before
                .as_deref()
                .map(DateTime::parse_from_rfc3339)
                .transpose()
                .map_err(|_| SourceError::InvalidConfig)?;
            if after
                .zip(before)
                .is_some_and(|(after, before)| after > before)
            {
                return Err(SourceError::InvalidConfig);
            }
            serde_json::to_value(config).map_err(SourceError::ConfigSerialization)
        }
        "video" => {
            let mut config: VideoSourceConfig =
                serde_json::from_value(value).map_err(|_| SourceError::InvalidConfig)?;
            config.input = crate::resolution::normalize_video_input(&config.input)
                .map_err(|_| SourceError::InvalidConfig)?;
            if !(1..=100).contains(&config.max_comment_pages_per_video) {
                return Err(SourceError::InvalidConfig);
            }
            serde_json::to_value(config).map_err(SourceError::ConfigSerialization)
        }
        _ => Err(SourceError::InvalidConfig),
    }
}

pub(crate) async fn sync_target_pin(
    transaction: &mut Transaction<'_, Postgres>,
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
            r"
            UPDATE collection_target_pins
            SET enabled = FALSE, updated_at = now()
            WHERE target_id = $1
            ",
        )
        .bind(target_id)
        .execute(&mut **transaction)
        .await?;
        return Ok(());
    }
    if matches!(target_type.as_str(), "channel" | "keyword") {
        sqlx::query(
            r"
            INSERT INTO collection_target_pins (
              target_id, enabled, interval_minutes, next_run_at
            )
            VALUES ($1, TRUE, 360, now())
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

fn video_contract(video: &VideoRow) -> Value {
    json!({
        "id": video.youtube_video_id,
        "channelId": video.youtube_channel_id,
        "title": video.title,
        "description": video.description,
        "publishedAt": video.published_at,
        "durationSeconds": video.duration_seconds.map(i64::from).map(|value| value.max(0)),
        "privacyStatus": video.privacy_status,
        "madeForKids": video.made_for_kids,
        "statistics": video.statistics,
        "fetchedAt": video.source_fetched_at,
    })
}

fn decode_source_video_cursor(
    encoded: Option<&str>,
    source_id: Uuid,
) -> Result<Option<SourceVideoCursor>, SourceError> {
    let Some(encoded) = encoded else {
        return Ok(None);
    };
    if encoded.len() > 512 {
        return Err(SourceError::InvalidCursor);
    }
    let bytes = URL_SAFE_NO_PAD
        .decode(encoded)
        .map_err(|_| SourceError::InvalidCursor)?;
    let cursor = serde_json::from_slice::<SourceVideoCursor>(&bytes)
        .map_err(|_| SourceError::InvalidCursor)?;
    if cursor.v != 1
        || cursor.scope != source_id.to_string()
        || cursor.filter != SOURCE_VIDEO_FILTER_HASH
        || cursor.sort != SOURCE_VIDEO_SORT
        || cursor.id.is_empty()
    {
        return Err(SourceError::InvalidCursor);
    }
    Ok(Some(cursor))
}

fn encode_source_video_cursor(
    video: &VideoRow,
    source_id: Uuid,
    snapshot_at: DateTime<Utc>,
) -> Result<String, SourceError> {
    let cursor = SourceVideoCursor {
        v: 1,
        at: video.published_at.unwrap_or(video.source_fetched_at),
        id: video.youtube_video_id.clone(),
        snapshot: snapshot_at,
        scope: source_id.to_string(),
        filter: SOURCE_VIDEO_FILTER_HASH.to_owned(),
        sort: SOURCE_VIDEO_SORT.to_owned(),
    };
    serde_json::to_vec(&cursor)
        .map(|bytes| URL_SAFE_NO_PAD.encode(bytes))
        .map_err(SourceError::CursorSerialization)
}

#[allow(clippy::too_many_lines)]
async fn select_sources(
    pool: &PgPool,
    owner_id: Uuid,
    source_id: Option<Uuid>,
) -> Result<Vec<SourceRow>, SourceError> {
    sqlx::query_as::<_, SourceRow>(
        r"
        WITH visible_subscriptions AS MATERIALIZED (
          SELECT subscription.id,
                 subscription.target_id,
                 subscription.display_config,
                 subscription.enabled,
                 subscription.created_at
          FROM collection_subscriptions AS subscription
          WHERE subscription.user_id = $1
            AND ($2::uuid IS NULL OR subscription.id = $2)
        ),
        visible_legacy_sources AS MATERIALIZED (
          SELECT source.id, source.type, source.config, source.enabled,
                 source.next_run_at, source.created_at
          FROM collection_sources AS source
          WHERE source.owner_id = $1 AND source.target_id IS NULL
            AND ($2::uuid IS NULL OR source.id = $2)
        ),
        visible_targets AS MATERIALIZED (
          SELECT DISTINCT target_id FROM visible_subscriptions
        ),
        target_counts AS (
          SELECT membership.target_id,
                 count(DISTINCT membership.video_id)::bigint AS stored_video_count,
                 count(comment.id)::bigint AS stored_comment_count
          FROM collection_target_videos AS membership
          JOIN visible_targets ON visible_targets.target_id = membership.target_id
          LEFT JOIN comments AS comment ON comment.video_id = membership.video_id
          GROUP BY membership.target_id
        ),
        target_reported AS (
          SELECT membership.target_id,
                 COALESCE(sum(COALESCE(snapshot.comment_count, 0)), 0)::bigint
                   AS reported_comment_count
          FROM collection_target_videos AS membership
          JOIN visible_targets ON visible_targets.target_id = membership.target_id
          LEFT JOIN LATERAL (
            SELECT current.comment_count
            FROM video_stat_snapshots AS current
            WHERE current.video_id = membership.video_id
            ORDER BY current.fetched_at DESC
            LIMIT 1
          ) AS snapshot ON TRUE
          GROUP BY membership.target_id
        ),
        source_counts AS (
          SELECT membership.source_id,
                 count(DISTINCT membership.video_id)::bigint AS stored_video_count,
                 count(comment.id)::bigint AS stored_comment_count
          FROM source_videos AS membership
          JOIN visible_legacy_sources AS source ON source.id = membership.source_id
          LEFT JOIN comments AS comment ON comment.video_id = membership.video_id
          GROUP BY membership.source_id
        ),
        source_reported AS (
          SELECT membership.source_id,
                 COALESCE(sum(COALESCE(snapshot.comment_count, 0)), 0)::bigint
                   AS reported_comment_count
          FROM source_videos AS membership
          JOIN visible_legacy_sources AS source ON source.id = membership.source_id
          LEFT JOIN LATERAL (
            SELECT current.comment_count
            FROM video_stat_snapshots AS current
            WHERE current.video_id = membership.video_id
            ORDER BY current.fetched_at DESC
            LIMIT 1
          ) AS snapshot ON TRUE
          GROUP BY membership.source_id
        ),
        latest_target_jobs AS (
          SELECT DISTINCT ON (job.target_id)
                 job.target_id, to_jsonb(job) AS latest_job
          FROM sync_jobs AS job
          JOIN visible_targets ON visible_targets.target_id = job.target_id
          WHERE job.parent_job_id IS NULL
          ORDER BY job.target_id, job.created_at DESC, job.id DESC
        ),
        latest_source_jobs AS (
          SELECT DISTINCT ON (job.source_id)
                 job.source_id, to_jsonb(job) AS latest_job
          FROM sync_jobs AS job
          JOIN visible_legacy_sources AS source ON source.id = job.source_id
          WHERE job.target_id IS NULL AND job.parent_job_id IS NULL
          ORDER BY job.source_id, job.created_at DESC, job.id DESC
        )
        SELECT subscription.id,
               target.type::text AS source_type,
               COALESCE(NULLIF(subscription.display_config, '{}'::jsonb), target.config) AS config,
               subscription.enabled,
               NULL::timestamptz AS next_run_at,
               target.id AS target_id,
               target.canonical_key,
               target.coverage,
               target.last_completed_at,
               latest.latest_job,
               COALESCE(counts.stored_video_count, 0)::bigint AS stored_video_count,
               COALESCE(counts.stored_comment_count, 0)::bigint AS stored_comment_count,
               COALESCE(reported.reported_comment_count, 0)::bigint AS reported_comment_count,
               subscription.created_at AS sort_created_at
        FROM visible_subscriptions AS subscription
        JOIN collection_targets AS target ON target.id = subscription.target_id
        LEFT JOIN target_counts AS counts ON counts.target_id = target.id
        LEFT JOIN target_reported AS reported ON reported.target_id = target.id
        LEFT JOIN latest_target_jobs AS latest ON latest.target_id = target.id

        UNION ALL

        SELECT source.id,
               source.type::text AS source_type,
               source.config,
               source.enabled,
               source.next_run_at,
               NULL::uuid AS target_id,
               NULL::text AS canonical_key,
               '{}'::jsonb AS coverage,
               NULL::timestamptz AS last_completed_at,
               latest.latest_job,
               COALESCE(counts.stored_video_count, 0)::bigint AS stored_video_count,
               COALESCE(counts.stored_comment_count, 0)::bigint AS stored_comment_count,
               COALESCE(reported.reported_comment_count, 0)::bigint AS reported_comment_count,
               source.created_at AS sort_created_at
        FROM visible_legacy_sources AS source
        LEFT JOIN source_counts AS counts ON counts.source_id = source.id
        LEFT JOIN source_reported AS reported ON reported.source_id = source.id
        LEFT JOIN latest_source_jobs AS latest ON latest.source_id = source.id
        ORDER BY sort_created_at, id
        ",
    )
    .bind(owner_id)
    .bind(source_id)
    .fetch_all(pool)
    .await
    .map_err(SourceError::Database)
}

fn source_contract(row: &SourceRow) -> Value {
    let config = canonical_source_config(&row.source_type, row.config.clone())
        .unwrap_or_else(|_| row.config.clone());
    json!({
        "id": row.id,
        "type": row.source_type,
        "config": config,
        "enabled": row.enabled,
        "nextRunAt": row.next_run_at,
        "targetId": row.target_id,
        "canonicalKey": row.canonical_key,
        "coverage": row.coverage,
        "lastCompletedAt": row.last_completed_at,
        "latestJob": row.latest_job.as_ref().map(job_contract),
        "storedVideoCount": row.stored_video_count.max(0),
        "storedCommentCount": row.stored_comment_count.max(0),
        "reportedCommentCount": row.reported_comment_count.max(0),
    })
}

pub(crate) fn job_contract(job: &Value) -> Value {
    job_contract_with_public_source(job, None)
}

pub(crate) fn job_contract_for_source(job: &Value, public_source_id: Uuid) -> Value {
    job_contract_with_public_source(job, Some(public_source_id))
}

fn job_contract_with_public_source(job: &Value, public_source_id: Option<Uuid>) -> Value {
    let progress_completed = integer(job, "progress_completed");
    let progress_total = optional_integer(job, "progress_total");
    let progress_unit = string(job, "progress_unit").unwrap_or("sources");
    let checkpoint = job.get("checkpoint").and_then(Value::as_object);
    let phase_progress = checkpoint
        .and_then(|value| value.get("phaseProgress"))
        .and_then(Value::as_object);
    let mut output = Map::new();
    output.insert("id".to_owned(), value_or_null(job, "id"));
    output.insert("state".to_owned(), value_or_null(job, "state"));
    output.insert(
        "currentStage".to_owned(),
        value_or_null(job, "current_stage"),
    );
    output.insert(
        "progress".to_owned(),
        json!({
            "completed": progress_completed.max(0),
            "total": progress_total.map(|value| value.max(0)),
            "unit": progress_unit,
        }),
    );
    for (output_name, phase_name, unit) in [
        ("videoProgress", "videos", "videos"),
        ("transcriptProgress", "transcripts", "videos"),
        ("commentProgress", "comments", "comments"),
    ] {
        output.insert(
            output_name.to_owned(),
            phase_contract(
                phase_progress.and_then(|phases| phases.get(phase_name)),
                progress_completed,
                progress_total,
                progress_unit,
                unit,
            ),
        );
    }
    output.insert("pauseReason".to_owned(), value_or_null(job, "pause_reason"));
    output.insert("retryCount".to_owned(), value_or_null(job, "retry_count"));
    output.insert(
        "lastErrorCode".to_owned(),
        value_or_null(job, "last_error_code"),
    );
    output.insert(
        "lastErrorProvider".to_owned(),
        value_or_null(job, "last_error_provider"),
    );
    output.insert(
        "lastErrorOperation".to_owned(),
        value_or_null(job, "last_error_operation"),
    );
    output.insert(
        "lastErrorRetryable".to_owned(),
        value_or_null(job, "last_error_retryable"),
    );
    output.insert(
        "lastErrorHttpStatus".to_owned(),
        value_or_null(job, "last_error_http_status"),
    );
    output.insert(
        "lastErrorAt".to_owned(),
        value_or_null(job, "last_error_at"),
    );
    output.insert("quotaBucket".to_owned(), value_or_null(job, "quota_bucket"));
    output.insert("resumeAt".to_owned(), value_or_null(job, "resume_at"));
    output.insert(
        "resumeIsAutomatic".to_owned(),
        job.get("resume_is_automatic")
            .cloned()
            .unwrap_or(Value::Bool(false)),
    );
    let partial_errors = job
        .get("partial_errors")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .map(|item| {
                    let mut item = item.clone();
                    if let (Some(source_id), Some(object)) =
                        (public_source_id, item.as_object_mut())
                    {
                        object.insert("sourceId".to_owned(), Value::from(source_id.to_string()));
                    }
                    item
                })
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();
    output.insert("partialErrors".to_owned(), Value::Array(partial_errors));
    Value::Object(output)
}

fn phase_contract(
    phase: Option<&Value>,
    fallback_completed: i64,
    fallback_total: Option<i64>,
    fallback_unit: &str,
    unit: &str,
) -> Value {
    if let Some(phase) = phase.and_then(Value::as_object) {
        let mut output = Map::new();
        output.insert(
            "completed".to_owned(),
            Value::from(integer_object(phase, "completed").max(0)),
        );
        output.insert(
            "total".to_owned(),
            phase
                .get("total")
                .and_then(Value::as_i64)
                .map_or(Value::Null, |value| Value::from(value.max(0))),
        );
        output.insert("unit".to_owned(), Value::from(unit));
        let failed = integer_object(phase, "failed").max(0);
        if failed > 0 {
            output.insert("failed".to_owned(), Value::from(failed));
        }
        let waiting_quota = integer_object(phase, "waitingQuota").max(0);
        if waiting_quota > 0 {
            output.insert("waitingQuota".to_owned(), Value::from(waiting_quota));
        }
        return Value::Object(output);
    }
    if fallback_unit != unit {
        return Value::Null;
    }
    json!({
        "completed": fallback_completed.max(0),
        "total": fallback_total.map(|value| value.max(0)),
        "unit": unit,
    })
}

fn integer(value: &Value, name: &str) -> i64 {
    value.get(name).and_then(Value::as_i64).unwrap_or(0)
}

fn optional_integer(value: &Value, name: &str) -> Option<i64> {
    value.get(name).and_then(Value::as_i64)
}

fn integer_object(value: &Map<String, Value>, name: &str) -> i64 {
    value.get(name).and_then(Value::as_i64).unwrap_or(0)
}

fn string<'a>(value: &'a Value, name: &str) -> Option<&'a str> {
    value.get(name).and_then(Value::as_str)
}

fn value_or_null(value: &Value, name: &str) -> Value {
    value.get(name).cloned().unwrap_or(Value::Null)
}

#[derive(Debug, Error)]
pub enum SourceError {
    #[error("source was not found")]
    NotFound,
    #[error("source video cursor is invalid")]
    InvalidCursor,
    #[error("source video limit is invalid")]
    InvalidLimit,
    #[error("source configuration is invalid")]
    InvalidConfig,
    #[error("source configuration serialization failed")]
    ConfigSerialization(serde_json::Error),
    #[error("source video cursor serialization failed")]
    CursorSerialization(serde_json::Error),
    #[error("source database operation failed")]
    Database(#[from] sqlx::Error),
}

impl IntoResponse for SourceError {
    fn into_response(self) -> Response {
        match self {
            Self::NotFound => (
                StatusCode::NOT_FOUND,
                Json(SourceErrorResponse {
                    detail: "Source was not found",
                    retryable: false,
                }),
            )
                .into_response(),
            Self::InvalidCursor => (
                StatusCode::UNPROCESSABLE_ENTITY,
                Json(SourceErrorResponse {
                    detail: "Invalid source video cursor",
                    retryable: false,
                }),
            )
                .into_response(),
            Self::InvalidLimit => (
                StatusCode::UNPROCESSABLE_ENTITY,
                Json(SourceErrorResponse {
                    detail: "limit must be between 1 and 100",
                    retryable: false,
                }),
            )
                .into_response(),
            Self::InvalidConfig => (
                StatusCode::UNPROCESSABLE_ENTITY,
                Json(SourceErrorResponse {
                    detail: "Source configuration is invalid",
                    retryable: false,
                }),
            )
                .into_response(),
            Self::ConfigSerialization(error) => {
                tracing::error!(%error, "source configuration serialization failed");
                (
                    StatusCode::INTERNAL_SERVER_ERROR,
                    Json(SourceErrorResponse {
                        detail: "Source configuration could not be serialized",
                        retryable: false,
                    }),
                )
                    .into_response()
            }
            Self::CursorSerialization(error) => {
                tracing::error!(%error, "source cursor serialization failed");
                (
                    StatusCode::INTERNAL_SERVER_ERROR,
                    Json(SourceErrorResponse {
                        detail: "Source video cursor could not be created",
                        retryable: false,
                    }),
                )
                    .into_response()
            }
            Self::Database(error) => {
                let failure = crate::db_error::classify(&error, "source");
                (
                    failure.status,
                    Json(SourceErrorResponse {
                        detail: failure.detail,
                        retryable: failure.retryable,
                    }),
                )
                    .into_response()
            }
        }
    }
}

#[derive(Serialize)]
struct SourceErrorResponse {
    detail: &'static str,
    #[serde(skip_serializing_if = "crate::is_false")]
    retryable: bool,
}

#[cfg(test)]
mod tests {
    use super::{
        SourceError, VideoRow, decode_source_video_cursor, encode_source_video_cursor, job_contract,
    };
    use chrono::Utc;
    use serde_json::json;
    use uuid::Uuid;

    #[test]
    fn job_presenter_preserves_phase_progress_contract() {
        let job = json!({
            "id": "job-id",
            "state": "running",
            "current_stage": "comments",
            "progress_completed": 2,
            "progress_total": 5,
            "progress_unit": "videos",
            "checkpoint": {
                "phaseProgress": {
                    "comments": {"completed": 10, "total": 20, "waitingQuota": 1}
                }
            },
            "partial_errors": [],
            "resume_is_automatic": false
        });
        let contract = job_contract(&job);
        assert_eq!(contract["videoProgress"]["completed"], 2);
        assert_eq!(contract["commentProgress"]["waitingQuota"], 1);
    }

    #[test]
    fn source_video_cursor_round_trips_and_is_bound_to_source() -> Result<(), SourceError> {
        let source_id = Uuid::new_v4();
        let now = Utc::now();
        let video = VideoRow {
            youtube_video_id: "video-1".to_owned(),
            youtube_channel_id: None,
            title: None,
            description: None,
            published_at: Some(now),
            duration_seconds: None,
            privacy_status: None,
            made_for_kids: None,
            source_fetched_at: now,
            statistics: json!({}),
        };
        let encoded = encode_source_video_cursor(&video, source_id, now)?;
        let decoded = decode_source_video_cursor(Some(&encoded), source_id)?
            .ok_or(SourceError::InvalidCursor)?;
        assert_eq!(decoded.id, "video-1");
        assert!(decode_source_video_cursor(Some(&encoded), Uuid::new_v4()).is_err());
        Ok(())
    }
}
