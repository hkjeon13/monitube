//! Persisted analysis preferences and pure-frequency read support.

use crate::{AppState, auth::AuthUser};
use axum::Json;
use axum::extract::{Extension, Path, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sqlx::PgPool;
use std::collections::HashSet;
use thiserror::Error;
use unicode_casefold::UnicodeCaseFold;
use unicode_normalization::UnicodeNormalization;
use uuid::Uuid;

use monitube_analysis::{FrequencyAggregate, rank_by_frequency};

const MAX_EXCLUDED_TERMS: usize = 250;
const MAX_EXCLUDED_TERM_CHARACTERS: usize = 64;

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExcludedTermsUpdate {
    #[serde(default)]
    terms: Vec<String>,
}

#[derive(Debug, Serialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct ExcludedTermsResponse {
    video_terms: Vec<String>,
    comment_terms: Vec<String>,
}

pub async fn list_excluded_terms(
    State(state): State<AppState>,
    Extension(user): Extension<AuthUser>,
) -> Result<Json<ExcludedTermsResponse>, AnalysisError> {
    load_excluded_terms(&state.pool, user.id).await.map(Json)
}

pub async fn replace_excluded_terms(
    State(state): State<AppState>,
    Extension(user): Extension<AuthUser>,
    Path(corpus_kind): Path<String>,
    Json(payload): Json<ExcludedTermsUpdate>,
) -> Result<Json<ExcludedTermsResponse>, AnalysisError> {
    if !matches!(corpus_kind.as_str(), "video" | "comment") {
        return Err(AnalysisError::InvalidCorpusKind);
    }
    let terms = normalize_excluded_terms(payload.terms)?;
    let mut transaction = state.pool.begin().await?;
    sqlx::query("SELECT id FROM app_users WHERE id = $1 FOR UPDATE")
        .bind(user.id)
        .fetch_one(&mut *transaction)
        .await?;
    sqlx::query("DELETE FROM analysis_excluded_terms WHERE user_id = $1 AND corpus_kind = $2")
        .bind(user.id)
        .bind(&corpus_kind)
        .execute(&mut *transaction)
        .await?;
    for term in terms {
        sqlx::query(
            "INSERT INTO analysis_excluded_terms (user_id, corpus_kind, term) VALUES ($1, $2, $3)",
        )
        .bind(user.id)
        .bind(&corpus_kind)
        .bind(term)
        .execute(&mut *transaction)
        .await?;
    }
    transaction.commit().await?;
    load_excluded_terms(&state.pool, user.id).await.map(Json)
}

async fn load_excluded_terms(
    pool: &PgPool,
    user_id: uuid::Uuid,
) -> Result<ExcludedTermsResponse, AnalysisError> {
    let rows = sqlx::query_as::<_, (String, String)>(
        r"
        SELECT corpus_kind, term
        FROM analysis_excluded_terms
        WHERE user_id = $1
        ORDER BY corpus_kind, term
        ",
    )
    .bind(user_id)
    .fetch_all(pool)
    .await?;
    let mut video_terms = Vec::new();
    let mut comment_terms = Vec::new();
    for (corpus_kind, term) in rows {
        match corpus_kind.as_str() {
            "video" => video_terms.push(term),
            "comment" => comment_terms.push(term),
            _ => tracing::warn!(%corpus_kind, "ignored unknown excluded-term corpus"),
        }
    }
    Ok(ExcludedTermsResponse {
        video_terms,
        comment_terms,
    })
}

