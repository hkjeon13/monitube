use axum::http::HeaderValue;
use monitube_postgres::PoolConfig;
use std::env;
use std::net::{IpAddr, SocketAddr};
use std::time::Duration;
use thiserror::Error;

#[derive(Debug, Clone)]
pub struct AppConfig {
    pub database_url: String,
    pub listen_address: SocketAddr,
    pub pool: PoolConfig,
    pub secure_cookies: bool,
    pub cors_origins: Vec<HeaderValue>,
    pub request_timeout: Duration,
    pub tokenizer_base_url: String,
    pub enable_transcript_search: bool,
    pub youtube_api_key_encryption_key: Option<String>,
    pub youtube_key_registration_token: Option<String>,
}

impl AppConfig {
    pub fn from_environment() -> Result<Self, ConfigError> {
        let database_url = required("DATABASE_URL")?;
        let host = optional("RUST_API_HOST").unwrap_or_else(|| "0.0.0.0".to_owned());
        let port = parse_u16("RUST_API_PORT", 8001)?;
        let ip = host
            .parse::<IpAddr>()
            .map_err(|_| ConfigError::InvalidIpAddress(host.clone()))?;
        let min_connections = parse_u32("DB_POOL_MIN_SIZE", 1)?;
        let max_connections = parse_u32("DB_POOL_MAX_SIZE", 8)?;
        let timeout_millis = parse_u64("DB_POOL_TIMEOUT_MILLIS", 5_000)?;
        let secure_cookies = optional("APP_ENV").is_none_or(|value| value != "development");
        let cors_origins = optional("CORS_ORIGINS")
            .unwrap_or_else(|| "http://localhost:3000".to_owned())
            .split(',')
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .map(|value| {
                value
                    .parse::<HeaderValue>()
                    .map_err(|_| ConfigError::InvalidCorsOrigin(value.to_owned()))
            })
            .collect::<Result<Vec<_>, _>>()?;
        if cors_origins.is_empty() {
            return Err(ConfigError::EmptyCorsOrigins);
        }
        let request_timeout =
            Duration::from_secs(parse_u64("RUST_API_REQUEST_TIMEOUT_SECONDS", 30)?);
        if request_timeout.is_zero() {
            return Err(ConfigError::ZeroRequestTimeout);
        }

        Ok(Self {
            database_url,
            listen_address: SocketAddr::new(ip, port),
            secure_cookies,
            cors_origins,
            request_timeout,
            tokenizer_base_url: optional("TOKENIZER_BASE_URL")
                .unwrap_or_else(|| "http://tokenizer:8010".to_owned())
                .trim_end_matches('/')
                .to_owned(),
            enable_transcript_search: parse_bool("ENABLE_TRANSCRIPT_SEARCH", true)?,
            youtube_api_key_encryption_key: optional("YOUTUBE_API_KEY_ENCRYPTION_KEY"),
            youtube_key_registration_token: optional("YOUTUBE_KEY_REGISTRATION_TOKEN"),
            pool: PoolConfig {
                min_connections,
                max_connections,
                acquire_timeout: Duration::from_millis(timeout_millis),
                connect_timeout: Duration::from_millis(timeout_millis),
            },
        })
    }
}

fn required(name: &'static str) -> Result<String, ConfigError> {
    optional(name).ok_or(ConfigError::Missing(name))
}

fn optional(name: &'static str) -> Option<String> {
    env::var(name)
        .ok()
        .map(|value| value.trim().to_owned())
        .filter(|value| !value.is_empty())
}

fn parse_u16(name: &'static str, default: u16) -> Result<u16, ConfigError> {
    optional(name).map_or(Ok(default), |value| {
        value
            .parse::<u16>()
            .map_err(|_| ConfigError::InvalidInteger(name))
    })
}

fn parse_u32(name: &'static str, default: u32) -> Result<u32, ConfigError> {
    optional(name).map_or(Ok(default), |value| {
        value
            .parse::<u32>()
            .map_err(|_| ConfigError::InvalidInteger(name))
    })
}

fn parse_u64(name: &'static str, default: u64) -> Result<u64, ConfigError> {
    optional(name).map_or(Ok(default), |value| {
        value
            .parse::<u64>()
            .map_err(|_| ConfigError::InvalidInteger(name))
    })
}

fn parse_bool(name: &'static str, default: bool) -> Result<bool, ConfigError> {
    optional(name).map_or(Ok(default), |value| match value.as_str() {
        "true" => Ok(true),
        "false" => Ok(false),
        _ => Err(ConfigError::InvalidBoolean(name)),
    })
}

#[derive(Debug, Error)]
pub enum ConfigError {
    #[error("missing required environment variable {0}")]
    Missing(&'static str),
    #[error("environment variable {0} must be an unsigned integer")]
    InvalidInteger(&'static str),
    #[error("environment variable {0} must be true or false")]
    InvalidBoolean(&'static str),
    #[error("RUST_API_HOST is not a valid IP address: {0}")]
    InvalidIpAddress(String),
    #[error("CORS origin is not a valid header value: {0}")]
    InvalidCorsOrigin(String),
    #[error("CORS_ORIGINS must contain at least one origin")]
    EmptyCorsOrigins,
    #[error("RUST_API_REQUEST_TIMEOUT_SECONDS must be greater than zero")]
    ZeroRequestTimeout,
}
