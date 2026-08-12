//! Bounded owner-scoped search over persisted trigram documents.

use crate::{AppState, auth::AuthUser};
use axum::Json;
use axum::extract::{Extension, Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use thiserror::Error;
use unicode_casefold::UnicodeCaseFold;
use unicode_normalization::UnicodeNormalization;
use uuid::Uuid;

#[derive(Debug, Deserialize)]
pub struct SearchQuery {
    q: String,
    limit: Option<usize>,
    scope: Option<String>,
}

pub async fn search(
    State(state): State<AppState>,
    Extension(user): Extension<AuthUser>,
    Query(query): Query<SearchQuery>,
) -> Result<Json<Value>, SearchError> {
    let normalized = normalize(&query.q);
    let limit = query.limit.unwrap_or(20);
    let scope = query.scope.as_deref().unwrap_or("all");
    if !(2..=200).contains(&query.q.chars().count())
        || normalized.chars().count() < 2
        || !(1..=50).contains(&limit)
        || !matches!(scope, "all" | "videos" | "comments")
    {
        return Err(SearchError::InvalidQuery);
    }
    let candidate_limit =
        i64::try_from((limit * 10).clamp(100, 300)).map_err(|_| SearchError::InvalidQuery)?;
    let short = normalized.chars().count() == 2;
    let videos = if matches!(scope, "all" | "videos") {
        search_videos(&state, user.id, &normalized, short, candidate_limit, limit).await?
    } else {
        Vec::new()
    };
    let comments = if matches!(scope, "all" | "comments") && !short {
        search_comments(&state, user.id, &normalized, candidate_limit, limit).await?
    } else {
        Vec::new()
    };
    Ok(Json(
        json!({"query": query.q, "videos": videos, "comments": comments}),
    ))
}

async fn search_videos(
    state: &AppState,
    user_id: Uuid,
    normalized: &str,
    short: bool,
    candidate_limit: i64,
    limit: usize,
) -> Result<Vec<Value>, SearchError> {
    let prefix = format!("{}%", escape_like(normalized));
    let contains = format!("%{}%", escape_like(normalized));
    let rows = sqlx::query_scalar::<_, Value>(
        r"
        WITH candidates AS MATERIALIZED (
          SELECT document.video_id,
                 CASE WHEN $3 THEN 1.0
                      ELSE similarity(document.document, $2) END AS score
          FROM video_search_documents AS document
          JOIN videos AS video ON video.id = document.video_id
          LEFT JOIN channels AS channel ON channel.id = video.channel_id
          WHERE EXISTS (
            SELECT 1 FROM collection_target_videos AS membership
            JOIN collection_subscriptions AS subscription
              ON subscription.target_id = membership.target_id
            WHERE membership.video_id = document.video_id
              AND subscription.user_id = $1
          )
            AND (
              ($3 AND (
                video.youtube_video_id ILIKE $4 ESCAPE '\'
                OR ltrim(COALESCE(channel.handle, ''), '@') ILIKE $4 ESCAPE '\'
                OR COALESCE(video.title, '') ILIKE $4 ESCAPE '\'
              ))
              OR (NOT $3 AND (
                document.document ILIKE $5 ESCAPE '\'
                OR document.document % $2
              ))
            )
          ORDER BY score DESC, video.source_fetched_at DESC NULLS LAST
          LIMIT $6
        )
        SELECT jsonb_build_object(
          'video', jsonb_build_object(
            'id', video.youtube_video_id, 'channelId', channel.youtube_channel_id,
            'title', video.title, 'description', video.description,
            'publishedAt', video.published_at,
            'durationSeconds', GREATEST(video.duration_seconds, 0),
            'privacyStatus', video.privacy_status, 'madeForKids', video.made_for_kids,
            'statistics', jsonb_build_object(
              'viewCount', GREATEST(COALESCE(stats.view_count, 0), 0),
              'likeCount', GREATEST(COALESCE(stats.like_count, 0), 0),
              'commentCount', GREATEST(COALESCE(stats.comment_count, 0), 0)),
            'fetchedAt', video.source_fetched_at),
          'score', LEAST(GREATEST(candidate.score, 0), 1),
          'matchedFields', array_remove(ARRAY[
            CASE WHEN lower(video.youtube_video_id) LIKE '%' || $2 || '%' THEN 'id' END,
            CASE WHEN lower(COALESCE(video.title, '')) LIKE '%' || $2 || '%' THEN 'title' END,
            CASE WHEN lower(COALESCE(video.description, '')) LIKE '%' || $2 || '%' THEN 'description' END,
            CASE WHEN lower(COALESCE(channel.title, '')) LIKE '%' || $2 || '%' THEN 'channel' END,
            CASE WHEN lower(COALESCE(channel.handle, '')) LIKE '%' || $2 || '%' THEN 'handle' END
          ], NULL),
          'transcriptSnippet', NULL)
        FROM candidates AS candidate
        JOIN videos AS video ON video.id = candidate.video_id
        LEFT JOIN channels AS channel ON channel.id = video.channel_id
        LEFT JOIN LATERAL (
          SELECT snapshot.view_count, snapshot.like_count, snapshot.comment_count
          FROM video_stat_snapshots AS snapshot WHERE snapshot.video_id = video.id
          ORDER BY snapshot.fetched_at DESC LIMIT 1
        ) AS stats ON TRUE
        ORDER BY candidate.score DESC, video.source_fetched_at DESC NULLS LAST
        ",
    )
    .bind(user_id)
    .bind(normalized)
    .bind(short)
    .bind(prefix)
    .bind(contains)
    .bind(candidate_limit)
    .fetch_all(&state.pool)
    .await?;
    Ok(rows.into_iter().take(limit).collect())
}

async fn search_comments(
    state: &AppState,
    user_id: Uuid,
    normalized: &str,
    candidate_limit: i64,
    limit: usize,
) -> Result<Vec<Value>, SearchError> {
    let contains = format!("%{}%", escape_like(normalized));
    let rows = sqlx::query_scalar::<_, Value>(
        r"
        WITH candidates AS MATERIALIZED (
          SELECT comment.id,
                 similarity(lower(COALESCE(comment.text_display, '')), $2) AS score
          FROM comments AS comment
          WHERE comment.text_display IS NOT NULL
            AND comment.deleted_at IS NULL
            AND (comment.expires_at IS NULL OR comment.expires_at > now())
            AND EXISTS (
              SELECT 1 FROM collection_target_videos AS membership
              JOIN collection_subscriptions AS subscription
                ON subscription.target_id = membership.target_id
              WHERE membership.video_id = comment.video_id
                AND subscription.user_id = $1
            )
            AND (lower(comment.text_display) ILIKE $3 ESCAPE '\'
                 OR lower(comment.text_display) % $2)
          ORDER BY score DESC, comment.source_fetched_at DESC NULLS LAST
          LIMIT $4
        )
        SELECT jsonb_build_object(
          'comment', jsonb_build_object(
            'id', comment.youtube_comment_id, 'videoId', video.youtube_video_id,
            'parentCommentId', comment.youtube_parent_comment_id,
            'threadId', comment.youtube_thread_id, 'text', comment.text_display,
            'likeCount', GREATEST(comment.like_count, 0),
            'publishedAt', comment.published_at, 'updatedAt', comment.updated_at,
            'fetchedAt', comment.source_fetched_at,
            'authorChannelId', comment.author_channel_id,
            'authorName', comment.author_display_name),
          'video', jsonb_build_object(
            'id', video.youtube_video_id, 'channelId', channel.youtube_channel_id,
            'title', video.title, 'description', video.description,
            'publishedAt', video.published_at,
            'durationSeconds', GREATEST(video.duration_seconds, 0),
            'privacyStatus', video.privacy_status, 'madeForKids', video.made_for_kids,
            'statistics', jsonb_build_object(
              'viewCount', GREATEST(COALESCE(stats.view_count, 0), 0),
              'likeCount', GREATEST(COALESCE(stats.like_count, 0), 0),
              'commentCount', GREATEST(COALESCE(stats.comment_count, 0), 0)),
            'fetchedAt', video.source_fetched_at),
          'channelTitle', channel.title,
          'score', LEAST(GREATEST(candidate.score, 0), 1),
          'matchedFields', ARRAY['comment'])
        FROM candidates AS candidate
        JOIN comments AS comment ON comment.id = candidate.id
        JOIN videos AS video ON video.id = comment.video_id
        LEFT JOIN channels AS channel ON channel.id = video.channel_id
        LEFT JOIN LATERAL (
          SELECT snapshot.view_count, snapshot.like_count, snapshot.comment_count
          FROM video_stat_snapshots AS snapshot WHERE snapshot.video_id = video.id
          ORDER BY snapshot.fetched_at DESC LIMIT 1
        ) AS stats ON TRUE
        ORDER BY candidate.score DESC, comment.source_fetched_at DESC NULLS LAST
        ",
    )
    .bind(user_id)
    .bind(normalized)
    .bind(contains)
    .bind(candidate_limit)
    .fetch_all(&state.pool)
    .await?;
    Ok(rows.into_iter().take(limit).collect())
}

fn normalize(value: &str) -> String {
    value
        .nfkc()
        .case_fold()
        .filter(|character| character.is_alphanumeric())
        .collect()
}

fn escape_like(value: &str) -> String {
    value
        .replace('\\', "\\\\")
        .replace('%', "\\%")
        .replace('_', "\\_")
}

#[derive(Debug, Error)]
pub enum SearchError {
    #[error("search query is invalid")]
    InvalidQuery,
    #[error("search database operation failed")]
    Database(#[from] sqlx::Error),
}

impl IntoResponse for SearchError {
    fn into_response(self) -> Response {
        let (status, detail, retryable) = match self {
            Self::InvalidQuery => (
                StatusCode::UNPROCESSABLE_ENTITY,
                "Search query, scope, or limit is invalid",
                false,
            ),
            Self::Database(error) => {
                let failure = crate::db_error::classify(&error, "search");
                (failure.status, failure.detail, failure.retryable)
            }
        };
        (status, Json(SearchErrorResponse { detail, retryable })).into_response()
    }
}

#[derive(Serialize)]
struct SearchErrorResponse {
    detail: &'static str,
    #[serde(skip_serializing_if = "crate::is_false")]
    retryable: bool,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalization_matches_compact_casefold_search_contract() {
        assert_eq!(normalize(" 가 A-B_1 "), "가ab1");
    }
}
