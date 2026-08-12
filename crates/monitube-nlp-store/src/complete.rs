use crate::{ClaimedDocument, NlpDocumentRow, NlpStoreError};
use chrono::NaiveDate;
use monitube_analysis::BagOfWords;
use sqlx::{FromRow, PgPool, Postgres, Transaction};
use std::collections::BTreeMap;
use uuid::Uuid;

#[derive(Debug)]
pub struct CompleteDocument<'a> {
    pub claim: &'a ClaimedDocument,
    pub worker_id: &'a str,
    pub analyzer_version: &'a str,
    pub bag: Option<&'a BagOfWords>,
    pub segment_terms: &'a BTreeMap<i32, Vec<String>>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CompletionOutcome {
    Completed,
    Deleted,
    Superseded,
    LeaseLost,
}

#[derive(Debug, FromRow)]
struct ScopeRow {
    scope_kind: String,
    scope_id: Uuid,
    document_date: Option<NaiveDate>,
    membership_ref_count: i32,
}

/// Replaces one document's sparse bag-of-words and all aggregate deltas atomically.
///
/// A completion is fenced by the active lease owner. The tokenizer never writes
/// to the database; only the Rust worker invokes this transaction.
///
/// # Errors
///
/// Returns an error when counters exceed database bounds, aggregate invariants
/// are missing, or a database statement fails.
#[allow(clippy::too_many_lines)]
pub async fn complete_document(
    pool: &PgPool,
    completion: CompleteDocument<'_>,
) -> Result<CompletionOutcome, NlpStoreError> {
    let mut transaction = pool.begin().await?;
    let row = sqlx::query_as::<_, NlpDocumentRow>(
        r"
        SELECT source_kind, source_id, video_id, source_hash, source_date,
               comment_type, indexed_comment_type, analyzer_version,
               token_count, indexed_at, NULL::text AS requested_state
        FROM nlp_documents
        WHERE source_kind = $1 AND source_id = $2
          AND state = 'running' AND lease_owner = $3
        FOR UPDATE
        ",
    )
    .bind(&completion.claim.source_kind)
    .bind(completion.claim.source_id)
    .bind(completion.worker_id)
    .fetch_optional(&mut *transaction)
    .await?;
    let Some(row) = row else {
        transaction.rollback().await?;
        return Ok(CompletionOutcome::LeaseLost);
    };

    if completion.bag.is_some() && row.source_hash != completion.claim.source_hash {
        sqlx::query(
            r"
            UPDATE nlp_documents
            SET state = 'pending', lease_owner = NULL,
                lease_expires_at = NULL, updated_at = now()
            WHERE source_kind = $1 AND source_id = $2 AND lease_owner = $3
            ",
        )
        .bind(&completion.claim.source_kind)
        .bind(completion.claim.source_id)
        .bind(completion.worker_id)
        .execute(&mut *transaction)
        .await?;
        transaction.commit().await?;
        return Ok(CompletionOutcome::Superseded);
    }

    let old_terms = sqlx::query_as::<_, (String, i32)>(
        r"
        SELECT term, term_frequency
        FROM nlp_document_terms
        WHERE source_kind = $1 AND source_id = $2
        ORDER BY term
        ",
    )
    .bind(&completion.claim.source_kind)
    .bind(completion.claim.source_id)
    .fetch_all(&mut *transaction)
    .await?;
    let old_scopes = sqlx::query_as::<_, ScopeRow>(
        r"
        SELECT scope_kind, scope_id, document_date, membership_ref_count
        FROM nlp_scope_documents
        WHERE source_kind = $1 AND source_id = $2
        ORDER BY scope_kind, scope_id
        FOR UPDATE
        ",
    )
    .bind(&completion.claim.source_kind)
    .bind(completion.claim.source_id)
    .fetch_all(&mut *transaction)
    .await?;

    if row.indexed_at.is_some() {
        if let Some(old_version) = row.analyzer_version.as_deref() {
            for scope in &old_scopes {
                for corpus_kind in
                    corpus_kinds(&row.source_kind, row.indexed_comment_type.as_deref())
                {
                    apply_corpus_delta(
                        &mut transaction,
                        scope,
                        corpus_kind,
                        old_version,
                        &old_terms,
                        i64::from(row.token_count),
                        DeltaDirection::Remove,
                    )
                    .await?;
                }
            }
        }
    }

    sqlx::query("DELETE FROM nlp_scope_documents WHERE source_kind = $1 AND source_id = $2")
        .bind(&completion.claim.source_kind)
        .bind(completion.claim.source_id)
        .execute(&mut *transaction)
        .await?;
    sqlx::query("DELETE FROM nlp_document_terms WHERE source_kind = $1 AND source_id = $2")
        .bind(&completion.claim.source_kind)
        .bind(completion.claim.source_id)
        .execute(&mut *transaction)
        .await?;

    let Some(bag) = completion.bag else {
        sqlx::query(
            "DELETE FROM nlp_documents WHERE source_kind = $1 AND source_id = $2 AND lease_owner = $3",
        )
        .bind(&completion.claim.source_kind)
        .bind(completion.claim.source_id)
        .bind(completion.worker_id)
        .execute(&mut *transaction)
        .await?;
        transaction.commit().await?;
        return Ok(CompletionOutcome::Deleted);
    };

    let token_count =
        i32::try_from(bag.token_count()).map_err(|_| NlpStoreError::CountOutOfRange)?;
    let (new_terms, new_frequencies) = database_terms(bag)?;
    sqlx::query(
        r"
        INSERT INTO nlp_document_terms (source_kind, source_id, term, term_frequency)
        SELECT $1, $2, input.term, input.frequency
        FROM unnest($3::text[], $4::integer[]) AS input(term, frequency)
        ",
    )
    .bind(&completion.claim.source_kind)
    .bind(completion.claim.source_id)
    .bind(&new_terms)
    .bind(&new_frequencies)
    .execute(&mut *transaction)
    .await?;

    let mut new_scopes = current_scope_memberships(&mut transaction, row.video_id).await?;
    for scope in &mut new_scopes {
        scope.document_date = row.source_date;
    }
    let new_term_pairs = new_terms
        .iter()
        .cloned()
        .zip(new_frequencies.iter().copied())
        .collect::<Vec<_>>();
    for scope in &new_scopes {
        sqlx::query(
            r"
            INSERT INTO nlp_scope_documents (
              scope_kind, scope_id, source_kind, source_id,
              document_date, membership_ref_count
            ) VALUES ($1, $2, $3, $4, $5, $6)
            ",
        )
        .bind(&scope.scope_kind)
        .bind(scope.scope_id)
        .bind(&completion.claim.source_kind)
        .bind(completion.claim.source_id)
        .bind(row.source_date)
        .bind(scope.membership_ref_count)
        .execute(&mut *transaction)
        .await?;
        for corpus_kind in corpus_kinds(&row.source_kind, row.comment_type.as_deref()) {
            apply_corpus_delta(
                &mut transaction,
                scope,
                corpus_kind,
                completion.analyzer_version,
                &new_term_pairs,
                i64::from(token_count),
                DeltaDirection::Add,
            )
            .await?;
        }
    }

    if row.source_kind == "transcript" {
        persist_segment_terms(
            &mut transaction,
            row.source_id,
            completion.analyzer_version,
            completion.segment_terms,
        )
        .await?;
    }

    let result = sqlx::query(
        r"
        UPDATE nlp_documents
        SET analyzer_version = $1, state = 'ready', token_count = $2,
            indexed_source_date = source_date,
            indexed_comment_type = comment_type,
            indexed_at = now(), retry_count = 0, error_code = NULL,
            lease_owner = NULL, lease_expires_at = NULL, updated_at = now()
        WHERE source_kind = $3 AND source_id = $4
          AND state = 'running' AND lease_owner = $5
        ",
    )
    .bind(completion.analyzer_version)
    .bind(token_count)
    .bind(&completion.claim.source_kind)
    .bind(completion.claim.source_id)
    .bind(completion.worker_id)
    .execute(&mut *transaction)
    .await?;
    if result.rows_affected() != 1 {
        return Err(NlpStoreError::AggregateInvariant(
            "lease disappeared during completion",
        ));
    }
    transaction.commit().await?;
    Ok(CompletionOutcome::Completed)
}

