//! Leased NLP queue access and atomic bag-of-words persistence.

mod complete;

pub use complete::{CompleteDocument, CompletionOutcome, complete_document};

use chrono::{DateTime, NaiveDate, Utc};
use sqlx::{FromRow, PgPool};
use thiserror::Error;
use uuid::Uuid;

const CLAIM_DELETE: &str = r"
    WITH candidate AS (
      SELECT source_kind, source_id, state AS requested_state
      FROM nlp_documents
      WHERE state = 'delete_pending'
      ORDER BY updated_at, source_id
      FOR UPDATE SKIP LOCKED
      LIMIT 1
    )
    UPDATE nlp_documents AS document
       SET state = 'running', lease_owner = $1,
           lease_expires_at = now() + ($2 * interval '1 second'),
           updated_at = now()
      FROM candidate
     WHERE document.source_kind = candidate.source_kind
       AND document.source_id = candidate.source_id
    RETURNING document.source_kind, document.source_id, document.video_id,
              document.source_hash, document.source_date, document.comment_type,
              document.indexed_comment_type, document.analyzer_version,
              document.token_count, document.indexed_at,
              candidate.requested_state
    ";

const CLAIM_EXPIRED: &str = r"
    WITH candidate AS (
      SELECT source_kind, source_id, state AS requested_state
      FROM nlp_documents
      WHERE state = 'running' AND lease_expires_at < now()
      ORDER BY lease_expires_at, updated_at, source_id
      FOR UPDATE SKIP LOCKED
      LIMIT 1
    )
    UPDATE nlp_documents AS document
       SET state = 'running', lease_owner = $1,
           lease_expires_at = now() + ($2 * interval '1 second'),
           updated_at = now()
      FROM candidate
     WHERE document.source_kind = candidate.source_kind
       AND document.source_id = candidate.source_id
    RETURNING document.source_kind, document.source_id, document.video_id,
              document.source_hash, document.source_date, document.comment_type,
              document.indexed_comment_type, document.analyzer_version,
              document.token_count, document.indexed_at,
              candidate.requested_state
    ";

