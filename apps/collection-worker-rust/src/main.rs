mod collector;
mod searchapi;
mod youtube;

use collector::{Collector, CollectorConfig, CollectorError};
use monitube_collection_store::{CollectionStore, StoreError};
use monitube_postgres::PoolConfig;
use searchapi::{SearchApiClient, SearchApiConfig};
use std::env;
use std::error::Error;
use std::time::Duration;
use youtube::YouTubeClient;

#[tokio::main]
#[allow(clippy::too_many_lines)]
async fn main() -> Result<(), Box<dyn Error>> {
    monitube_observability::init("monitube-collection-worker-rust")?;
    let database_url = required("DATABASE_URL")?;
    let worker_id = optional("WORKER_ID").unwrap_or_else(|| {
        format!(
            "rust-worker-{}-{}",
            optional("HOSTNAME").unwrap_or_else(|| "local".to_owned()),
            std::process::id()
        )
    });
    let lease_seconds = parse_i64("WORKER_LEASE_SECONDS", 180, 30, 3_600)?;
    let poll_millis = parse_u64("WORKER_POLL_MILLIS", 1_000, 100, 60_000)?;
    let timeout_seconds = parse_u64("YOUTUBE_API_TIMEOUT_SECONDS", 20, 1, 120)?;
    let keys = youtube_keys();
    let pool = monitube_postgres::connect(
        &database_url,
        PoolConfig {
            min_connections: 1,
            max_connections: u32::try_from(parse_u64("DB_POOL_MAX_SIZE", 2, 1, 16)?)?,
            acquire_timeout: Duration::from_millis(parse_u64(
                "DB_POOL_TIMEOUT_MILLIS",
                10_000,
                100,
                60_000,
            )?),
            connect_timeout: Duration::from_secs(10),
        },
    )
    .await?;
    let store = CollectionStore::new(pool.clone());
    let youtube_base_url = optional("YOUTUBE_API_BASE_URL")
        .unwrap_or_else(|| "https://www.googleapis.com/youtube/v3".to_owned());
    let youtube = YouTubeClient::new(
        keys,
        &youtube_base_url,
        Duration::from_secs(timeout_seconds),
        &worker_id,
    )?;
    let discovery_provider = optional("DISCOVERY_PROVIDER")
        .unwrap_or_else(|| "searchapi".to_owned())
        .to_ascii_lowercase();
    if !matches!(discovery_provider.as_str(), "searchapi" | "youtube") {
        return Err(WorkerConfigError::Invalid("DISCOVERY_PROVIDER").into());
    }
    let transcript_enabled = enabled("TRANSCRIPT_COLLECTION_ENABLED", true);
    let searchapi = optional("SEARCH_API_KEY")
        .or_else(|| optional("SEARCHAPI_API_KEY"))
        .map(|api_key| {
            SearchApiClient::new(SearchApiConfig {
                api_key,
                base_url: optional("SEARCHAPI_BASE_URL")
                    .unwrap_or_else(|| "https://www.searchapi.io/api/v1/search".to_owned()),
                timeout: Duration::from_secs(parse_u64("SEARCHAPI_TIMEOUT_SECONDS", 20, 1, 120)?),
                gl: optional("SEARCHAPI_GL").unwrap_or_else(|| "kr".to_owned()),
                hl: optional("SEARCHAPI_HL").unwrap_or_else(|| "ko".to_owned()),
                zero_retention: enabled("SEARCHAPI_ZERO_RETENTION", false),
                channel_post_threshold: usize::try_from(parse_u64(
                    "SEARCHAPI_CHANNEL_TOKEN_POST_THRESHOLD_BYTES",
                    1_800,
                    256,
                    65_536,
                )?)
                .map_err(|_| {
                    WorkerConfigError::Invalid("SEARCHAPI_CHANNEL_TOKEN_POST_THRESHOLD_BYTES")
                })?,
            })
            .map_err(WorkerConfigError::SearchApi)
        })
        .transpose()?;
    if (discovery_provider == "searchapi" || transcript_enabled) && searchapi.is_none() {
        return Err(WorkerConfigError::Missing("SEARCH_API_KEY").into());
    }
    let mut collector = Collector::new(
        pool,
        youtube,
        CollectorConfig {
            discovery_provider,
            searchapi,
            transcript_enabled,
            transcript_primary_language: optional("TRANSCRIPT_PRIMARY_LANGUAGE")
                .unwrap_or_else(|| "ko".to_owned()),
            transcript_fallback_language: optional("TRANSCRIPT_FALLBACK_LANGUAGE")
                .unwrap_or_else(|| "en".to_owned()),
            transcript_type: optional("TRANSCRIPT_TYPE_PREFERENCE")
                .unwrap_or_else(|| "manual".to_owned()),
            transcript_max_segments: usize::try_from(parse_u64(
                "TRANSCRIPT_MAX_SEGMENTS",
                100_000,
                1,
                100_000,
            )?)?,
            runtime_key_encryption_key: optional("YOUTUBE_API_KEY_ENCRYPTION_KEY"),
        },
    );
    let mut shutdown = Box::pin(tokio::signal::ctrl_c());
    tracing::info!(%worker_id, "Rust collection worker started");
    loop {
        tokio::select! {
            signal = &mut shutdown => {
                if let Err(error) = signal {
                    tracing::warn!(%error, "shutdown signal failed");
                }
                break;
            }
            result = run_once(&store, &mut collector, &worker_id, lease_seconds) => {
                match result {
                    Ok(true) => {}
                    Ok(false) => tokio::time::sleep(Duration::from_millis(poll_millis)).await,
                    Err(error) => {
                        tracing::error!(%error, "collection worker loop failed");
                        tokio::time::sleep(Duration::from_millis(poll_millis)).await;
                    }
                }
            }
        }
    }
    tracing::info!(%worker_id, "Rust collection worker stopped");
    Ok(())
}