fn database_terms(bag: &BagOfWords) -> Result<(Vec<String>, Vec<i32>), NlpStoreError> {
    let mut terms = Vec::with_capacity(bag.terms().len());
    let mut frequencies = Vec::with_capacity(bag.terms().len());
    for (term, frequency) in bag.terms() {
        terms.push(term.clone());
        frequencies.push(i32::try_from(*frequency).map_err(|_| NlpStoreError::CountOutOfRange)?);
    }
    Ok((terms, frequencies))
}

async fn current_scope_memberships(
    transaction: &mut Transaction<'_, Postgres>,
    video_id: Uuid,
) -> Result<Vec<ScopeRow>, NlpStoreError> {
    sqlx::query_as::<_, ScopeRow>(
        r"
        SELECT 'target'::text AS scope_kind,
               membership.target_id AS scope_id,
               NULL::date AS document_date,
               1::integer AS membership_ref_count
        FROM collection_target_videos AS membership
        WHERE membership.video_id = $1
        UNION ALL
        SELECT 'owner'::text AS scope_kind,
               subscription.user_id AS scope_id,
               NULL::date AS document_date,
               count(DISTINCT membership.target_id)::integer AS membership_ref_count
        FROM collection_target_videos AS membership
        JOIN collection_subscriptions AS subscription
          ON subscription.target_id = membership.target_id
         AND subscription.enabled = TRUE
        WHERE membership.video_id = $1
        GROUP BY subscription.user_id
        ORDER BY scope_kind, scope_id
        ",
    )
    .bind(video_id)
    .fetch_all(&mut **transaction)
    .await
    .map_err(NlpStoreError::Database)
}

