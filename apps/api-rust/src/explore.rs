//! Small owner-scoped explore reads and legacy target pin compatibility.

use crate::{AppState, auth::AuthUser};
use axum::Json;
use axum::extract::{Extension, Path, Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use base64::Engine;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use sqlx::FromRow;
use thiserror::Error;
use uuid::Uuid;

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
pub struct TargetPinUpdate {
    enabled: bool,
    #[serde(default = "default_interval")]
    interval_minutes: i32,
}

const fn default_interval() -> i32 {
    360
}

#[derive(Debug, FromRow, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct TargetPin {
    target_id: Uuid,
    enabled: bool,
    interval_minutes: i32,
    next_run_at: DateTime<Utc>,
    last_dispatched_at: Option<DateTime<Utc>>,
}

#[derive(Debug, FromRow, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SubscriberSnapshot {
    fetched_at: DateTime<Utc>,
    subscriber_count: Option<i64>,
    hidden_subscriber_count: Option<bool>,
}

#[derive(Debug, Deserialize)]
pub struct ExploreVideosQuery {
    #[serde(rename = "channelId")]
    channel_id: Option<String>,
    cursor: Option<String>,
    limit: Option<usize>,
}

#[derive(Debug, Deserialize)]
pub struct ExploreQuery {
    #[serde(rename = "channelId")]
    channel_id: Option<String>,
    offset: Option<usize>,
    limit: Option<usize>,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct ExploreVideoCursor {
    v: u8,
    kind: String,
    effective_at: DateTime<Utc>,
    fetched_at: DateTime<Utc>,
    id: String,
    snapshot: DateTime<Utc>,
    scope: String,
    filter: String,
    sort: String,
}

#[derive(Debug, FromRow)]
struct ExploreVideoRow {
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

#[derive(Debug, FromRow)]
struct ExploreChannelRow {
    youtube_channel_id: String,
    handle: Option<String>,
    title: Option<String>,
    description: Option<String>,
    thumbnail_url: Option<String>,
    subscriber_count: Option<i64>,
    view_count: Option<i64>,
    youtube_video_count: Option<i64>,
    hidden_subscriber_count: Option<bool>,
    video_count: i64,
    comment_count: i64,
    youtube_comment_count: i64,
    last_fetched_at: Option<DateTime<Utc>>,
    target_id: Option<Uuid>,
    pin_enabled: Option<bool>,
    pin_interval_minutes: Option<i32>,
    pin_next_run_at: Option<DateTime<Utc>>,
    pin_last_dispatched_at: Option<DateTime<Utc>>,
}

const EXPLORE_VIDEO_SORT: &str = "effective_published_fetched_desc";

pub async fn get_target_pin(
    State(state): State<AppState>,
    Extension(user): Extension<AuthUser>,
    Path(target_id): Path<String>,
) -> Result<Json<Option<TargetPin>>, ExploreError> {
    let target_id = Uuid::parse_str(&target_id).map_err(|_| ExploreError::TargetNotFound)?;
    require_subscription(&state, user.id, target_id).await?;
    Ok(Json(load_pin(&state, target_id).await?))
}

pub async fn set_target_pin(
    State(state): State<AppState>,
    Extension(user): Extension<AuthUser>,
    Path(target_id): Path<String>,
    Json(payload): Json<TargetPinUpdate>,
) -> Result<Json<TargetPin>, ExploreError> {
    if !(15..=10_080).contains(&payload.interval_minutes) {
        return Err(ExploreError::InvalidInterval);
    }
    let target_id = Uuid::parse_str(&target_id).map_err(|_| ExploreError::TargetNotFound)?;
    let mut transaction = state.pool.begin().await?;
    let subscribed = sqlx::query_scalar::<_, bool>(
        r"
        SELECT EXISTS (
          SELECT 1 FROM collection_subscriptions
          WHERE user_id = $1 AND target_id = $2
        )
        ",
    )
    .bind(user.id)
    .bind(target_id)
    .fetch_one(&mut *transaction)
    .await?;
    if !subscribed {
        return Err(ExploreError::TargetNotFound);
    }
    let pin = sqlx::query_as::<_, TargetPin>(
        r"
        INSERT INTO collection_target_pins (
          target_id, enabled, interval_minutes, next_run_at
        )
        VALUES ($1, $2, $3, now())
        ON CONFLICT (target_id) DO UPDATE
        SET enabled = EXCLUDED.enabled,
            interval_minutes = EXCLUDED.interval_minutes,
            next_run_at = CASE
              WHEN EXCLUDED.enabled THEN now()
              ELSE collection_target_pins.next_run_at
            END,
            updated_at = now()
        RETURNING target_id, enabled, interval_minutes,
                  next_run_at, last_dispatched_at
        ",
    )
    .bind(target_id)
    .bind(payload.enabled)
    .bind(payload.interval_minutes)
    .fetch_one(&mut *transaction)
    .await?;
    transaction.commit().await?;
    Ok(Json(pin))
}

pub async fn subscriber_history(
    State(state): State<AppState>,
    Extension(user): Extension<AuthUser>,
    Path(youtube_channel_id): Path<String>,
) -> Result<Json<Vec<SubscriberSnapshot>>, ExploreError> {
    let rows = sqlx::query_as::<_, SubscriberSnapshot>(
        r"
        SELECT snapshot.fetched_at,
               snapshot.subscriber_count,
               snapshot.hidden_subscriber_count
        FROM channel_snapshots AS snapshot
        JOIN channels AS channel ON channel.id = snapshot.channel_id
        WHERE channel.youtube_channel_id = $1
          AND (
            snapshot.subscriber_count IS NOT NULL
            OR snapshot.hidden_subscriber_count IS NOT NULL
          )
          AND EXISTS (
            SELECT 1
            FROM videos AS video
            JOIN collection_target_videos AS membership
              ON membership.video_id = video.id
            JOIN collection_subscriptions AS subscription
              ON subscription.target_id = membership.target_id
            WHERE video.channel_id = channel.id
              AND subscription.user_id = $2
          )
        ORDER BY snapshot.fetched_at DESC
        LIMIT 180
        ",
    )
    .bind(youtube_channel_id)
    .bind(user.id)
    .fetch_all(&state.pool)
    .await?;
    Ok(Json(rows.into_iter().rev().collect()))
}

pub async fn list_channels(
    State(state): State<AppState>,
    Extension(user): Extension<AuthUser>,
) -> Result<Json<Value>, ExploreError> {
    Ok(Json(json!({
        "channels": load_channels(&state, user.id).await?,
    })))
}

#[allow(clippy::too_many_lines)]
pub async fn list_videos(
    State(state): State<AppState>,
    Extension(user): Extension<AuthUser>,
    Query(query): Query<ExploreVideosQuery>,
) -> Result<Json<Value>, ExploreError> {
    validate_channel_id(query.channel_id.as_deref())?;
    let limit = query.limit.unwrap_or(60);
    if !(1..=100).contains(&limit) {
        return Err(ExploreError::InvalidLimit);
    }
    let scope = format!("owner:{}", user.id);
    let filter_hash = explore_filter_hash(query.channel_id.as_deref())?;
    let cursor = decode_video_cursor(query.cursor.as_deref(), &scope, &filter_hash)?;
    let snapshot_at = cursor
        .as_ref()
        .map_or_else(Utc::now, |value| value.snapshot);

    let total = sqlx::query_scalar::<_, i64>(
        r"
        WITH visible_video AS (
          SELECT membership.video_id,
                 min(GREATEST(membership.first_seen_at, subscription.created_at))
                   AS first_seen_at
          FROM collection_target_videos AS membership
          JOIN collection_subscriptions AS subscription
            ON subscription.target_id = membership.target_id
           AND subscription.user_id = $1
          GROUP BY membership.video_id
        )
        SELECT count(*)::bigint
        FROM visible_video AS visible
        JOIN videos AS video ON video.id = visible.video_id
        LEFT JOIN channels AS channel ON channel.id = video.channel_id
        WHERE visible.first_seen_at <= $2
          AND ($3::text IS NULL OR channel.youtube_channel_id = $3)
        ",
    )
    .bind(user.id)
    .bind(snapshot_at)
    .bind(query.channel_id.as_deref())
    .fetch_one(&state.pool)
    .await?;

    let after_effective = cursor.as_ref().map(|value| value.effective_at);
    let after_fetched = cursor.as_ref().map(|value| value.fetched_at);
    let after_id = cursor.as_ref().map(|value| value.id.as_str());
    let fetch_limit = i64::try_from(limit + 1).map_err(|_| ExploreError::InvalidLimit)?;
    let mut videos = sqlx::query_as::<_, ExploreVideoRow>(
        r"
        WITH visible_video AS (
          SELECT membership.video_id,
                 min(GREATEST(membership.first_seen_at, subscription.created_at))
                   AS first_seen_at
          FROM collection_target_videos AS membership
          JOIN collection_subscriptions AS subscription
            ON subscription.target_id = membership.target_id
           AND subscription.user_id = $1
          GROUP BY membership.video_id
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
        FROM visible_video AS visible
        JOIN videos AS video ON video.id = visible.video_id
        LEFT JOIN channels AS channel ON channel.id = video.channel_id
        LEFT JOIN LATERAL (
          SELECT snapshot.view_count, snapshot.like_count, snapshot.comment_count
          FROM video_stat_snapshots AS snapshot
          WHERE snapshot.video_id = video.id
          ORDER BY snapshot.fetched_at DESC
          LIMIT 1
        ) AS stats ON TRUE
        WHERE visible.first_seen_at <= $2
          AND ($3::text IS NULL OR channel.youtube_channel_id = $3)
          AND (
            $4::timestamptz IS NULL
            OR (
              COALESCE(video.published_at, video.source_fetched_at, 'epoch'::timestamptz),
              COALESCE(video.source_fetched_at, 'epoch'::timestamptz),
              video.youtube_video_id
            ) < ($4, $5, $6)
          )
        ORDER BY COALESCE(video.published_at, video.source_fetched_at, 'epoch'::timestamptz) DESC,
                 COALESCE(video.source_fetched_at, 'epoch'::timestamptz) DESC,
                 video.youtube_video_id DESC
        LIMIT $7
        ",
    )
    .bind(user.id)
    .bind(snapshot_at)
    .bind(query.channel_id.as_deref())
    .bind(after_effective)
    .bind(after_fetched)
    .bind(after_id)
    .bind(fetch_limit)
    .fetch_all(&state.pool)
    .await?;
    let has_more = videos.len() > limit;
    videos.truncate(limit);
    let next_cursor = if has_more {
        videos
            .last()
            .map(|video| encode_video_cursor(video, snapshot_at, &scope, &filter_hash))
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

pub async fn explore(
    State(state): State<AppState>,
    Extension(user): Extension<AuthUser>,
    Query(query): Query<ExploreQuery>,
) -> Result<Json<Value>, ExploreError> {
    validate_channel_id(query.channel_id.as_deref())?;
    let offset = query.offset.unwrap_or(0);
    let limit = query.limit.unwrap_or(60);
    if !(12..=120).contains(&limit) || offset > i32::MAX as usize {
        return Err(ExploreError::InvalidLegacyPage);
    }
    let fetch_limit = i64::try_from(limit + 1).map_err(|_| ExploreError::InvalidLegacyPage)?;
    let offset = i64::try_from(offset).map_err(|_| ExploreError::InvalidLegacyPage)?;
    let mut videos = load_offset_videos(
        &state,
        user.id,
        query.channel_id.as_deref(),
        fetch_limit,
        offset,
    )
    .await?;
    let has_more = videos.len() > limit;
    videos.truncate(limit);
    Ok(Json(json!({
        "channels": load_channels(&state, user.id).await?,
        "videos": videos.iter().map(video_contract).collect::<Vec<_>>(),
        "nextOffset": has_more.then_some(offset + i64::try_from(videos.len()).unwrap_or(0)),
    })))
}

#[allow(clippy::too_many_lines)]
async fn load_channels(state: &AppState, user_id: Uuid) -> Result<Vec<Value>, ExploreError> {
    let rows = sqlx::query_as::<_, ExploreChannelRow>(
        r"
        WITH visible_video AS MATERIALIZED (
          SELECT membership.video_id,
                 video.channel_id,
                 max(video.source_fetched_at) AS source_fetched_at
          FROM collection_target_videos AS membership
          JOIN collection_subscriptions AS subscription
            ON subscription.target_id = membership.target_id
           AND subscription.user_id = $1
          JOIN videos AS video ON video.id = membership.video_id
          WHERE video.channel_id IS NOT NULL
          GROUP BY membership.video_id, video.channel_id
        ),
        video_aggregate AS (
          SELECT channel_id,
                 count(*)::bigint AS video_count,
                 max(source_fetched_at) AS last_fetched_at
          FROM visible_video
          GROUP BY channel_id
        ),
        comment_aggregate AS (
          SELECT visible.channel_id, count(comment.id)::bigint AS comment_count
          FROM visible_video AS visible
          LEFT JOIN comments AS comment ON comment.video_id = visible.video_id
          GROUP BY visible.channel_id
        ),
        latest_video_stats AS (
          SELECT DISTINCT ON (snapshot.video_id)
                 snapshot.video_id, snapshot.comment_count
          FROM video_stat_snapshots AS snapshot
          JOIN visible_video AS visible ON visible.video_id = snapshot.video_id
          ORDER BY snapshot.video_id, snapshot.fetched_at DESC
        ),
        youtube_comment_aggregate AS (
          SELECT visible.channel_id,
                 COALESCE(sum(COALESCE(stats.comment_count, 0)), 0)::bigint
                   AS youtube_comment_count
          FROM visible_video AS visible
          LEFT JOIN latest_video_stats AS stats ON stats.video_id = visible.video_id
          GROUP BY visible.channel_id
        )
        SELECT channel.youtube_channel_id,
               channel.handle,
               channel.title,
               channel.description,
               channel.thumbnail_url,
               channel_stats.subscriber_count,
               channel_stats.view_count,
               channel_stats.video_count AS youtube_video_count,
               channel_stats.hidden_subscriber_count,
               video_aggregate.video_count,
               COALESCE(comment_aggregate.comment_count, 0)::bigint AS comment_count,
               COALESCE(youtube_comments.youtube_comment_count, 0)::bigint
                 AS youtube_comment_count,
               GREATEST(channel.source_fetched_at, video_aggregate.last_fetched_at)
                 AS last_fetched_at,
               target.id AS target_id,
               pin.enabled AS pin_enabled,
               pin.interval_minutes AS pin_interval_minutes,
               pin.next_run_at AS pin_next_run_at,
               pin.last_dispatched_at AS pin_last_dispatched_at
        FROM video_aggregate
        JOIN channels AS channel ON channel.id = video_aggregate.channel_id
        LEFT JOIN comment_aggregate ON comment_aggregate.channel_id = channel.id
        LEFT JOIN youtube_comment_aggregate AS youtube_comments
          ON youtube_comments.channel_id = channel.id
        LEFT JOIN LATERAL (
          SELECT snapshot.subscriber_count,
                 snapshot.view_count,
                 snapshot.video_count,
                 snapshot.hidden_subscriber_count
          FROM channel_snapshots AS snapshot
          WHERE snapshot.channel_id = channel.id
            AND (
              snapshot.subscriber_count IS NOT NULL
              OR snapshot.view_count IS NOT NULL
              OR snapshot.video_count IS NOT NULL
              OR snapshot.hidden_subscriber_count IS NOT NULL
            )
          ORDER BY snapshot.fetched_at DESC
          LIMIT 1
        ) AS channel_stats ON TRUE
        LEFT JOIN LATERAL (
          SELECT candidate.id
          FROM collection_targets AS candidate
          WHERE candidate.resolved_channel_id = channel.id
            AND EXISTS (
              SELECT 1 FROM collection_subscriptions AS subscription
              WHERE subscription.target_id = candidate.id
                AND subscription.user_id = $1
            )
          ORDER BY candidate.updated_at DESC, candidate.id
          LIMIT 1
        ) AS target ON TRUE
        LEFT JOIN collection_target_pins AS pin ON pin.target_id = target.id
        ORDER BY GREATEST(channel.source_fetched_at, video_aggregate.last_fetched_at)
                   DESC NULLS LAST,
                 channel.title,
                 channel.youtube_channel_id
        ",
    )
    .bind(user_id)
    .fetch_all(&state.pool)
    .await?;
    Ok(rows.iter().map(channel_contract).collect())
}

async fn load_offset_videos(
    state: &AppState,
    user_id: Uuid,
    channel_id: Option<&str>,
    limit: i64,
    offset: i64,
) -> Result<Vec<ExploreVideoRow>, ExploreError> {
    sqlx::query_as::<_, ExploreVideoRow>(
        r"
        WITH visible_video AS (
          SELECT DISTINCT membership.video_id
          FROM collection_target_videos AS membership
          JOIN collection_subscriptions AS subscription
            ON subscription.target_id = membership.target_id
           AND subscription.user_id = $1
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
        FROM visible_video AS visible
        JOIN videos AS video ON video.id = visible.video_id
        LEFT JOIN channels AS channel ON channel.id = video.channel_id
        LEFT JOIN LATERAL (
          SELECT snapshot.view_count, snapshot.like_count, snapshot.comment_count
          FROM video_stat_snapshots AS snapshot
          WHERE snapshot.video_id = video.id
          ORDER BY snapshot.fetched_at DESC
          LIMIT 1
        ) AS stats ON TRUE
        WHERE ($2::text IS NULL OR channel.youtube_channel_id = $2)
        ORDER BY COALESCE(video.published_at, video.source_fetched_at, 'epoch'::timestamptz) DESC,
                 video.source_fetched_at DESC,
                 video.youtube_video_id DESC
        LIMIT $3 OFFSET $4
        ",
    )
    .bind(user_id)
    .bind(channel_id)
    .bind(limit)
    .bind(offset)
    .fetch_all(&state.pool)
    .await
    .map_err(ExploreError::Database)
}

fn channel_contract(row: &ExploreChannelRow) -> Value {
    let video_count = row.video_count.max(0);
    let comment_count = row.comment_count.max(0);
    let youtube_comment_count = row.youtube_comment_count.max(0);
    let youtube_video_count = row.youtube_video_count.map(|value| value.max(0));
    let pin = match (
        row.target_id,
        row.pin_enabled,
        row.pin_interval_minutes,
        row.pin_next_run_at,
    ) {
        (Some(target_id), Some(enabled), Some(interval_minutes), Some(next_run_at)) => {
            Some(json!({
                "targetId": target_id,
                "enabled": enabled,
                "intervalMinutes": interval_minutes,
                "nextRunAt": next_run_at,
                "lastDispatchedAt": row.pin_last_dispatched_at,
            }))
        }
        _ => None,
    };
    json!({
        "youtubeChannelId": row.youtube_channel_id,
        "handle": row.handle,
        "title": row.title,
        "description": row.description,
        "thumbnailUrl": row.thumbnail_url,
        "subscriberCount": row.subscriber_count.map(|value| value.max(0)),
        "viewCount": row.view_count.map(|value| value.max(0)),
        "youtubeVideoCount": youtube_video_count,
        "hiddenSubscriberCount": row.hidden_subscriber_count,
        "videoCount": video_count,
        "commentCount": comment_count,
        "youtubeCommentCount": youtube_comment_count,
        "videoCollectionRate": collection_rate(video_count, youtube_video_count.unwrap_or(0)),
        "commentCollectionRate": collection_rate(comment_count, youtube_comment_count),
        "lastFetchedAt": row.last_fetched_at,
        "targetId": row.target_id,
        "pin": pin,
    })
}

fn collection_rate(stored: i64, reported: i64) -> i64 {
    if reported <= 0 {
        return 0;
    }
    let numerator = i128::from(stored.max(0)) * 100;
    let denominator = i128::from(reported);
    let quotient = numerator / denominator;
    let remainder = numerator % denominator;
    let doubled = remainder * 2;
    let rounded = if doubled > denominator || (doubled == denominator && quotient % 2 != 0) {
        quotient + 1
    } else {
        quotient
    };
    let rate = i64::try_from(rounded).unwrap_or(i64::MAX);
    rate.clamp(0, 100)
}

fn video_contract(video: &ExploreVideoRow) -> Value {
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

fn validate_channel_id(channel_id: Option<&str>) -> Result<(), ExploreError> {
    if channel_id.is_some_and(|value| value.is_empty() || value.len() > 64) {
        return Err(ExploreError::InvalidChannel);
    }
    Ok(())
}

fn explore_filter_hash(channel_id: Option<&str>) -> Result<String, ExploreError> {
    let payload = serde_json::to_vec(&json!({"channelId": channel_id}))
        .map_err(ExploreError::CursorSerialization)?;
    Ok(hex::encode(Sha256::digest(payload)))
}

fn decode_video_cursor(
    encoded: Option<&str>,
    scope: &str,
    filter_hash: &str,
) -> Result<Option<ExploreVideoCursor>, ExploreError> {
    let Some(encoded) = encoded else {
        return Ok(None);
    };
    if encoded.len() > 768 {
        return Err(ExploreError::InvalidCursor);
    }
    let bytes = URL_SAFE_NO_PAD
        .decode(encoded)
        .map_err(|_| ExploreError::InvalidCursor)?;
    let cursor = serde_json::from_slice::<ExploreVideoCursor>(&bytes)
        .map_err(|_| ExploreError::InvalidCursor)?;
    if cursor.v != 1
        || cursor.kind != "explore-video"
        || cursor.scope != scope
        || cursor.filter != filter_hash
        || cursor.sort != EXPLORE_VIDEO_SORT
        || cursor.id.is_empty()
    {
        return Err(ExploreError::InvalidCursor);
    }
    Ok(Some(cursor))
}

fn encode_video_cursor(
    video: &ExploreVideoRow,
    snapshot_at: DateTime<Utc>,
    scope: &str,
    filter_hash: &str,
) -> Result<String, ExploreError> {
    let cursor = ExploreVideoCursor {
        v: 1,
        kind: "explore-video".to_owned(),
        effective_at: video.published_at.unwrap_or(video.source_fetched_at),
        fetched_at: video.source_fetched_at,
        id: video.youtube_video_id.clone(),
        snapshot: snapshot_at,
        scope: scope.to_owned(),
        filter: filter_hash.to_owned(),
        sort: EXPLORE_VIDEO_SORT.to_owned(),
    };
    serde_json::to_vec(&cursor)
        .map(|bytes| URL_SAFE_NO_PAD.encode(bytes))
        .map_err(ExploreError::CursorSerialization)
}

async fn require_subscription(
    state: &AppState,
    user_id: Uuid,
    target_id: Uuid,
) -> Result<(), ExploreError> {
    let exists = sqlx::query_scalar::<_, bool>(
        r"
        SELECT EXISTS (
          SELECT 1 FROM collection_subscriptions
          WHERE user_id = $1 AND target_id = $2
        )
        ",
    )
    .bind(user_id)
    .bind(target_id)
    .fetch_one(&state.pool)
    .await?;
    if exists {
        Ok(())
    } else {
        Err(ExploreError::TargetNotFound)
    }
}

async fn load_pin(state: &AppState, target_id: Uuid) -> Result<Option<TargetPin>, ExploreError> {
    sqlx::query_as::<_, TargetPin>(
        r"
        SELECT target_id, enabled, interval_minutes,
               next_run_at, last_dispatched_at
        FROM collection_target_pins
        WHERE target_id = $1
        ",
    )
    .bind(target_id)
    .fetch_optional(&state.pool)
    .await
    .map_err(ExploreError::Database)
}

#[derive(Debug, Error)]
pub enum ExploreError {
    #[error("collection target was not found")]
    TargetNotFound,
    #[error("target pin interval is invalid")]
    InvalidInterval,
    #[error("explore video cursor is invalid")]
    InvalidCursor,
    #[error("explore video limit is invalid")]
    InvalidLimit,
    #[error("legacy explore page is invalid")]
    InvalidLegacyPage,
    #[error("explore channel filter is invalid")]
    InvalidChannel,
    #[error("explore cursor serialization failed")]
    CursorSerialization(serde_json::Error),
    #[error("explore database operation failed")]
    Database(#[from] sqlx::Error),
}

impl IntoResponse for ExploreError {
    fn into_response(self) -> Response {
        let (status, detail, retryable) = match self {
            Self::TargetNotFound => (
                StatusCode::NOT_FOUND,
                "Collection target was not found",
                false,
            ),
            Self::InvalidInterval => (
                StatusCode::UNPROCESSABLE_ENTITY,
                "intervalMinutes must be between 15 and 10080",
                false,
            ),
            Self::InvalidCursor => (
                StatusCode::UNPROCESSABLE_ENTITY,
                "Invalid explore video cursor",
                false,
            ),
            Self::InvalidLimit => (
                StatusCode::UNPROCESSABLE_ENTITY,
                "limit must be between 1 and 100",
                false,
            ),
            Self::InvalidLegacyPage => (
                StatusCode::UNPROCESSABLE_ENTITY,
                "limit must be between 12 and 120 and offset must be non-negative",
                false,
            ),
            Self::InvalidChannel => (
                StatusCode::UNPROCESSABLE_ENTITY,
                "channelId must contain between 1 and 64 characters",
                false,
            ),
            Self::CursorSerialization(error) => {
                tracing::error!(%error, "explore cursor serialization failed");
                (
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "Explore video cursor could not be created",
                    false,
                )
            }
            Self::Database(error) => {
                let failure = crate::db_error::classify(&error, "explore");
                (failure.status, failure.detail, failure.retryable)
            }
        };
        (status, Json(ExploreErrorResponse { detail, retryable })).into_response()
    }
}

#[derive(Serialize)]
struct ExploreErrorResponse {
    detail: &'static str,
    #[serde(skip_serializing_if = "crate::is_false")]
    retryable: bool,
}

#[cfg(test)]
mod tests {
    use super::*;

    fn video() -> Result<ExploreVideoRow, chrono::ParseError> {
        let fetched_at = "2026-08-13T01:03:00Z".parse()?;
        Ok(ExploreVideoRow {
            youtube_video_id: "video-1".to_owned(),
            youtube_channel_id: Some("channel-1".to_owned()),
            title: None,
            description: None,
            published_at: Some("2026-08-13T01:02:03Z".parse()?),
            duration_seconds: None,
            privacy_status: None,
            made_for_kids: None,
            source_fetched_at: fetched_at,
            statistics: json!({}),
        })
    }

    #[test]
    fn filter_hash_matches_python_canonical_json() -> Result<(), ExploreError> {
        assert_eq!(
            explore_filter_hash(None)?,
            "1a3b01df882bfe18949a49b3486a9b35388f5cac068845ba1e50befcb82621d1"
        );
        Ok(())
    }

    #[test]
    fn collection_rate_matches_python_ties_to_even_rounding() {
        assert_eq!(collection_rate(1, 8), 12);
        assert_eq!(collection_rate(3, 8), 38);
        assert_eq!(collection_rate(9, 8), 100);
        assert_eq!(collection_rate(1, 0), 0);
    }

    #[test]
    fn video_cursor_round_trips_and_binds_scope() -> Result<(), Box<dyn std::error::Error>> {
        let row = video()?;
        let scope = "owner:00000000-0000-0000-0000-000000000001";
        let filter = explore_filter_hash(None)?;
        let encoded = encode_video_cursor(&row, row.source_fetched_at, scope, &filter)?;
        let decoded = decode_video_cursor(Some(&encoded), scope, &filter)?
            .ok_or(ExploreError::InvalidCursor)?;

        assert_eq!(decoded.id, "video-1");
        assert!(decode_video_cursor(Some(&encoded), "owner:another", &filter).is_err());
        Ok(())
    }
}
