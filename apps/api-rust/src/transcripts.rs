//! Owner-scoped video transcript reads.

use crate::{AppState, auth::AuthUser};
use axum::Json;
use axum::extract::{Extension, Path, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use chrono::{DateTime, Utc};
use serde::Serialize;
use sqlx::FromRow;
use thiserror::Error;
use uuid::Uuid;

#[derive(Debug, FromRow)]
struct TranscriptRow {
    id: Uuid,
    video_id: String,
    provider: String,
    requested_language: String,
    resolved_language: Option<String>,
    language_name: Option<String>,
    selection_reason: Option<String>,
    transcript_type: Option<String>,
    is_auto_generated: Option<bool>,
    is_translated: Option<bool>,
    state: String,
    full_text: Option<String>,
    fetched_at: Option<DateTime<Utc>>,
    last_attempted_at: DateTime<Utc>,
    error_code: Option<String>,
}

#[derive(Debug, FromRow, Serialize)]
#[serde(rename_all = "camelCase")]
struct TranscriptSegment {
    sequence: i32,
    start_ms: i32,
    duration_ms: i32,
    text: String,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct TranscriptResponse {
    video_id: String,
    provider: String,
    requested_language: String,
    resolved_language: Option<String>,
    language_name: Option<String>,
    selection_reason: Option<String>,
    transcript_type: Option<String>,
    is_auto_generated: Option<bool>,
    is_translated: Option<bool>,
    state: String,
    full_text: Option<String>,
    fetched_at: Option<DateTime<Utc>>,
    last_attempted_at: DateTime<Utc>,
    error_code: Option<String>,
    segments: Vec<TranscriptSegment>,
}

pub async fn get_video_transcript(
    State(state): State<AppState>,
    Extension(user): Extension<AuthUser>,
    Path(video_id): Path<String>,
) -> Result<Json<TranscriptResponse>, TranscriptError> {
    let transcript = sqlx::query_as::<_, TranscriptRow>(
        r"
        SELECT transcript.id,
               video.youtube_video_id AS video_id,
               transcript.provider,
               transcript.requested_language,
               transcript.resolved_language,
               transcript.language_name,
               transcript.selection_reason,
               transcript.transcript_type,
               transcript.is_auto_generated,
               transcript.is_translated,
               transcript.state,
               transcript.full_text,
               transcript.fetched_at,
               transcript.last_attempted_at,
               transcript.error_code
        FROM video_transcripts AS transcript
        JOIN videos AS video ON video.id = transcript.video_id
        WHERE video.youtube_video_id = $1
          AND EXISTS (
            SELECT 1
            FROM collection_target_videos AS membership
            JOIN collection_subscriptions AS subscription
              ON subscription.target_id = membership.target_id
            WHERE membership.video_id = video.id
              AND subscription.user_id = $2
          )
        ",
    )
    .bind(&video_id)
    .bind(user.id)
    .fetch_optional(&state.pool)
    .await?
    .ok_or(TranscriptError::NotFound)?;
    let segments = sqlx::query_as::<_, TranscriptSegment>(
        r"
        SELECT sequence, start_ms, duration_ms, text
        FROM video_transcript_segments
        WHERE transcript_id = $1
        ORDER BY sequence
        ",
    )
    .bind(transcript.id)
    .fetch_all(&state.pool)
    .await?;

    Ok(Json(TranscriptResponse {
        video_id: transcript.video_id,
        provider: transcript.provider,
        requested_language: transcript.requested_language,
        resolved_language: transcript.resolved_language,
        language_name: transcript.language_name,
        selection_reason: transcript.selection_reason,
        transcript_type: transcript.transcript_type,
        is_auto_generated: transcript.is_auto_generated,
        is_translated: transcript.is_translated,
        state: transcript.state,
        full_text: transcript.full_text,
        fetched_at: transcript.fetched_at,
        last_attempted_at: transcript.last_attempted_at,
        error_code: transcript.error_code,
        segments,
    }))
}

#[derive(Debug, Error)]
pub enum TranscriptError {
    #[error("transcript was not found")]
    NotFound,
    #[error("transcript database operation failed")]
    Database(#[from] sqlx::Error),
}

impl IntoResponse for TranscriptError {
    fn into_response(self) -> Response {
        let (status, detail, retryable) = match self {
            Self::NotFound => (StatusCode::NOT_FOUND, "Transcript was not found", false),
            Self::Database(error) => {
                let failure = crate::db_error::classify(&error, "transcript");
                (failure.status, failure.detail, failure.retryable)
            }
        };
        (status, Json(TranscriptErrorResponse { detail, retryable })).into_response()
    }
}

#[derive(Serialize)]
struct TranscriptErrorResponse {
    detail: &'static str,
    #[serde(skip_serializing_if = "crate::is_false")]
    retryable: bool,
}

#[cfg(test)]
mod tests {
    use super::{TranscriptResponse, TranscriptSegment};
    use chrono::Utc;

    #[test]
    fn transcript_contract_uses_python_field_names() -> Result<(), serde_json::Error> {
        let response = TranscriptResponse {
            video_id: "video-1".to_owned(),
            provider: "youtube".to_owned(),
            requested_language: "ko".to_owned(),
            resolved_language: Some("ko".to_owned()),
            language_name: None,
            selection_reason: None,
            transcript_type: None,
            is_auto_generated: Some(false),
            is_translated: Some(false),
            state: "available".to_owned(),
            full_text: Some("내용".to_owned()),
            fetched_at: Some(Utc::now()),
            last_attempted_at: Utc::now(),
            error_code: None,
            segments: vec![TranscriptSegment {
                sequence: 0,
                start_ms: 10,
                duration_ms: 20,
                text: "내용".to_owned(),
            }],
        };
        let value = serde_json::to_value(response)?;
        assert_eq!(value["videoId"], "video-1");
        assert_eq!(value["segments"][0]["startMs"], 10);
        assert!(value.get("requested_language").is_none());
        Ok(())
    }
}
