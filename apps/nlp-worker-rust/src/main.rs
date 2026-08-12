//! Dedicated Rust worker for leased tokenizer and bag-of-words indexing.

use monitube_analysis::{BagOfWords, BagOfWordsError, TokenizerClient, TokenizerClientError};
use monitube_contracts::{
    TOKENIZER_ANALYZER_VERSION, TOKENIZER_MAX_DOCUMENTS, TOKENIZER_MAX_TOTAL_TEXT_BYTES,
    TokenizeDocument, TokenizeRequest,
};
use monitube_nlp_store::{
    ClaimedDocument, CompleteDocument, DocumentAction, NlpStoreError, claim_next_document,
    complete_document, enqueue_stale_documents, fail_document,
};
use monitube_postgres::PoolConfig;
use sqlx::PgPool;
use std::collections::{BTreeMap, HashSet};
use std::env;
use std::error::Error;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;
use thiserror::Error;

#[derive(Debug)]
struct WorkerConfig {
    database_url: String,
    tokenizer_base_url: String,
    tokenizer_timeout: Duration,
    poll_interval: Duration,
    lease_seconds: i64,
    batch_size: usize,
    worker_id: String,
    enabled: bool,
    pool: PoolConfig,
}

impl WorkerConfig {
    fn from_environment() -> Result<Self, WorkerConfigError> {
        let database_url = required("DATABASE_URL")?;
        let tokenizer_base_url =
            optional("TOKENIZER_BASE_URL").unwrap_or_else(|| "http://127.0.0.1:8010".to_owned());
        let tokenizer_timeout = Duration::from_secs(parse_u64("TOKENIZER_TIMEOUT_SECONDS", 30)?);
        let poll_interval = Duration::from_millis(parse_u64("NLP_WORKER_POLL_MILLIS", 1_000)?);
        let lease_seconds = parse_i64("NLP_INDEX_LEASE_SECONDS", 300)?;
        let batch_size = parse_usize("NLP_INDEX_BATCH_SIZE", 10)?;
        let timeout_millis = parse_u64("DB_POOL_TIMEOUT_MILLIS", 10_000)?;
        if tokenizer_timeout.is_zero()
            || poll_interval.is_zero()
            || !(30..=3_600).contains(&lease_seconds)
            || !(1..=100).contains(&batch_size)
        {
            return Err(WorkerConfigError::InvalidBounds);
        }
        let default_worker_id = format!(
            "nlp-{}-{}",
            optional("HOSTNAME").unwrap_or_else(|| "local".to_owned()),
            std::process::id()
        );
        Ok(Self {
            database_url,
            tokenizer_base_url,
            tokenizer_timeout,
            poll_interval,
            lease_seconds,
            batch_size,
            worker_id: optional("NLP_WORKER_ID").unwrap_or(default_worker_id),
            enabled: parse_bool("ENABLE_NLP_INDEXING", true)?,
            pool: PoolConfig {
                min_connections: parse_u32("DB_POOL_MIN_SIZE", 1)?,
                max_connections: parse_u32("DB_POOL_MAX_SIZE", 2)?,
                acquire_timeout: Duration::from_millis(timeout_millis),
                connect_timeout: Duration::from_millis(timeout_millis),
            },
        })
    }
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn Error>> {
    monitube_observability::init("monitube-nlp-worker-rust")?;
    let config = WorkerConfig::from_environment()?;
    let pool = monitube_postgres::connect(&config.database_url, config.pool).await?;
    let tokenizer = TokenizerClient::new(&config.tokenizer_base_url, config.tokenizer_timeout)?;

    let shutdown = Arc::new(AtomicBool::new(false));
    let signal_flag = Arc::clone(&shutdown);
    let signal_task = tokio::spawn(async move {
        shutdown_signal().await;
        signal_flag.store(true, Ordering::Release);
    });

    if !config.enabled {
        tracing::info!("NLP indexing is disabled; worker will remain idle");
        let _ = signal_task.await;
        return Ok(());
    }

    let stale = enqueue_stale_documents(&pool, TOKENIZER_ANALYZER_VERSION).await?;
    if stale > 0 {
        tracing::info!(stale, "queued documents produced by an older analyzer");
    }
    tracing::info!(worker_id = %config.worker_id, "Rust NLP worker started");
    run_loop(&pool, &tokenizer, &config, &shutdown).await;
    signal_task.abort();
    tracing::info!("Rust NLP worker stopped");
    Ok(())
}

async fn run_loop(
    pool: &PgPool,
    tokenizer: &TokenizerClient,
    config: &WorkerConfig,
    shutdown: &AtomicBool,
) {
    while !shutdown.load(Ordering::Acquire) {
        let mut processed = 0_usize;
        for _ in 0..config.batch_size {
            if shutdown.load(Ordering::Acquire) {
                break;
            }
            let claim =
                match claim_next_document(pool, &config.worker_id, config.lease_seconds).await {
                    Ok(Some(claim)) => claim,
                    Ok(None) => break,
                    Err(error) => {
                        tracing::warn!(%error, "could not claim NLP document");
                        break;
                    }
                };
            processed += 1;
            if let Err(error) = process_document(pool, tokenizer, config, &claim).await {
                tracing::warn!(
                    %error,
                    source_kind = %claim.source_kind,
                    source_id = %claim.source_id,
                    "NLP document processing failed"
                );
                if let Err(failure_error) =
                    fail_document(pool, &claim, &config.worker_id, &error.to_string()).await
                {
                    tracing::warn!(%failure_error, "could not persist NLP failure state");
                }
            }
        }

        if processed > 0 {
            tracing::info!(processed, "processed NLP document batch");
            continue;
        }
        tokio::time::sleep(config.poll_interval).await;
    }
}

async fn process_document(
    pool: &PgPool,
    tokenizer: &TokenizerClient,
    config: &WorkerConfig,
    claim: &ClaimedDocument,
) -> Result<(), WorkerError> {
    if claim.action == DocumentAction::Delete {
        let outcome = complete_document(
            pool,
            CompleteDocument {
                claim,
                worker_id: &config.worker_id,
                analyzer_version: TOKENIZER_ANALYZER_VERSION,
                bag: None,
                segment_terms: &BTreeMap::new(),
            },
        )
        .await?;
        tracing::debug!(?outcome, source_id = %claim.source_id, "completed NLP deletion");
        return Ok(());
    }

    let response = tokenizer
        .tokenize(&TokenizeRequest {
            analyzer_version: TOKENIZER_ANALYZER_VERSION.to_owned(),
            documents: vec![TokenizeDocument {
                id: claim.source_id.to_string(),
                text: claim.text.clone(),
                segments: Vec::new(),
            }],
        })
        .await?;
    let mut documents = response.documents.into_iter();
    let document = documents.next().ok_or(WorkerError::TokenizerShape)?;
    if documents.next().is_some() || document.id != claim.source_id.to_string() {
        return Err(WorkerError::TokenizerShape);
    }
    let bag = BagOfWords::from_tokens(document.tokens)?;
    let segment_terms = tokenize_segments(tokenizer, claim).await?;
    let outcome = complete_document(
        pool,
        CompleteDocument {
            claim,
            worker_id: &config.worker_id,
            analyzer_version: TOKENIZER_ANALYZER_VERSION,
            bag: Some(&bag),
            segment_terms: &segment_terms,
        },
    )
    .await?;
    tracing::debug!(?outcome, source_id = %claim.source_id, "completed NLP indexing");
    Ok(())
}

async fn tokenize_segments(
    tokenizer: &TokenizerClient,
    claim: &ClaimedDocument,
) -> Result<BTreeMap<i32, Vec<String>>, WorkerError> {
    let mut output = BTreeMap::new();
    let mut offset = 0_usize;
    while offset < claim.segments.len() {
        let mut documents = Vec::new();
        let mut total_bytes = 0_usize;
        while offset < claim.segments.len() && documents.len() < TOKENIZER_MAX_DOCUMENTS {
            let segment = &claim.segments[offset];
            let segment_bytes = segment.text.len();
            if segment_bytes > TOKENIZER_MAX_TOTAL_TEXT_BYTES {
                return Err(WorkerError::SegmentTooLarge(segment.sequence));
            }
            if !documents.is_empty()
                && total_bytes.saturating_add(segment_bytes) > TOKENIZER_MAX_TOTAL_TEXT_BYTES
            {
                break;
            }
            total_bytes = total_bytes.saturating_add(segment_bytes);
            documents.push(TokenizeDocument {
                id: segment.sequence.to_string(),
                text: segment.text.clone(),
                segments: Vec::new(),
            });
            offset += 1;
        }
        let expected_ids = documents
            .iter()
            .map(|document| document.id.clone())
            .collect::<HashSet<_>>();
        let response = tokenizer
            .tokenize(&TokenizeRequest {
                analyzer_version: TOKENIZER_ANALYZER_VERSION.to_owned(),
                documents,
            })
            .await?;
        if response.documents.len() != expected_ids.len() {
            return Err(WorkerError::TokenizerShape);
        }
        let mut returned_ids = HashSet::new();
        for document in response.documents {
            if !expected_ids.contains(&document.id) || !returned_ids.insert(document.id.clone()) {
                return Err(WorkerError::TokenizerShape);
            }
            let sequence = document
                .id
                .parse::<i32>()
                .map_err(|_| WorkerError::TokenizerShape)?;
            output.insert(sequence, document.tokens);
        }
    }
    Ok(output)
}

async fn shutdown_signal() {
    let ctrl_c = async {
        if tokio::signal::ctrl_c().await.is_err() {
            std::future::pending::<()>().await;
        }
    };

    #[cfg(unix)]
    let terminate = async {
        match tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()) {
            Ok(mut signal) => {
                signal.recv().await;
            }
            Err(_) => std::future::pending::<()>().await,
        }
    };

