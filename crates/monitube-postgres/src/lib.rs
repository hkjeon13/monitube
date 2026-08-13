//! Shared `PostgreSQL` pool construction and bounded readiness checks.

use sqlx::postgres::{PgConnectOptions, PgPoolOptions};
use sqlx::{ConnectOptions, PgPool};
use std::str::FromStr;
use std::time::Duration;
use thiserror::Error;

#[derive(Debug, Clone, Copy)]
pub struct PoolConfig {
    pub min_connections: u32,
    pub max_connections: u32,
    pub acquire_timeout: Duration,
    pub connect_timeout: Duration,
}

impl PoolConfig {
    /// Validates connection-count and timeout bounds.
    ///
    /// # Errors
    ///
    /// Returns an error for an empty pool, inverted pool bounds, or zero timeout.
    pub fn validate(self) -> Result<Self, PostgresConfigError> {
        if self.max_connections == 0 {
            return Err(PostgresConfigError::ZeroMaximumConnections);
        }
        if self.min_connections > self.max_connections {
            return Err(PostgresConfigError::MinimumExceedsMaximum);
        }
        if self.acquire_timeout.is_zero() || self.connect_timeout.is_zero() {
            return Err(PostgresConfigError::ZeroTimeout);
        }
        Ok(self)
    }
}

/// Establishes a bounded `PostgreSQL` connection pool.
///
/// # Errors
///
/// Returns an error when the configuration or URL is invalid, the connection
/// deadline expires, or `PostgreSQL` rejects the connection.
pub async fn connect(
    database_url: &str,
    config: PoolConfig,
) -> Result<PgPool, PostgresConnectError> {
    let config = config.validate()?;
    let mut options =
        PgConnectOptions::from_str(database_url).map_err(PostgresConnectError::InvalidUrl)?;
    options = options
        .log_statements(tracing::log::LevelFilter::Debug)
        .log_slow_statements(tracing::log::LevelFilter::Warn, Duration::from_millis(500));

    let connect = PgPoolOptions::new()
        .min_connections(config.min_connections)
        .max_connections(config.max_connections)
        .acquire_timeout(config.acquire_timeout)
        .acquire_slow_threshold(Duration::from_millis(250))
        .connect_with(options);

    tokio::time::timeout(config.connect_timeout, connect)
        .await
        .map_err(|_| PostgresConnectError::ConnectTimeout(config.connect_timeout))?
        .map_err(PostgresConnectError::Connect)
}

/// Checks whether the last schema migration required by Rust services is present.
///
/// # Errors
///
/// Returns a database error if the migration table cannot be queried.
pub async fn check_required_schema(pool: &PgPool) -> Result<bool, sqlx::Error> {
    sqlx::query_scalar::<_, bool>(
        r"
        SELECT EXISTS (
          SELECT 1 FROM monitube_schema_migrations
          WHERE filename = '025_whitespace_token_metrics.sql'
        )
        ",
    )
    .fetch_one(pool)
    .await
}

#[derive(Debug, Error)]
pub enum PostgresConfigError {
    #[error("PostgreSQL pool maximum must be greater than zero")]
    ZeroMaximumConnections,
    #[error("PostgreSQL pool minimum cannot exceed maximum")]
    MinimumExceedsMaximum,
    #[error("PostgreSQL timeouts must be greater than zero")]
    ZeroTimeout,
}

#[derive(Debug, Error)]
pub enum PostgresConnectError {
    #[error(transparent)]
    Config(#[from] PostgresConfigError),
    #[error("DATABASE_URL is invalid")]
    InvalidUrl(#[source] sqlx::Error),
    #[error("could not connect to PostgreSQL")]
    Connect(#[source] sqlx::Error),
    #[error("could not connect to PostgreSQL within {0:?}")]
    ConnectTimeout(Duration),
}

#[cfg(test)]
mod tests {
    use super::{PoolConfig, PostgresConfigError};
    use std::time::Duration;

    #[test]
    fn pool_budget_rejects_invalid_bounds() {
        let result = PoolConfig {
            min_connections: 4,
            max_connections: 2,
            acquire_timeout: Duration::from_secs(1),
            connect_timeout: Duration::from_secs(1),
        }
        .validate();
        assert!(matches!(
            result,
            Err(PostgresConfigError::MinimumExceedsMaximum)
        ));
    }
}
