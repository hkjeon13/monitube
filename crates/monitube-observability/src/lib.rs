//! Structured logging initialization shared by Rust processes.

use thiserror::Error;
use tracing_subscriber::EnvFilter;
use tracing_subscriber::layer::SubscriberExt;
use tracing_subscriber::util::SubscriberInitExt;

/// Installs the process-wide JSON tracing subscriber.
///
/// # Errors
///
/// Returns an error when a global tracing subscriber has already been installed
/// or the subscriber cannot otherwise be initialized.
pub fn init(service_name: &'static str) -> Result<(), ObservabilityError> {
    let filter = EnvFilter::try_from_default_env()
        .unwrap_or_else(|_| EnvFilter::new("info,sqlx=warn,tower_http=info"));
    tracing_subscriber::registry()
        .with(filter)
        .with(
            tracing_subscriber::fmt::layer()
                .json()
                .with_current_span(true)
                .with_span_list(true)
                .with_target(true),
        )
        .try_init()
        .map_err(|error| ObservabilityError {
            service_name,
            detail: error.to_string(),
        })
}

#[derive(Debug, Error)]
#[error("could not initialize observability for {service_name}: {detail}")]
pub struct ObservabilityError {
    service_name: &'static str,
    detail: String,
}