const CLAIM_PENDING: &str = r"
    WITH candidate AS (
      SELECT source_kind, source_id, state AS requested_state
      FROM nlp_documents
      WHERE state = 'pending'
      ORDER BY updated_at, source_id
      FOR UPDATE SKIP LOCKED
      LIMIT 1
    )
    UPDATE nlp_documents AS document
       SET state = 'running', lease_owner = $1,
           lease_expires_at = now() + ($2 * interval '1 second'),
           updated_at = now()
      FROM candidate
     WHERE document.source_kind = candidate.source_kind
       AND document.source_id = candidate.source_id
    RETURNING document.source_kind, document.source_id, document.video_id,
              document.source_hash, document.source_date, document.comment_type,
              document.indexed_comment_type, document.analyzer_version,
              document.token_count, document.indexed_at,
              candidate.requested_state
    ";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ClaimedDocument {
    pub source_kind: String,
    pub source_id: Uuid,
    pub video_id: Uuid,
    pub source_hash: String,
    pub source_date: Option<NaiveDate>,
    pub comment_type: Option<String>,
    pub action: DocumentAction,
    pub text: String,
    pub segments: Vec<DocumentSegment>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DocumentAction {
    Index,
    Delete,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DocumentSegment {
    pub sequence: i32,
    pub text: String,
}

#[derive(Debug, Clone, FromRow)]
pub(crate) struct NlpDocumentRow {
    pub source_kind: String,
    pub source_id: Uuid,
    pub video_id: Uuid,
    pub source_hash: String,
    pub source_date: Option<NaiveDate>,
    pub comment_type: Option<String>,
    pub indexed_comment_type: Option<String>,
    pub analyzer_version: Option<String>,
    pub token_count: i32,
    pub indexed_at: Option<DateTime<Utc>>,
    pub requested_state: Option<String>,
}

/// Claims the highest-priority NLP document using index-aligned queue scans.
///
/// # Errors
///
/// Returns an error for invalid lease bounds or any database failure.
pub async fn claim_next_document(
    pool: &PgPool,
    worker_id: &str,
    lease_seconds: i64,
) -> Result<Option<ClaimedDocument>, NlpStoreError> {
    if worker_id.trim().is_empty() || !(30..=3_600).contains(&lease_seconds) {
        return Err(NlpStoreError::InvalidLease);
    }

    let mut claimed = None;
    for statement in [CLAIM_DELETE, CLAIM_EXPIRED, CLAIM_PENDING] {
        claimed = sqlx::query_as::<_, NlpDocumentRow>(statement)
            .bind(worker_id)
            .bind(lease_seconds)
            .fetch_optional(pool)
            .await?;
        if claimed.is_some() {
            break;
        }
    }
    let Some(row) = claimed else {
        return Ok(None);
    };
    hydrate_claim(pool, row).await.map(Some)
}

async fn hydrate_claim(
    pool: &PgPool,
    row: NlpDocumentRow,
) -> Result<ClaimedDocument, NlpStoreError> {
    let requested_delete = row.requested_state.as_deref() == Some("delete_pending");
    let (action, text, segments) = if requested_delete {
        (DocumentAction::Delete, String::new(), Vec::new())
    } else if row.source_kind == "transcript" {
        let source = sqlx::query_scalar::<_, String>(
            "SELECT full_text FROM video_transcripts WHERE id = $1 AND state = 'available'",
        )
        .bind(row.source_id)
        .fetch_optional(pool)
        .await?;
        let Some(text) = source.filter(|text| !text.trim().is_empty()) else {
            return Ok(delete_claim(row));
        };
        let segments = sqlx::query_as::<_, (i32, String)>(
            "SELECT sequence, text FROM video_transcript_segments WHERE transcript_id = $1 ORDER BY sequence",
        )
        .bind(row.source_id)
        .fetch_all(pool)
        .await?
        .into_iter()
        .map(|(sequence, text)| DocumentSegment { sequence, text })
        .collect();
        (DocumentAction::Index, text, segments)
    } else if row.source_kind == "comment" {
        let source = sqlx::query_scalar::<_, String>(
            "SELECT text_display FROM comments WHERE id = $1 AND deleted_at IS NULL",
        )
        .bind(row.source_id)
        .fetch_optional(pool)
        .await?;
        let Some(text) = source.filter(|text| !text.trim().is_empty()) else {
            return Ok(delete_claim(row));
        };
        (DocumentAction::Index, text, Vec::new())
    } else {
        return Err(NlpStoreError::UnknownSourceKind(row.source_kind));
    };

    Ok(ClaimedDocument {
        source_kind: row.source_kind,
        source_id: row.source_id,
        video_id: row.video_id,
        source_hash: row.source_hash,
        source_date: row.source_date,
        comment_type: row.comment_type,
        action,
        text,
        segments,
    })
}

fn delete_claim(row: NlpDocumentRow) -> ClaimedDocument {
    ClaimedDocument {
        source_kind: row.source_kind,
        source_id: row.source_id,
        video_id: row.video_id,
        source_hash: row.source_hash,
        source_date: row.source_date,
        comment_type: row.comment_type,
        action: DocumentAction::Delete,
        text: String::new(),
        segments: Vec::new(),
    }
}

/// Releases a worker-owned lease into retry or terminal failure state.
///
/// # Errors
///
/// Returns a database error when the durable failure state cannot be recorded.
pub async fn fail_document(
    pool: &PgPool,
    document: &ClaimedDocument,
    worker_id: &str,
    error_code: &str,
) -> Result<bool, NlpStoreError> {
    let result = sqlx::query(
        r"
        UPDATE nlp_documents
        SET state = CASE WHEN retry_count >= 4 THEN 'failed' ELSE 'pending' END,
            retry_count = retry_count + 1,
            error_code = left($1, 500), lease_owner = NULL,
            lease_expires_at = NULL, updated_at = now()
        WHERE source_kind = $2 AND source_id = $3
          AND state = 'running' AND lease_owner = $4
        ",
    )
    .bind(error_code)
    .bind(&document.source_kind)
    .bind(document.source_id)
    .bind(worker_id)
    .execute(pool)
    .await?;
    Ok(result.rows_affected() == 1)
}

/// Requeues ready or failed rows created by an older analyzer version.
///
/// # Errors
///
/// Returns a database error if the queue cannot be updated.
pub async fn enqueue_stale_documents(
    pool: &PgPool,
    analyzer_version: &str,
) -> Result<u64, NlpStoreError> {
    let result = sqlx::query(
        r"
        UPDATE nlp_documents
        SET state = 'pending', retry_count = 0, error_code = NULL,
            lease_owner = NULL, lease_expires_at = NULL, updated_at = now()
        WHERE state IN ('ready', 'failed')
          AND analyzer_version IS DISTINCT FROM $1
        ",
    )
    .bind(analyzer_version)
    .execute(pool)
    .await?;
    Ok(result.rows_affected())
}

#[derive(Debug, Error)]
pub enum NlpStoreError {
    #[error("NLP lease owner or duration is invalid")]
    InvalidLease,
    #[error("unknown NLP source kind: {0}")]
    UnknownSourceKind(String),
    #[error("NLP database operation failed")]
    Database(#[from] sqlx::Error),
    #[error("NLP term or token count exceeds the database integer bound")]
    CountOutOfRange,
    #[error("NLP aggregate invariant failed: {0}")]
    AggregateInvariant(&'static str),
}

#[cfg(test)]
mod tests {
    use super::{NlpDocumentRow, delete_claim};
    use chrono::NaiveDate;
    use uuid::Uuid;

    #[test]
    fn missing_source_becomes_delete_without_losing_identity() {
        let source_id = Uuid::new_v4();
        let claim = delete_claim(NlpDocumentRow {
            source_kind: "comment".to_owned(),
            source_id,
            video_id: Uuid::new_v4(),
            source_hash: "hash".to_owned(),
            source_date: NaiveDate::from_ymd_opt(2026, 8, 12),
            comment_type: Some("top_level".to_owned()),
            indexed_comment_type: None,
            analyzer_version: None,
            token_count: 0,
            indexed_at: None,
            requested_state: Some("pending".to_owned()),
        });
        assert_eq!(claim.source_id, source_id);
        assert!(claim.text.is_empty());
    }
}