    #[cfg(unix)]
    tokio::select! {
        () = ctrl_c => {},
        () = terminate => {},
    }

    #[cfg(not(unix))]
    ctrl_c.await;
}

fn required(name: &'static str) -> Result<String, WorkerConfigError> {
    optional(name).ok_or(WorkerConfigError::Missing(name))
}

fn optional(name: &'static str) -> Option<String> {
    env::var(name)
        .ok()
        .map(|value| value.trim().to_owned())
        .filter(|value| !value.is_empty())
}

fn parse_u64(name: &'static str, default: u64) -> Result<u64, WorkerConfigError> {
    optional(name).map_or(Ok(default), |value| {
        value.parse().map_err(|_| WorkerConfigError::Invalid(name))
    })
}

fn parse_i64(name: &'static str, default: i64) -> Result<i64, WorkerConfigError> {
    optional(name).map_or(Ok(default), |value| {
        value.parse().map_err(|_| WorkerConfigError::Invalid(name))
    })
}

fn parse_u32(name: &'static str, default: u32) -> Result<u32, WorkerConfigError> {
    optional(name).map_or(Ok(default), |value| {
        value.parse().map_err(|_| WorkerConfigError::Invalid(name))
    })
}

fn parse_usize(name: &'static str, default: usize) -> Result<usize, WorkerConfigError> {
    optional(name).map_or(Ok(default), |value| {
        value.parse().map_err(|_| WorkerConfigError::Invalid(name))
    })
}

fn parse_bool(name: &'static str, default: bool) -> Result<bool, WorkerConfigError> {
    optional(name).map_or(Ok(default), |value| match value.as_str() {
        "true" => Ok(true),
        "false" => Ok(false),
        _ => Err(WorkerConfigError::Invalid(name)),
    })
}

#[derive(Debug, Error)]
enum WorkerConfigError {
    #[error("missing required environment variable {0}")]
    Missing(&'static str),
    #[error("environment variable {0} is invalid")]
    Invalid(&'static str),
    #[error("NLP worker configuration bounds are invalid")]
    InvalidBounds,
}

#[derive(Debug, Error)]
enum WorkerError {
    #[error(transparent)]
    Store(#[from] NlpStoreError),
    #[error(transparent)]
    Tokenizer(#[from] TokenizerClientError),
    #[error(transparent)]
    BagOfWords(#[from] BagOfWordsError),
    #[error("tokenizer response does not match the requested document batch")]
    TokenizerShape,
    #[error("transcript segment {0} exceeds the tokenizer request bound")]
    SegmentTooLarge(i32),
}
