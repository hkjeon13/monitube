//! Bounded owner-scoped comment reads over persisted rows and `BoW` terms.

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
use sqlx::FromRow;
use std::collections::BTreeMap;
use thiserror::Error;
use uuid::Uuid;

#[derive(Debug, FromRow)]
struct VideoRow {
    id: Uuid,
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
struct CommentRow {
    youtube_comment_id: String,
    youtube_video_id: String,
    youtube_parent_comment_id: Option<String>,
    youtube_thread_id: Option<String>,
    text_display: Option<String>,
    like_count: i32,
    published_at: Option<DateTime<Utc>>,
    updated_at: Option<DateTime<Utc>>,
    source_fetched_at: DateTime<Utc>,
    author_channel_id: Option<String>,
    author_display_name: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct RepliesQuery {
    cursor: Option<String>,
    limit: Option<usize>,
}

#[derive(Debug, Deserialize, Serialize)]
struct CommentCursor {
    at: DateTime<Utc>,
    id: String,
}

#[derive(Debug, Deserialize)]
pub struct ThreadQuery {
    cursor: Option<String>,
    limit: Option<usize>,
    sort: Option<String>,
}

#[derive(Debug, Deserialize, Serialize)]
struct ThreadCursor {
    sort: String,
    at: DateTime<Utc>,
    id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    likes: Option<i32>,
}

pub async fn get_video_comments(
    State(state): State<AppState>,
    Extension(user): Extension<AuthUser>,
    Path(video_id): Path<String>,
) -> Result<Json<Value>, CommentError> {
    let video = load_video(&state, user.id, &video_id).await?;
    let comments = sqlx::query_as::<_, CommentRow>(&comment_projection(
        r"
        WHERE comment.video_id = $1
        ORDER BY COALESCE(
                   comment.published_at,
                   comment.source_fetched_at,
                   'epoch'::timestamptz
                 ) DESC,
                 comment.youtube_comment_id DESC
        LIMIT 100
        ",
    ))
    .bind(video.id)
    .fetch_all(&state.pool)
    .await?;
    let (total, latest) = sqlx::query_as::<_, (i64, Option<DateTime<Utc>>)>(
        r"
        SELECT count(*)::bigint,
               max(COALESCE(published_at, source_fetched_at))
        FROM comments
        WHERE video_id = $1
        ",
    )
    .bind(video.id)
    .fetch_one(&state.pool)
    .await?;
    let top_words = load_video_top_words(&state, user.id, video.id).await?;
    Ok(Json(json!({
        "video": video_contract(&video),
        "comments": comments.iter().map(comment_contract).collect::<Vec<_>>(),
        "summary": {
            "total": total.max(0),
            "latestPublishedAt": latest,
            "topWords": top_words,
        }
    })))
}

pub async fn get_comment_replies(
    State(state): State<AppState>,
    Extension(user): Extension<AuthUser>,
    Path(comment_id): Path<String>,
    Query(query): Query<RepliesQuery>,
) -> Result<Json<Value>, CommentError> {
    let limit = query.limit.unwrap_or(20);
    if !(1..=100).contains(&limit) {
        return Err(CommentError::InvalidLimit);
    }
    let cursor = decode_cursor(query.cursor.as_deref())?;
    let root_comment_id = sqlx::query_scalar::<_, String>(
        r"
        SELECT COALESCE(comment.youtube_parent_comment_id, comment.youtube_comment_id)
        FROM comments AS comment
        JOIN videos AS video ON video.id = comment.video_id
        WHERE comment.youtube_comment_id = $1
          AND EXISTS (
            SELECT 1
            FROM collection_target_videos AS membership
            JOIN collection_subscriptions AS subscription
              ON subscription.target_id = membership.target_id
            WHERE membership.video_id = video.id AND subscription.user_id = $2
          )
        ",
    )
    .bind(comment_id)
    .bind(user.id)
    .fetch_optional(&state.pool)
    .await?
    .ok_or(CommentError::NotFound)?;
    let after_at = cursor.as_ref().map(|value| value.at);
    let after_id = cursor.as_ref().map(|value| value.id.as_str());
    let fetch_limit = i64::try_from(limit + 1).map_err(|_| CommentError::InvalidLimit)?;
    let mut comments = sqlx::query_as::<_, CommentRow>(&comment_projection(
        r"
        WHERE comment.youtube_parent_comment_id = $1
          AND (
            $2::timestamptz IS NULL
            OR (
              COALESCE(
                comment.published_at,
                comment.source_fetched_at,
                'epoch'::timestamptz
              ),
              comment.youtube_comment_id
            ) > ($2, $3)
          )
        ORDER BY COALESCE(
                   comment.published_at,
                   comment.source_fetched_at,
                   'epoch'::timestamptz
                 ) ASC,
                 comment.youtube_comment_id ASC
        LIMIT $4
        ",
    ))
    .bind(root_comment_id)
    .bind(after_at)
    .bind(after_id)
    .bind(fetch_limit)
    .fetch_all(&state.pool)
    .await?;
    let has_more = comments.len() > limit;
    comments.truncate(limit);
    let next_cursor = if has_more {
        comments.last().map(encode_cursor).transpose()?
    } else {
        None
    };
    Ok(Json(json!({
        "comments": comments.iter().map(comment_contract).collect::<Vec<_>>(),
        "nextCursor": next_cursor,
    })))
}

pub async fn get_comment_detail(
    State(state): State<AppState>,
    Extension(user): Extension<AuthUser>,
    Path(comment_id): Path<String>,
) -> Result<Json<Value>, CommentError> {
    let comment = sqlx::query_as::<_, CommentRow>(&comment_projection(
        r"
        WHERE comment.youtube_comment_id = $1
          AND EXISTS (
            SELECT 1
            FROM collection_target_videos AS membership
            JOIN collection_subscriptions AS subscription
              ON subscription.target_id = membership.target_id
            WHERE membership.video_id = video.id AND subscription.user_id = $2
          )
        ",
    ))
    .bind(&comment_id)
    .bind(user.id)
    .fetch_optional(&state.pool)
    .await?
    .ok_or(CommentError::NotFound)?;
    let video = load_video(&state, user.id, &comment.youtube_video_id).await?;
    let root_comment_id = comment
        .youtube_parent_comment_id
        .as_deref()
        .unwrap_or(&comment.youtube_comment_id);
    let parent = if comment.youtube_parent_comment_id.is_some() {
        sqlx::query_as::<_, CommentRow>(&comment_projection(
            "WHERE comment.youtube_comment_id = $1",
        ))
        .bind(root_comment_id)
        .fetch_optional(&state.pool)
        .await?
    } else {
        None
    };
    let reply_rows = sqlx::query_as::<_, (Value, i64)>(
        r"
        WITH ranked AS (
          SELECT jsonb_build_object(
                   'id', comment.youtube_comment_id,
                   'videoId', video.youtube_video_id,
                   'parentCommentId', comment.youtube_parent_comment_id,
                   'threadId', comment.youtube_thread_id,
                   'text', comment.text_display,
                   'likeCount', GREATEST(comment.like_count, 0),
                   'publishedAt', comment.published_at,
                   'updatedAt', comment.updated_at,
                   'fetchedAt', COALESCE(comment.source_fetched_at, 'epoch'::timestamptz),
                   'authorChannelId', comment.author_channel_id,
                   'authorName', comment.author_display_name
                 ) AS contract,
                 comment.youtube_comment_id,
                 count(*) OVER ()::bigint AS stored_reply_count,
                 row_number() OVER (
                   ORDER BY COALESCE(
                              comment.published_at,
                              comment.source_fetched_at,
                              'epoch'::timestamptz
                            ),
                            comment.youtube_comment_id
                 ) AS reply_rank
          FROM comments AS comment
          JOIN videos AS video ON video.id = comment.video_id
          WHERE comment.youtube_parent_comment_id = $1
        )
        SELECT contract, stored_reply_count
        FROM ranked
        WHERE reply_rank <= 2 OR youtube_comment_id = $2
        ORDER BY reply_rank
        ",
    )
    .bind(root_comment_id)
    .bind(&comment.youtube_comment_id)
    .fetch_all(&state.pool)
    .await?;
    let stored_reply_count = reply_rows.first().map_or(0, |row| row.1.max(0));
    let replies = reply_rows.into_iter().map(|row| row.0).collect::<Vec<_>>();
    let author_comments = if let Some(author_channel_id) = comment.author_channel_id.as_deref() {
        load_author_comments(
            &state,
            user.id,
            author_channel_id,
            &comment.youtube_comment_id,
            root_comment_id,
        )
        .await?
    } else {
        Vec::new()
    };
    Ok(Json(json!({
        "comment": comment_contract(&comment),
        "video": video_contract(&video),
        "parentComment": parent.as_ref().map(comment_contract),
        "storedReplyCount": stored_reply_count,
        "replies": replies,
        "authorComments": author_comments,
    })))
}

#[allow(clippy::too_many_lines)]
pub async fn get_video_comment_threads(
    State(state): State<AppState>,
    Extension(user): Extension<AuthUser>,
    Path(video_id): Path<String>,
    Query(query): Query<ThreadQuery>,
) -> Result<Json<Value>, CommentError> {
    let limit = query.limit.unwrap_or(20);
    if !(1..=100).contains(&limit) {
        return Err(CommentError::InvalidLimit);
    }
    let sort = query.sort.as_deref().unwrap_or("newest");
    if !matches!(sort, "newest" | "oldest" | "recommended") {
        return Err(CommentError::InvalidSort);
    }
    let cursor = decode_thread_cursor(query.cursor.as_deref(), sort)?;
    let video = load_video(&state, user.id, &video_id).await?;
    let after_at = cursor.as_ref().map(|value| value.at);
    let after_id = cursor.as_ref().map(|value| value.id.as_str());
    let after_likes = cursor.as_ref().and_then(|value| value.likes);
    let fetch_limit = i64::try_from(limit + 1).map_err(|_| CommentError::InvalidLimit)?;
    let suffix = match sort {
        "oldest" => {
            r"
          WHERE comment.video_id = $1
            AND comment.youtube_parent_comment_id IS NULL
            AND $4::integer IS NULL
            AND (
              $2::timestamptz IS NULL
              OR (
                COALESCE(comment.published_at, comment.source_fetched_at, 'epoch'::timestamptz),
                comment.youtube_comment_id
              ) > ($2, $3)
            )
          ORDER BY COALESCE(comment.published_at, comment.source_fetched_at, 'epoch'::timestamptz),
                   comment.youtube_comment_id
          LIMIT $5
        "
        }
        "recommended" => {
            r"
          WHERE comment.video_id = $1
            AND comment.youtube_parent_comment_id IS NULL
            AND (
              $2::timestamptz IS NULL
              OR (
                COALESCE(comment.like_count, 0),
                COALESCE(comment.published_at, comment.source_fetched_at, 'epoch'::timestamptz),
                comment.youtube_comment_id
              ) < ($4, $2, $3)
            )
          ORDER BY COALESCE(comment.like_count, 0) DESC,
                   COALESCE(comment.published_at, comment.source_fetched_at, 'epoch'::timestamptz) DESC,
                   comment.youtube_comment_id DESC
          LIMIT $5
        "
        }
        _ => {
            r"
          WHERE comment.video_id = $1
            AND comment.youtube_parent_comment_id IS NULL
            AND $4::integer IS NULL
            AND (
              $2::timestamptz IS NULL
              OR (
                COALESCE(comment.published_at, comment.source_fetched_at, 'epoch'::timestamptz),
                comment.youtube_comment_id
              ) < ($2, $3)
            )
          ORDER BY COALESCE(comment.published_at, comment.source_fetched_at, 'epoch'::timestamptz) DESC,
                   comment.youtube_comment_id DESC
          LIMIT $5
        "
        }
    };
    let mut comments = sqlx::query_as::<_, CommentRow>(&comment_projection(suffix))
        .bind(video.id)
        .bind(after_at)
        .bind(after_id)
        .bind(after_likes)
        .bind(fetch_limit)
        .fetch_all(&state.pool)
        .await?;
    let has_more = comments.len() > limit;
    comments.truncate(limit);
    let parent_ids = comments
        .iter()
        .map(|comment| comment.youtube_comment_id.clone())
        .collect::<Vec<_>>();
    let reply_rows = if parent_ids.is_empty() {
        Vec::new()
    } else {
        sqlx::query_as::<_, (String, Value, i64)>(
            r"
            WITH ranked AS (
              SELECT comment.youtube_parent_comment_id,
                     jsonb_build_object(
                       'id', comment.youtube_comment_id,
                       'videoId', video.youtube_video_id,
                       'parentCommentId', comment.youtube_parent_comment_id,
                       'threadId', comment.youtube_thread_id,
                       'text', comment.text_display,
                       'likeCount', GREATEST(comment.like_count, 0),
                       'publishedAt', comment.published_at,
                       'updatedAt', comment.updated_at,
                       'fetchedAt', COALESCE(comment.source_fetched_at, 'epoch'::timestamptz),
                       'authorChannelId', comment.author_channel_id,
                       'authorName', comment.author_display_name
                     ) AS contract,
                     count(*) OVER (
                       PARTITION BY comment.youtube_parent_comment_id
                     )::bigint AS reply_count,
                     row_number() OVER (
                       PARTITION BY comment.youtube_parent_comment_id
                       ORDER BY COALESCE(
                                  comment.published_at,
                                  comment.source_fetched_at,
                                  'epoch'::timestamptz
                                ),
                                comment.youtube_comment_id
                     ) AS reply_rank
              FROM comments AS comment
              JOIN videos AS video ON video.id = comment.video_id
              WHERE comment.youtube_parent_comment_id = ANY($1)
            )
            SELECT youtube_parent_comment_id, contract, reply_count
            FROM ranked
            WHERE reply_rank <= 2
            ORDER BY youtube_parent_comment_id, reply_rank
            ",
        )
        .bind(&parent_ids)
        .fetch_all(&state.pool)
        .await?
    };
    let mut reply_groups: BTreeMap<String, (Vec<Value>, i64)> = BTreeMap::new();
    for (parent_id, reply, count) in reply_rows {
        let entry = reply_groups.entry(parent_id).or_default();
        entry.0.push(reply);
        entry.1 = count.max(0);
    }
    let items = comments
        .iter()
        .map(|comment| {
            let (replies, count) = reply_groups
                .get(&comment.youtube_comment_id)
                .cloned()
                .unwrap_or_default();
            json!({
                "comment": comment_contract(comment),
                "repliesPreview": replies,
                "storedReplyCount": count,
            })
        })
        .collect::<Vec<_>>();
    let next_cursor = if has_more {
        comments
            .last()
            .map(|comment| encode_thread_cursor(comment, sort))
            .transpose()?
    } else {
        None
    };
    Ok(Json(json!({
        "video": video_contract(&video),
        "sort": sort,
        "items": items,
        "nextCursor": next_cursor,
    })))
}

async fn load_author_comments(
    state: &AppState,
    user_id: Uuid,
    author_channel_id: &str,
    selected_comment_id: &str,
    root_comment_id: &str,
) -> Result<Vec<Value>, CommentError> {
    sqlx::query_scalar::<_, Value>(
        r"
        SELECT jsonb_build_object(
          'comment', jsonb_build_object(
            'id', comment.youtube_comment_id,
            'videoId', video.youtube_video_id,
            'parentCommentId', comment.youtube_parent_comment_id,
            'threadId', comment.youtube_thread_id,
            'text', comment.text_display,
            'likeCount', GREATEST(comment.like_count, 0),
            'publishedAt', comment.published_at,
            'updatedAt', comment.updated_at,
            'fetchedAt', COALESCE(comment.source_fetched_at, 'epoch'::timestamptz),
            'authorChannelId', comment.author_channel_id,
            'authorName', comment.author_display_name
          ),
          'video', jsonb_build_object(
            'id', video.youtube_video_id,
            'channelId', channel.youtube_channel_id,
            'title', video.title,
            'description', video.description,
            'publishedAt', video.published_at,
            'durationSeconds', video.duration_seconds,
            'privacyStatus', video.privacy_status,
            'madeForKids', video.made_for_kids,
            'statistics', jsonb_build_object(
              'viewCount', COALESCE(stats.view_count, 0),
              'likeCount', COALESCE(stats.like_count, 0),
              'commentCount', COALESCE(stats.comment_count, 0)
            ),
            'fetchedAt', COALESCE(video.source_fetched_at, 'epoch'::timestamptz)
          ),
          'channelTitle', channel.title
        )
        FROM comments AS comment
        JOIN videos AS video ON video.id = comment.video_id
        LEFT JOIN channels AS channel ON channel.id = video.channel_id
        LEFT JOIN LATERAL (
          SELECT snapshot.view_count, snapshot.like_count, snapshot.comment_count
          FROM video_stat_snapshots AS snapshot
          WHERE snapshot.video_id = video.id
          ORDER BY snapshot.fetched_at DESC
          LIMIT 1
        ) AS stats ON TRUE
        WHERE comment.author_channel_id = $1
          AND comment.youtube_comment_id <> $2
          AND comment.youtube_parent_comment_id IS DISTINCT FROM $3
          AND EXISTS (
            SELECT 1
            FROM collection_target_videos AS membership
            JOIN collection_subscriptions AS subscription
              ON subscription.target_id = membership.target_id
            WHERE membership.video_id = video.id AND subscription.user_id = $4
          )
        ORDER BY comment.published_at DESC NULLS LAST,
                 comment.source_fetched_at DESC
        LIMIT 50
        ",
    )
    .bind(author_channel_id)
    .bind(selected_comment_id)
    .bind(root_comment_id)
    .bind(user_id)
    .fetch_all(&state.pool)
    .await
    .map_err(CommentError::Database)
}

async fn load_video(
    state: &AppState,
    user_id: Uuid,
    video_id: &str,
) -> Result<VideoRow, CommentError> {
    sqlx::query_as::<_, VideoRow>(
        r"
        SELECT video.id,
               video.youtube_video_id,
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
        FROM videos AS video
        LEFT JOIN channels AS channel ON channel.id = video.channel_id
        LEFT JOIN LATERAL (
          SELECT snapshot.view_count, snapshot.like_count, snapshot.comment_count
          FROM video_stat_snapshots AS snapshot
          WHERE snapshot.video_id = video.id
          ORDER BY snapshot.fetched_at DESC
          LIMIT 1
        ) AS stats ON TRUE
        WHERE (video.youtube_video_id = $1 OR video.id::text = $1)
          AND EXISTS (
            SELECT 1
            FROM collection_target_videos AS membership
            JOIN collection_subscriptions AS subscription
              ON subscription.target_id = membership.target_id
            WHERE membership.video_id = video.id AND subscription.user_id = $2
          )
        ",
    )
    .bind(video_id)
    .bind(user_id)
    .fetch_optional(&state.pool)
    .await?
    .ok_or(CommentError::NotFound)
}

fn comment_projection(suffix: &str) -> String {
    format!(
        r"
        SELECT comment.youtube_comment_id,
               video.youtube_video_id,
               comment.youtube_parent_comment_id,
               comment.youtube_thread_id,
               comment.text_display,
               comment.like_count,
               comment.published_at,
               comment.updated_at,
               COALESCE(comment.source_fetched_at, 'epoch'::timestamptz)
                 AS source_fetched_at,
               comment.author_channel_id,
               comment.author_display_name
        FROM comments AS comment
        JOIN videos AS video ON video.id = comment.video_id
        {suffix}
        "
    )
}

async fn load_video_top_words(
    state: &AppState,
    user_id: Uuid,
    video_id: Uuid,
) -> Result<Vec<Value>, CommentError> {
    let rows = sqlx::query_as::<_, (String, i64)>(
        r"
        SELECT term.term,
               sum(term.term_frequency)::bigint AS term_count
        FROM comments AS comment
        JOIN nlp_documents AS document
          ON document.source_kind = 'comment'
         AND document.source_id = comment.id
         AND document.state = 'ready'
         AND document.analyzer_version = $1
        JOIN nlp_document_terms AS term
          ON term.source_kind = document.source_kind
         AND term.source_id = document.source_id
        WHERE comment.video_id = $2
          AND NOT EXISTS (
            SELECT 1
            FROM analysis_excluded_terms AS excluded
            WHERE excluded.user_id = $3
              AND excluded.corpus_kind = 'comment'
              AND excluded.term = term.term
          )
        GROUP BY term.term
        ORDER BY term_count DESC, term.term
        LIMIT 20
        ",
    )
    .bind(monitube_contracts::TOKENIZER_ANALYZER_VERSION)
    .bind(video_id)
    .bind(user_id)
    .fetch_all(&state.pool)
    .await?;
    Ok(rows
        .into_iter()
        .filter(|(_, count)| *count > 0)
        .map(|(word, count)| json!({"word": word, "count": count}))
        .collect())
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

fn comment_contract(comment: &CommentRow) -> Value {
    json!({
        "id": comment.youtube_comment_id,
        "videoId": comment.youtube_video_id,
        "parentCommentId": comment.youtube_parent_comment_id,
        "threadId": comment.youtube_thread_id,
        "text": comment.text_display,
        "likeCount": i64::from(comment.like_count).max(0),
        "publishedAt": comment.published_at,
        "updatedAt": comment.updated_at,
        "fetchedAt": comment.source_fetched_at,
        "authorChannelId": comment.author_channel_id,
        "authorName": comment.author_display_name,
    })
}

fn decode_cursor(encoded: Option<&str>) -> Result<Option<CommentCursor>, CommentError> {
    let Some(encoded) = encoded else {
        return Ok(None);
    };
    if encoded.len() > 512 {
        return Err(CommentError::InvalidCursor);
    }
    let bytes = URL_SAFE_NO_PAD
        .decode(encoded)
        .map_err(|_| CommentError::InvalidCursor)?;
    let cursor =
        serde_json::from_slice::<CommentCursor>(&bytes).map_err(|_| CommentError::InvalidCursor)?;
    if cursor.id.is_empty() {
        return Err(CommentError::InvalidCursor);
    }
    Ok(Some(cursor))
}

fn encode_cursor(comment: &CommentRow) -> Result<String, CommentError> {
    let cursor = CommentCursor {
        at: comment.published_at.unwrap_or(comment.source_fetched_at),
        id: comment.youtube_comment_id.clone(),
    };
    serde_json::to_vec(&cursor)
        .map(|bytes| URL_SAFE_NO_PAD.encode(bytes))
        .map_err(CommentError::CursorSerialization)
}

fn decode_thread_cursor(
    encoded: Option<&str>,
    requested_sort: &str,
) -> Result<Option<ThreadCursor>, CommentError> {
    let Some(encoded) = encoded else {
        return Ok(None);
    };
    if encoded.len() > 512 {
        return Err(CommentError::InvalidCursor);
    }
    let bytes = URL_SAFE_NO_PAD
        .decode(encoded)
        .map_err(|_| CommentError::InvalidCursor)?;
    let cursor =
        serde_json::from_slice::<ThreadCursor>(&bytes).map_err(|_| CommentError::InvalidCursor)?;
    if cursor.id.is_empty()
        || cursor.sort != requested_sort
        || (requested_sort == "recommended" && cursor.likes.is_none())
    {
        return Err(CommentError::InvalidCursor);
    }
    Ok(Some(cursor))
}

fn encode_thread_cursor(comment: &CommentRow, sort: &str) -> Result<String, CommentError> {
    let cursor = ThreadCursor {
        sort: sort.to_owned(),
        at: comment.published_at.unwrap_or(comment.source_fetched_at),
        id: comment.youtube_comment_id.clone(),
        likes: (sort == "recommended").then_some(comment.like_count.max(0)),
    };
    serde_json::to_vec(&cursor)
        .map(|bytes| URL_SAFE_NO_PAD.encode(bytes))
        .map_err(CommentError::CursorSerialization)
}

#[derive(Debug, Error)]
pub enum CommentError {
    #[error("comment or video was not found")]
    NotFound,
    #[error("comment cursor is invalid")]
    InvalidCursor,
    #[error("comment limit is invalid")]
    InvalidLimit,
    #[error("comment thread sort is invalid")]
    InvalidSort,
    #[error("comment cursor serialization failed")]
    CursorSerialization(serde_json::Error),
    #[error("comment database operation failed")]
    Database(#[from] sqlx::Error),
}

impl IntoResponse for CommentError {
    fn into_response(self) -> Response {
        let (status, detail, retryable) = match self {
            Self::NotFound => (
                StatusCode::NOT_FOUND,
                "Comment or video was not found",
                false,
            ),
            Self::InvalidCursor => (
                StatusCode::UNPROCESSABLE_ENTITY,
                "Invalid comment cursor",
                false,
            ),
            Self::InvalidLimit => (
                StatusCode::UNPROCESSABLE_ENTITY,
                "limit must be between 1 and 100",
                false,
            ),
            Self::InvalidSort => (
                StatusCode::UNPROCESSABLE_ENTITY,
                "sort must be newest, oldest, or recommended",
                false,
            ),
            Self::CursorSerialization(error) => {
                tracing::error!(%error, "comment cursor serialization failed");
                (
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "Comment cursor could not be created",
                    false,
                )
            }
            Self::Database(error) => {
                let failure = crate::db_error::classify(&error, "comment");
                (failure.status, failure.detail, failure.retryable)
            }
        };
        (status, Json(CommentErrorResponse { detail, retryable })).into_response()
    }
}

#[derive(Serialize)]
struct CommentErrorResponse {
    detail: &'static str,
    #[serde(skip_serializing_if = "crate::is_false")]
    retryable: bool,
}

#[cfg(test)]
mod tests {
    use super::*;

    fn comment() -> Result<CommentRow, chrono::ParseError> {
        Ok(CommentRow {
            youtube_comment_id: "comment-1".to_owned(),
            youtube_video_id: "video-1".to_owned(),
            youtube_parent_comment_id: None,
            youtube_thread_id: Some("thread-1".to_owned()),
            text_display: Some("text".to_owned()),
            like_count: 7,
            published_at: Some("2026-08-13T01:02:03Z".parse()?),
            updated_at: None,
            source_fetched_at: "2026-08-13T01:03:00Z".parse()?,
            author_channel_id: None,
            author_display_name: None,
        })
    }

    #[test]
    fn thread_cursor_round_trips_and_is_bound_to_sort() -> Result<(), Box<dyn std::error::Error>> {
        let encoded = encode_thread_cursor(&comment()?, "recommended")?;
        let decoded = decode_thread_cursor(Some(&encoded), "recommended")?
            .ok_or(CommentError::InvalidCursor)?;

        assert_eq!(decoded.sort, "recommended");
        assert_eq!(decoded.id, "comment-1");
        assert_eq!(decoded.likes, Some(7));
        assert!(matches!(
            decode_thread_cursor(Some(&encoded), "newest"),
            Err(CommentError::InvalidCursor)
        ));
        Ok(())
    }

    #[test]
    fn non_recommended_cursor_does_not_include_likes() -> Result<(), Box<dyn std::error::Error>> {
        let encoded = encode_thread_cursor(&comment()?, "newest")?;
        let decoded =
            decode_thread_cursor(Some(&encoded), "newest")?.ok_or(CommentError::InvalidCursor)?;

        assert_eq!(decoded.likes, None);
        Ok(())
    }
}
