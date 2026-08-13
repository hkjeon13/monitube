//! Bounded rotating client for the `YouTube Data API v3`.

use reqwest::{Client, StatusCode};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::time::Duration;
use thiserror::Error;

pub struct YouTubeClient {
    client: Client,
    base_url: String,
    keys: Vec<String>,
    index: usize,
}

impl YouTubeClient {
    pub fn new(
        keys: Vec<String>,
        base_url: &str,
        timeout: Duration,
        worker_id: &str,
    ) -> Result<Self, YouTubeError> {
        if keys.iter().any(String::is_empty) {
            return Err(YouTubeError::MissingKeys);
        }
        let client = Client::builder()
            .timeout(timeout)
            .connect_timeout(timeout.min(Duration::from_secs(10)))
            .pool_max_idle_per_host(2)
            .build()?;
        let digest = Sha256::digest(worker_id.as_bytes());
        let index = if keys.is_empty() {
            0
        } else {
            usize::from(digest[0]) % keys.len()
        };
        Ok(Self {
            client,
            base_url: base_url.trim_end_matches('/').to_owned(),
            keys,
            index,
        })
    }

    pub async fn request(
        &mut self,
        endpoint: &'static str,
        params: &[(&str, String)],
    ) -> Result<Value, YouTubeError> {
        if self.keys.is_empty() {
            return Err(YouTubeError::MissingKeys);
        }
        let attempts = self.keys.len();
        for _ in 0..attempts {
            let response = self
                .client
                .get(format!("{}/{endpoint}", self.base_url))
                .query(params)
                .query(&[("key", &self.keys[self.index])])
                .send()
                .await?;
            let status = response.status();
            let payload = response.json::<Value>().await.unwrap_or(Value::Null);
            if status.is_success() {
                return Ok(payload);
            }
            let reasons = error_reasons(&payload);
            if resource_error(&reasons) {
                return Err(YouTubeError::Upstream {
                    endpoint,
                    status,
                    reason: reasons
                        .first()
                        .cloned()
                        .unwrap_or_else(|| "upstream_error".to_owned()),
                    quota: false,
                });
            }
            let quota = quota_error(status, &reasons);
            if self.keys.len() > 1 {
                self.index = (self.index + 1) % self.keys.len();
                continue;
            }
            return Err(YouTubeError::Upstream {
                endpoint,
                status,
                reason: reasons
                    .first()
                    .cloned()
                    .unwrap_or_else(|| "upstream_error".to_owned()),
                quota,
            });
        }
        Err(YouTubeError::AllKeysUnavailable { endpoint })
    }

    pub fn replace_keys(&mut self, keys: Vec<String>) -> Result<(), YouTubeError> {
        if keys.is_empty() || keys.iter().any(String::is_empty) {
            return Err(YouTubeError::MissingKeys);
        }
        let current_fingerprint = self.key_fingerprint();
        self.keys = keys;
        self.index = current_fingerprint
            .and_then(|fingerprint| {
                self.keys
                    .iter()
                    .position(|key| fingerprint_for(key) == fingerprint)
            })
            .unwrap_or(0);
        Ok(())
    }

    #[must_use]
    pub fn key_fingerprint(&self) -> Option<String> {
        self.keys.get(self.index).map(|key| fingerprint_for(key))
    }
}

fn fingerprint_for(key: &str) -> String {
    hex::encode(Sha256::digest(key.as_bytes()))[..24].to_owned()
}

fn error_reasons(payload: &Value) -> Vec<String> {
    payload
        .get("error")
        .and_then(|error| error.get("errors"))
        .and_then(Value::as_array)
        .map(|errors| {
            errors
                .iter()
                .filter_map(|error| error.get("reason").and_then(Value::as_str))
                .map(str::to_owned)
                .collect()
        })
        .unwrap_or_default()
}

fn resource_error(reasons: &[String]) -> bool {
    reasons
        .iter()
        .any(|reason| matches!(reason.as_str(), "commentsDisabled" | "videoNotFound"))
}

fn quota_error(status: StatusCode, reasons: &[String]) -> bool {
    status == StatusCode::TOO_MANY_REQUESTS
        || reasons.iter().any(|reason| {
            matches!(
                reason.as_str(),
                "quotaExceeded"
                    | "dailyLimitExceeded"
                    | "rateLimitExceeded"
                    | "userRateLimitExceeded"
            )
        })
}

#[derive(Debug, Error)]
pub enum YouTubeError {
    #[error("at least one YouTube API key is required")]
    MissingKeys,
    #[error("YouTube transport failed")]
    Transport(#[from] reqwest::Error),
    #[error("YouTube {endpoint} failed with {status} ({reason})")]
    Upstream {
        endpoint: &'static str,
        status: StatusCode,
        reason: String,
        quota: bool,
    },
    #[error("all YouTube keys are unavailable for {endpoint}")]
    AllKeysUnavailable { endpoint: &'static str },
}

impl YouTubeError {
    #[must_use]
    pub const fn is_quota(&self) -> bool {
        matches!(
            self,
            Self::Upstream { quota: true, .. } | Self::AllKeysUnavailable { .. }
        )
    }

    #[must_use]
    pub fn bucket(&self) -> &'static str {
        match self {
            Self::Upstream {
                endpoint: "search", ..
            }
            | Self::AllKeysUnavailable { endpoint: "search" } => "search_queries",
            _ => "core",
        }
    }

    #[must_use]
    pub fn resource_reason(&self) -> Option<&str> {
        match self {
            Self::Upstream {
                reason,
                quota: false,
                ..
            } if matches!(reason.as_str(), "commentsDisabled" | "videoNotFound") => Some(reason),
            _ => None,
        }
    }

    #[must_use]
    pub fn status_code(&self) -> u16 {
        match self {
            Self::Upstream { status, .. } => status.as_u16(),
            Self::Transport(_) | Self::AllKeysUnavailable { .. } => 503,
            Self::MissingKeys => 500,
        }
    }

    #[must_use]
    pub fn reason(&self) -> Option<&str> {
        match self {
            Self::Upstream { reason, .. } => Some(reason),
            _ => None,
        }
    }

    #[must_use]
    pub fn endpoint(&self) -> Option<&'static str> {
        match self {
            Self::Upstream { endpoint, .. } | Self::AllKeysUnavailable { endpoint } => {
                Some(*endpoint)
            }
            _ => None,
        }
    }

    #[must_use]
    pub fn is_retryable(&self) -> bool {
        match self {
            Self::Transport(_) => true,
            Self::Upstream { status, .. } => status.as_u16() == 429 || status.is_server_error(),
            Self::MissingKeys | Self::AllKeysUnavailable { .. } => false,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn parses_quota_reason_without_exposing_key() {
        let reasons = error_reasons(&json!({"error": {"errors": [{"reason": "quotaExceeded"}]}}));
        assert!(quota_error(StatusCode::FORBIDDEN, &reasons));
    }
}