pub(crate) async fn load_frequency_keywords(
    pool: &PgPool,
    user_id: Uuid,
    scope_kind: &str,
    scope_id: Uuid,
    corpus_kind: &str,
    excluded_corpus: &str,
    limit: usize,
) -> Result<(Vec<Value>, u64), AnalysisError> {
    let document_count = sqlx::query_scalar::<_, i64>(
        r"
        SELECT COALESCE(document_count, 0)::bigint
        FROM nlp_corpus_stats
        WHERE scope_kind = $1 AND scope_id = $2
          AND corpus_kind = $3 AND analyzer_version = $4
        ",
    )
    .bind(scope_kind)
    .bind(scope_id)
    .bind(corpus_kind)
    .bind(monitube_contracts::TOKENIZER_ANALYZER_VERSION)
    .fetch_optional(pool)
    .await?
    .unwrap_or(0);
    let candidate_limit = i64::try_from(limit.saturating_mul(4).max(50))
        .map_err(|_| AnalysisError::InvalidFrequencyLimit)?;
    let rows = sqlx::query_as::<_, (String, i64, i64)>(
        r"
        SELECT stats.term,
               stats.total_term_frequency,
               stats.document_frequency
        FROM nlp_term_stats AS stats
        WHERE stats.scope_kind = $1 AND stats.scope_id = $2
          AND stats.corpus_kind = $3 AND stats.analyzer_version = $4
          AND NOT EXISTS (
            SELECT 1
            FROM analysis_excluded_terms AS excluded
            WHERE excluded.user_id = $5
              AND excluded.corpus_kind = $6
              AND excluded.term = stats.term
          )
        ORDER BY stats.total_term_frequency DESC,
                 stats.term
        LIMIT $7
        ",
    )
    .bind(scope_kind)
    .bind(scope_id)
    .bind(corpus_kind)
    .bind(monitube_contracts::TOKENIZER_ANALYZER_VERSION)
    .bind(user_id)
    .bind(excluded_corpus)
    .bind(candidate_limit)
    .fetch_all(pool)
    .await?;
    let document_count = u64::try_from(document_count.max(0)).unwrap_or(0);
    let aggregates = rows
        .into_iter()
        .map(
            |(term, total_term_frequency, document_frequency)| FrequencyAggregate {
                term,
                total_term_frequency: u64::try_from(total_term_frequency.max(0)).unwrap_or(0),
                document_frequency: u64::try_from(document_frequency.max(0)).unwrap_or(0),
            },
        );
    let keywords = rank_by_frequency(aggregates, document_count, limit)
        .into_iter()
        .map(|item| {
            json!({
                "term": item.term,
                "termCount": item.term_count,
                "documentCount": item.document_count,
                "documentRate": item.document_rate,
            })
        })
        .collect();
    Ok((keywords, document_count))
}

fn normalize_excluded_terms(terms: Vec<String>) -> Result<Vec<String>, AnalysisError> {
    if terms.len() > MAX_EXCLUDED_TERMS {
        return Err(AnalysisError::InvalidExcludedTerms);
    }
    let mut seen = HashSet::new();
    let mut normalized = Vec::with_capacity(terms.len());
    for raw_term in terms {
        let nfc = raw_term.nfc().collect::<String>();
        let term = nfc.trim().case_fold().collect::<String>();
        let length = term.chars().count();
        if length == 0
            || length > MAX_EXCLUDED_TERM_CHARACTERS
            || term.chars().any(|character| u32::from(character) < 32)
        {
            return Err(AnalysisError::InvalidExcludedTerms);
        }
        if seen.insert(term.clone()) {
            normalized.push(term);
        }
    }
    Ok(normalized)
}

#[derive(Debug, Error)]
pub enum AnalysisError {
    #[error("analysis corpus kind is invalid")]
    InvalidCorpusKind,
    #[error("excluded terms are invalid")]
    InvalidExcludedTerms,
    #[error("frequency limit is invalid")]
    InvalidFrequencyLimit,
    #[error("analysis database operation failed")]
    Database(#[from] sqlx::Error),
}

impl IntoResponse for AnalysisError {
    fn into_response(self) -> Response {
        match self {
            Self::InvalidCorpusKind => (
                StatusCode::UNPROCESSABLE_ENTITY,
                Json(AnalysisErrorResponse {
                    detail: "corpus_kind must be video or comment",
                    retryable: false,
                }),
            )
                .into_response(),
            Self::InvalidExcludedTerms => (
                StatusCode::UNPROCESSABLE_ENTITY,
                Json(AnalysisErrorResponse {
                    detail: "Excluded terms must contain 1 to 64 visible characters",
                    retryable: false,
                }),
            )
                .into_response(),
            Self::InvalidFrequencyLimit => (
                StatusCode::UNPROCESSABLE_ENTITY,
                Json(AnalysisErrorResponse {
                    detail: "frequency limit is invalid",
                    retryable: false,
                }),
            )
                .into_response(),
            Self::Database(error) => {
                let failure = crate::db_error::classify(&error, "analysis");
                (
                    failure.status,
                    Json(AnalysisErrorResponse {
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
struct AnalysisErrorResponse {
    detail: &'static str,
    #[serde(skip_serializing_if = "crate::is_false")]
    retryable: bool,
}

#[cfg(test)]
mod tests {
    use super::{AnalysisError, normalize_excluded_terms};

    #[test]
    fn excluded_terms_are_nfc_casefolded_and_deduplicated() -> Result<(), AnalysisError> {
        let normalized = normalize_excluded_terms(vec![
            "  STRASSE  ".to_owned(),
            "Straße".to_owned(),
            "데이터".to_owned(),
        ])?;
        assert_eq!(normalized, vec!["strasse", "데이터"]);
        Ok(())
    }

    #[test]
    fn excluded_terms_reject_controls_and_oversized_lists() {
        assert!(normalize_excluded_terms(vec!["bad\u{0}term".to_owned()]).is_err());
        assert!(normalize_excluded_terms(vec!["term".to_owned(); 251]).is_err());
    }
}
