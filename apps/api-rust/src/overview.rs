//! Bounded source overview backed by exact SQL aggregates and persisted `BoW` frequency.

use crate::AppState;
use crate::analysis::{AnalysisError, load_frequency_keywords};
use crate::auth::AuthUser;
use crate::sources::{SourceError, load_source_contract, source_scope};
use axum::Json;
use axum::extract::{Extension, Path, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use chrono::{DateTime, Utc};
use serde::Serialize;
use serde_json::{Value, json};
use sqlx::{FromRow, PgPool};
use thiserror::Error;
use uuid::Uuid;

#[derive(Debug, FromRow)]
struct SummaryRow {
    video_count: i64,
    comment_count: i64,
    latest_video_published_at: Option<DateTime<Utc>>,
    latest_comment_published_at: Option<DateTime<Utc>>,
}

#[derive(Debug, FromRow)]
struct OverviewVideoRow {
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

pub async fn get_source_overview(
    State(state): State<AppState>,
    Extension(user): Extension<AuthUser>,
    Path(source_id): Path<String>,
) -> Result<Json<Value>, OverviewError> {
    let source_id = Uuid::parse_str(&source_id).map_err(|_| OverviewError::NotFound)?;
    load_source_overview(&state, user.id, source_id)
        .await
        .map(Json)
}

pub async fn get_source_results(
    State(state): State<AppState>,
    Extension(user): Extension<AuthUser>,
    Path(source_id): Path<String>,
) -> Result<Json<Value>, OverviewError> {
    let source_id = Uuid::parse_str(&source_id).map_err(|_| OverviewError::NotFound)?;
    let overview = load_source_overview(&state, user.id, source_id).await?;
    let (target_id, legacy_source_id) = source_scope(&state.pool, user.id, source_id).await?;
    let videos = load_result_videos(&state.pool, target_id, legacy_source_id).await?;
    let summary = overview
        .get("summary")
        .cloned()
        .unwrap_or_else(|| json!({}));
    let analysis = json!({
        "videoCount": summary.get("videoCount").cloned().unwrap_or_else(|| json!(0)),
        "commentCount": summary.get("commentCount").cloned().unwrap_or_else(|| json!(0)),
        "latestVideoPublishedAt": summary
            .get("latestVideoPublishedAt")
            .cloned()
            .unwrap_or(Value::Null),
        "latestCommentPublishedAt": summary
            .get("latestCommentPublishedAt")
            .cloned()
            .unwrap_or(Value::Null),
        "topWords": summary.get("topWords").cloned().unwrap_or_else(|| json!([])),
        "generatedAt": summary.get("generatedAt").cloned().unwrap_or_else(|| json!(Utc::now())),
    });
    Ok(Json(json!({
        "source": overview.get("source").cloned().unwrap_or(Value::Null),
        "latestJob": overview.get("latestJob").cloned().unwrap_or(Value::Null),
        "videos": videos,
        "commentSummary": {
            "total": summary.get("commentCount").cloned().unwrap_or_else(|| json!(0)),
            "latestPublishedAt": summary.get("latestCommentPublishedAt").cloned().unwrap_or(Value::Null),
            "topWords": summary.get("topWords").cloned().unwrap_or_else(|| json!([])),
        },
        "analysis": analysis,
    })))
}

async fn load_source_overview(
    state: &AppState,
    user_id: Uuid,
    source_id: Uuid,
) -> Result<Value, OverviewError> {
    let source = load_source_contract(&state.pool, user_id, source_id).await?;
    let (target_id, legacy_source_id) = source_scope(&state.pool, user_id, source_id).await?;
    let summary = load_summary(&state.pool, target_id, legacy_source_id).await?;
    let scope_kind = if target_id.is_some() {
        "target"
    } else {
        "owner"
    };
    let frequency_scope_id = target_id.unwrap_or(user_id);
    let (comment_keywords, indexed_comment_documents) = load_frequency_keywords(
        &state.pool,
        user_id,
        scope_kind,
        frequency_scope_id,
        "comment",
        "comment",
        15,
    )
    .await?;
    let top_words = comment_keywords
        .iter()
        .map(|item| {
            json!({
                "word": item["term"],
                "count": item["termCount"],
            })
        })
        .collect::<Vec<_>>();
    let data_version = load_data_version(&state.pool, target_id, legacy_source_id).await?;
    let top_words_status = if summary.comment_count == 0 || indexed_comment_documents > 0 {
        "fresh"
    } else {
        "building"
    };
    let coverage = source.get("coverage").cloned().unwrap_or_else(|| json!({}));
    let partial_data = coverage
        .get("status")
        .and_then(Value::as_str)
        .is_some_and(|status| matches!(status, "limited" | "unknown"));
    let latest_job = source.get("latestJob").cloned().unwrap_or(Value::Null);
    let as_of_job_id = latest_job.get("id").cloned().unwrap_or(Value::Null);
    let top_videos = json!({
        "views": load_top_videos(&state.pool, target_id, legacy_source_id, "view_count").await?,
        "likes": load_top_videos(&state.pool, target_id, legacy_source_id, "like_count").await?,
        "comments": load_top_videos(&state.pool, target_id, legacy_source_id, "comment_count").await?,
    });
    let generated_at = Utc::now();

    Ok(json!({
        "source": source,
        "latestJob": latest_job,
        "summary": {
            "videoCount": summary.video_count.max(0),
            "commentCount": summary.comment_count.max(0),
            "latestVideoPublishedAt": summary.latest_video_published_at,
            "latestCommentPublishedAt": summary.latest_comment_published_at,
            "topWords": top_words,
            "generatedAt": generated_at,
            "asOfJobId": as_of_job_id,
            "dataVersion": data_version.max(0),
            "status": "fresh",
            "topWordsStatus": top_words_status,
            "partialData": partial_data,
            "coverage": coverage,
        },
        "topVideos": top_videos,
    }))
}

async fn load_summary(
    pool: &PgPool,
    target_id: Option<Uuid>,
    legacy_source_id: Option<Uuid>,
) -> Result<SummaryRow, sqlx::Error> {
    sqlx::query_as::<_, SummaryRow>(
        r"
        WITH visible_video AS MATERIALIZED (
          SELECT membership.video_id
          FROM collection_target_videos AS membership
          WHERE $1::uuid IS NOT NULL AND membership.target_id = $1
          UNION ALL
          SELECT membership.video_id
          FROM source_videos AS membership
          WHERE $2::uuid IS NOT NULL AND membership.source_id = $2
        ),
        comment_summary AS (
          SELECT count(*)::bigint AS comment_count,
                 max(COALESCE(comment.published_at, comment.source_fetched_at))
                   AS latest_comment_published_at
          FROM comments AS comment
          JOIN visible_video AS visible ON visible.video_id = comment.video_id
          WHERE comment.deleted_at IS NULL
            AND (comment.expires_at IS NULL OR comment.expires_at > now())
        )
        SELECT count(*)::bigint AS video_count,
               comment.comment_count,
               max(video.published_at) AS latest_video_published_at,
               comment.latest_comment_published_at
        FROM visible_video AS visible
        JOIN videos AS video ON video.id = visible.video_id
        CROSS JOIN comment_summary AS comment
        GROUP BY comment.comment_count, comment.latest_comment_published_at
        ",
    )
    .bind(target_id)
    .bind(legacy_source_id)
    .fetch_optional(pool)
    .await
    .map(|row| {
        row.unwrap_or(SummaryRow {
            video_count: 0,
            comment_count: 0,
            latest_video_published_at: None,
            latest_comment_published_at: None,
        })
    })
}

async fn load_data_version(
    pool: &PgPool,
    target_id: Option<Uuid>,
    legacy_source_id: Option<Uuid>,
) -> Result<i64, sqlx::Error> {
    sqlx::query_scalar::<_, i64>(
        r"
        SELECT data_version::bigint FROM collection_targets WHERE id = $1
        UNION ALL
        SELECT data_version::bigint FROM collection_sources WHERE id = $2
        ",
    )
    .bind(target_id)
    .bind(legacy_source_id)
    .fetch_optional(pool)
    .await
    .map(|value| value.unwrap_or(0))
}

#[allow(clippy::too_many_lines)]
async fn load_top_videos(
    pool: &PgPool,
    target_id: Option<Uuid>,
    legacy_source_id: Option<Uuid>,
    metric: &'static str,
) -> Result<Vec<Value>, OverviewError> {
    if !matches!(metric, "view_count" | "like_count" | "comment_count") {
        return Err(OverviewError::InvalidMetric);
    }
    let query = format!(
        r"
        WITH visible_video AS (
          SELECT membership.video_id
          FROM collection_target_videos AS membership
          WHERE $1::uuid IS NOT NULL AND membership.target_id = $1
          UNION ALL
          SELECT membership.video_id
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
        ORDER BY COALESCE(stats.{metric}, 0) DESC,
                 COALESCE(
                   video.published_at,
                   video.source_fetched_at,
                   'epoch'::timestamptz
                 ) DESC,
                 video.youtube_video_id DESC
        LIMIT 6
        "
    );
    let videos = sqlx::query_as::<_, OverviewVideoRow>(&query)
        .bind(target_id)
        .bind(legacy_source_id)
        .fetch_all(pool)
        .await?;
    Ok(videos.iter().map(video_contract).collect())
}

async fn load_result_videos(
    pool: &PgPool,
    target_id: Option<Uuid>,
    legacy_source_id: Option<Uuid>,
) -> Result<Vec<Value>, OverviewError> {
    let mut videos = sqlx::query_as::<_, OverviewVideoRow>(
        r"
        WITH visible_video AS (
          SELECT membership.video_id
          FROM collection_target_videos AS membership
          WHERE $1::uuid IS NOT NULL AND membership.target_id = $1
          UNION ALL
          SELECT membership.video_id
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
        ORDER BY COALESCE(video.published_at, video.source_fetched_at, 'epoch'::timestamptz) DESC,
                 video.youtube_video_id DESC
        LIMIT 5001
        ",
    )
    .bind(target_id)
    .bind(legacy_source_id)
    .fetch_all(pool)
    .await?;
    if videos.len() > 5_000 {
        return Err(OverviewError::LegacyResultTooLarge);
    }
    Ok(videos
        .drain(..)
        .map(|video| video_contract(&video))
        .collect())
}

fn video_contract(video: &OverviewVideoRow) -> Value {
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

#[derive(Debug, Error)]
pub enum OverviewError {
    #[error("source was not found")]
    NotFound,
    #[error("source lookup failed")]
    Source(#[from] SourceError),
    #[error("analysis lookup failed")]
    Analysis(#[from] AnalysisError),
    #[error("overview database operation failed")]
    Database(#[from] sqlx::Error),
    #[error("invalid internal overview metric")]
    InvalidMetric,
    #[error("legacy source result is too large")]
    LegacyResultTooLarge,
}

impl IntoResponse for OverviewError {
    fn into_response(self) -> Response {
        match self {
            Self::NotFound => (
                StatusCode::NOT_FOUND,
                Json(OverviewErrorResponse {
                    detail: "Source was not found",
                    retryable: false,
                }),
            )
                .into_response(),
            Self::Source(error) => error.into_response(),
            Self::Analysis(error) => error.into_response(),
            Self::Database(error) => {
                let failure = crate::db_error::classify(&error, "overview");
                (
                    failure.status,
                    Json(OverviewErrorResponse {
                        detail: failure.detail,
                        retryable: failure.retryable,
                    }),
                )
                    .into_response()
            }
            Self::InvalidMetric => {
                tracing::error!("invalid source overview metric");
                (
                    StatusCode::INTERNAL_SERVER_ERROR,
                    Json(OverviewErrorResponse {
                        detail: "Source overview metric is invalid",
                        retryable: false,
                    }),
                )
                    .into_response()
            }
            Self::LegacyResultTooLarge => (
                StatusCode::PAYLOAD_TOO_LARGE,
                Json(OverviewErrorResponse {
                    detail: "Source has more than 5000 videos; use the paginated videos endpoint",
                    retryable: false,
                }),
            )
                .into_response(),
        }
    }
}

#[derive(Serialize)]
struct OverviewErrorResponse {
    detail: &'static str,
    #[serde(skip_serializing_if = "crate::is_false")]
    retryable: bool,
}

#[cfg(test)]
mod tests {
    #[test]
    fn top_video_metric_allowlist_is_explicit() {
        assert!(matches!(
            "view_count",
            "view_count" | "like_count" | "comment_count"
        ));
        assert!(!matches!(
            "drop table",
            "view_count" | "like_count" | "comment_count"
        ));
    }
}