fn corpus_kinds(source_kind: &str, comment_type: Option<&str>) -> Vec<&'static str> {
    if source_kind == "transcript" {
        vec!["video"]
    } else if comment_type == Some("reply") {
        vec!["comment", "comment_reply"]
    } else {
        vec!["comment", "comment_top_level"]
    }
}

#[derive(Clone, Copy)]
enum DeltaDirection {
    Add,
    Remove,
}

#[allow(clippy::too_many_arguments)]
#[allow(clippy::too_many_lines)]
async fn apply_corpus_delta(
    transaction: &mut Transaction<'_, Postgres>,
    scope: &ScopeRow,
    corpus_kind: &str,
    analyzer_version: &str,
    terms: &[(String, i32)],
    token_count: i64,
    direction: DeltaDirection,
) -> Result<(), NlpStoreError> {
    let term_names = terms
        .iter()
        .map(|(term, _)| term.clone())
        .collect::<Vec<_>>();
    let frequencies = terms
        .iter()
        .map(|(_, frequency)| i64::from(*frequency))
        .collect::<Vec<_>>();
    match direction {
        DeltaDirection::Add => {
            sqlx::query(
                r"
                INSERT INTO nlp_corpus_stats (
                  scope_kind, scope_id, corpus_kind, analyzer_version,
                  document_count, total_token_count, stats_version, updated_at
                ) VALUES ($1, $2, $3, $4, 1, $5, 1, now())
                ON CONFLICT (scope_kind, scope_id, corpus_kind, analyzer_version)
                DO UPDATE SET
                  document_count = nlp_corpus_stats.document_count + 1,
                  total_token_count = nlp_corpus_stats.total_token_count + EXCLUDED.total_token_count,
                  stats_version = nlp_corpus_stats.stats_version + 1,
                  updated_at = now()
                ",
            )
            .bind(&scope.scope_kind)
            .bind(scope.scope_id)
            .bind(corpus_kind)
            .bind(analyzer_version)
            .bind(token_count)
            .execute(&mut **transaction)
            .await?;
            sqlx::query(
                r"
                INSERT INTO nlp_term_stats (
                  scope_kind, scope_id, corpus_kind, analyzer_version,
                  term, document_frequency, total_term_frequency, updated_at
                )
                SELECT $1, $2, $3, $4, input.term, 1, input.frequency, now()
                FROM unnest($5::text[], $6::bigint[]) AS input(term, frequency)
                ON CONFLICT (scope_kind, scope_id, corpus_kind, analyzer_version, term)
                DO UPDATE SET
                  document_frequency = nlp_term_stats.document_frequency + 1,
                  total_term_frequency = nlp_term_stats.total_term_frequency + EXCLUDED.total_term_frequency,
                  updated_at = now()
                ",
            )
            .bind(&scope.scope_kind)
            .bind(scope.scope_id)
            .bind(corpus_kind)
            .bind(analyzer_version)
            .bind(&term_names)
            .bind(&frequencies)
            .execute(&mut **transaction)
            .await?;
        }
        DeltaDirection::Remove => {
            require_one(
                sqlx::query(
                    r"
                    UPDATE nlp_corpus_stats
                    SET document_count = document_count - 1,
                        total_token_count = total_token_count - $1,
                        stats_version = stats_version + 1,
                        updated_at = now()
                    WHERE scope_kind = $2 AND scope_id = $3
                      AND corpus_kind = $4 AND analyzer_version = $5
                    ",
                )
                .bind(token_count)
                .bind(&scope.scope_kind)
                .bind(scope.scope_id)
                .bind(corpus_kind)
                .bind(analyzer_version)
                .execute(&mut **transaction)
                .await?
                .rows_affected(),
                "missing corpus aggregate during removal",
            )?;
            let updated_terms = sqlx::query(
                r"
                UPDATE nlp_term_stats AS stats
                SET document_frequency = stats.document_frequency - 1,
                    total_term_frequency = stats.total_term_frequency - input.frequency,
                    updated_at = now()
                FROM unnest($5::text[], $6::bigint[]) AS input(term, frequency)
                WHERE stats.scope_kind = $1 AND stats.scope_id = $2
                  AND stats.corpus_kind = $3 AND stats.analyzer_version = $4
                  AND stats.term = input.term
                ",
            )
            .bind(&scope.scope_kind)
            .bind(scope.scope_id)
            .bind(corpus_kind)
            .bind(analyzer_version)
            .bind(&term_names)
            .bind(&frequencies)
            .execute(&mut **transaction)
            .await?
            .rows_affected();
            require_all(
                updated_terms,
                term_names.len(),
                "missing term aggregate during removal",
            )?;
            sqlx::query(
                r"
                DELETE FROM nlp_term_stats
                WHERE scope_kind = $1 AND scope_id = $2
                  AND corpus_kind = $3 AND analyzer_version = $4
                  AND document_frequency = 0 AND total_term_frequency = 0
                ",
            )
            .bind(&scope.scope_kind)
            .bind(scope.scope_id)
            .bind(corpus_kind)
            .bind(analyzer_version)
            .execute(&mut **transaction)
            .await?;
        }
    }

    if let Some(document_date) = scope.document_date {
        apply_daily_delta(
            transaction,
            scope,
            corpus_kind,
            analyzer_version,
            document_date,
            &term_names,
            &frequencies,
            token_count,
            direction,
        )
        .await?;
    }
    if matches!(direction, DeltaDirection::Remove) {
        sqlx::query(
            r"
            DELETE FROM nlp_corpus_stats
            WHERE scope_kind = $1 AND scope_id = $2
              AND corpus_kind = $3 AND analyzer_version = $4
              AND document_count = 0 AND total_token_count = 0
            ",
        )
        .bind(&scope.scope_kind)
        .bind(scope.scope_id)
        .bind(corpus_kind)
        .bind(analyzer_version)
        .execute(&mut **transaction)
        .await?;
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
#[allow(clippy::too_many_lines)]
async fn apply_daily_delta(
    transaction: &mut Transaction<'_, Postgres>,
    scope: &ScopeRow,
    corpus_kind: &str,
    analyzer_version: &str,
    document_date: NaiveDate,
    term_names: &[String],
    frequencies: &[i64],
    token_count: i64,
    direction: DeltaDirection,
) -> Result<(), NlpStoreError> {
    match direction {
        DeltaDirection::Add => {
            sqlx::query(
                r"
                INSERT INTO nlp_daily_corpus_stats (
                  scope_kind, scope_id, corpus_kind, analyzer_version,
                  document_date, document_count, total_token_count, updated_at
                ) VALUES ($1, $2, $3, $4, $5, 1, $6, now())
                ON CONFLICT (scope_kind, scope_id, corpus_kind, analyzer_version, document_date)
                DO UPDATE SET
                  document_count = nlp_daily_corpus_stats.document_count + 1,
                  total_token_count = nlp_daily_corpus_stats.total_token_count + EXCLUDED.total_token_count,
                  updated_at = now()
                ",
            )
            .bind(&scope.scope_kind)
            .bind(scope.scope_id)
            .bind(corpus_kind)
            .bind(analyzer_version)
            .bind(document_date)
            .bind(token_count)
            .execute(&mut **transaction)
            .await?;
            sqlx::query(
                r"
                INSERT INTO nlp_daily_term_stats (
                  scope_kind, scope_id, corpus_kind, analyzer_version,
                  document_date, term, document_frequency,
                  total_term_frequency, updated_at
                )
                SELECT $1, $2, $3, $4, $5, input.term, 1, input.frequency, now()
                FROM unnest($6::text[], $7::bigint[]) AS input(term, frequency)
                ON CONFLICT (
                  scope_kind, scope_id, corpus_kind, analyzer_version,
                  document_date, term
                ) DO UPDATE SET
                  document_frequency = nlp_daily_term_stats.document_frequency + 1,
                  total_term_frequency = nlp_daily_term_stats.total_term_frequency + EXCLUDED.total_term_frequency,
                  updated_at = now()
                ",
            )
            .bind(&scope.scope_kind)
            .bind(scope.scope_id)
            .bind(corpus_kind)
            .bind(analyzer_version)
            .bind(document_date)
            .bind(term_names)
            .bind(frequencies)
            .execute(&mut **transaction)
            .await?;
        }
        DeltaDirection::Remove => {
            let updated_terms = sqlx::query(
                r"
                UPDATE nlp_daily_term_stats AS stats
                SET document_frequency = stats.document_frequency - 1,
                    total_term_frequency = stats.total_term_frequency - input.frequency,
                    updated_at = now()
                FROM unnest($6::text[], $7::bigint[]) AS input(term, frequency)
                WHERE stats.scope_kind = $1 AND stats.scope_id = $2
                  AND stats.corpus_kind = $3 AND stats.analyzer_version = $4
                  AND stats.document_date = $5 AND stats.term = input.term
                ",
            )
            .bind(&scope.scope_kind)
            .bind(scope.scope_id)
            .bind(corpus_kind)
            .bind(analyzer_version)
            .bind(document_date)
            .bind(term_names)
            .bind(frequencies)
            .execute(&mut **transaction)
            .await?
            .rows_affected();
            require_all(
                updated_terms,
                term_names.len(),
                "missing daily term aggregate during removal",
            )?;
            sqlx::query(
                r"
                DELETE FROM nlp_daily_term_stats
                WHERE scope_kind = $1 AND scope_id = $2
                  AND corpus_kind = $3 AND analyzer_version = $4
                  AND document_date = $5
                  AND document_frequency = 0 AND total_term_frequency = 0
                ",
            )
            .bind(&scope.scope_kind)
            .bind(scope.scope_id)
            .bind(corpus_kind)
            .bind(analyzer_version)
            .bind(document_date)
            .execute(&mut **transaction)
            .await?;
            require_one(
                sqlx::query(
                    r"
                    UPDATE nlp_daily_corpus_stats
                    SET document_count = document_count - 1,
                        total_token_count = total_token_count - $1,
                        updated_at = now()
                    WHERE scope_kind = $2 AND scope_id = $3
                      AND corpus_kind = $4 AND analyzer_version = $5
                      AND document_date = $6
                    ",
                )
                .bind(token_count)
                .bind(&scope.scope_kind)
                .bind(scope.scope_id)
                .bind(corpus_kind)
                .bind(analyzer_version)
                .bind(document_date)
                .execute(&mut **transaction)
                .await?
                .rows_affected(),
                "missing daily corpus aggregate during removal",
            )?;
            sqlx::query(
                r"
                DELETE FROM nlp_daily_corpus_stats
                WHERE scope_kind = $1 AND scope_id = $2
                  AND corpus_kind = $3 AND analyzer_version = $4
                  AND document_date = $5
                  AND document_count = 0 AND total_token_count = 0
                ",
            )
            .bind(&scope.scope_kind)
            .bind(scope.scope_id)
            .bind(corpus_kind)
            .bind(analyzer_version)
            .bind(document_date)
            .execute(&mut **transaction)
            .await?;
        }
    }
    Ok(())
}

async fn persist_segment_terms(
    transaction: &mut Transaction<'_, Postgres>,
    transcript_id: Uuid,
    analyzer_version: &str,
    segment_terms: &BTreeMap<i32, Vec<String>>,
) -> Result<(), NlpStoreError> {
    sqlx::query(
        "UPDATE video_transcript_segments SET search_terms = '{}', analyzer_version = $1 WHERE transcript_id = $2",
    )
    .bind(analyzer_version)
    .bind(transcript_id)
    .execute(&mut **transaction)
    .await?;
    for (sequence, terms) in segment_terms {
        let mut unique = terms.clone();
        unique.sort();
        unique.dedup();
        sqlx::query(
            r"
            UPDATE video_transcript_segments
            SET search_terms = $1, analyzer_version = $2
            WHERE transcript_id = $3 AND sequence = $4
            ",
        )
        .bind(unique)
        .bind(analyzer_version)
        .bind(transcript_id)
        .bind(sequence)
        .execute(&mut **transaction)
        .await?;
    }
    Ok(())
}

fn require_one(rows_affected: u64, message: &'static str) -> Result<(), NlpStoreError> {
    if rows_affected == 1 {
        Ok(())
    } else {
        Err(NlpStoreError::AggregateInvariant(message))
    }
}

fn require_all(
    rows_affected: u64,
    expected: usize,
    message: &'static str,
) -> Result<(), NlpStoreError> {
    if usize::try_from(rows_affected).ok() == Some(expected) {
        Ok(())
    } else {
        Err(NlpStoreError::AggregateInvariant(message))
    }
}