async fn run_once(
    store: &CollectionStore,
    collector: &mut Collector,
    worker_id: &str,
    lease_seconds: i64,
) -> Result<bool, StoreError> {
    let dispatched = store.dispatch_due_pins(10).await?;
    if dispatched > 0 {
        tracing::info!(dispatched, "dispatched due collection pins");
    }
    let Some(job) = store.claim_next(worker_id, lease_seconds).await? else {
        return Ok(dispatched > 0);
    };
    tracing::info!(job_id = %job.id, source_id = %job.source_id, "claimed collection job");
    match collector.collect(&job).await {
        Ok(()) => store.complete(&job).await?,
        Err(CollectorError::YouTube(error)) if error.is_quota() => {
            let reason = error.to_string();
            store
                .wait_quota(&job, &reason, error.bucket(), 3_600)
                .await?;
        }
        Err(CollectorError::YouTube(error)) if error.resource_reason().is_some() => {
            let reason = error.resource_reason().unwrap_or("resourceUnavailable");
            let scope = job
                .checkpoint
                .get("youtubeVideoId")
                .and_then(serde_json::Value::as_str);
            store
                .add_partial_error(&job, reason, &error.to_string(), scope)
                .await?;
            store.complete(&job).await?;
        }
        Err(CollectorError::YouTube(error)) => {
            store.wait_retry(&job, &error.to_string(), 60).await?;
        }
        Err(CollectorError::SearchApi(error)) => {
            store.wait_retry(&job, &error.to_string(), 60).await?;
        }
        Err(CollectorError::WaitingForChildren { waiting_quota }) => {
            store.wait_children(&job, waiting_quota > 0).await?;
        }
        Err(CollectorError::Store(StoreError::LeaseLost)) => {
            tracing::warn!(job_id = %job.id, "collection lease was lost");
        }
        Err(error) => store.fail(&job, &error.to_string()).await?,
    }
    Ok(true)
}

fn youtube_keys() -> Vec<String> {
    let mut keys = optional("YOUTUBE_API_KEYS")
        .unwrap_or_default()
        .replace('\n', ",")
        .split(',')
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_owned)
        .collect::<Vec<_>>();
    if let Some(key) = optional("YOUTUBE_API_KEY") {
        if !keys.contains(&key) {
            keys.push(key);
        }
    }
    keys
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

fn enabled(name: &'static str, default: bool) -> bool {
    optional(name).map_or(default, |value| {
        matches!(
            value.to_ascii_lowercase().as_str(),
            "1" | "true" | "yes" | "on"
        )
    })
}

fn parse_u64(
    name: &'static str,
    default: u64,
    minimum: u64,
    maximum: u64,
) -> Result<u64, WorkerConfigError> {
    let value = optional(name).map_or(Ok(default), |value| {
        value
            .parse::<u64>()
            .map_err(|_| WorkerConfigError::Invalid(name))
    })?;
    if (minimum..=maximum).contains(&value) {
        Ok(value)
    } else {
        Err(WorkerConfigError::Invalid(name))
    }
}

fn parse_i64(
    name: &'static str,
    default: i64,
    minimum: i64,
    maximum: i64,
) -> Result<i64, WorkerConfigError> {
    let value = optional(name).map_or(Ok(default), |value| {
        value
            .parse::<i64>()
            .map_err(|_| WorkerConfigError::Invalid(name))
    })?;
    if (minimum..=maximum).contains(&value) {
        Ok(value)
    } else {
        Err(WorkerConfigError::Invalid(name))
    }
}

#[derive(Debug, thiserror::Error)]
enum WorkerConfigError {
    #[error("missing required environment variable {0}")]
    Missing(&'static str),
    #[error("environment variable {0} is invalid")]
    Invalid(&'static str),
    #[error("SearchAPI client configuration is invalid")]
    SearchApi(#[source] searchapi::SearchApiError),
}
