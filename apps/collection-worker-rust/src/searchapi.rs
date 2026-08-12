//! Bounded SearchAPI.io adapter for discovery and transcript retrieval.

use reqwest::{Client, StatusCode};
use serde_json::{Value, json};
use std::time::Duration;
use thiserror::Error;

const MAX_RESPONSE_BYTES: usize = 16 * 1024 * 1024;

pub struct SearchApiClient {
    client: Client,
    base_url: String,
    api_key: String,
    gl: String,
    hl: String,
    zero_retention: bool,
    channel_post_threshold: usize,
}

impl SearchApiClient {
    pub fn new(config: SearchApiConfig) -> Result<Self, SearchApiError> {
        if config.api_key.is_empty() || config.base_url.is_empty() {
            return Err(SearchApiError::InvalidConfig);
        }
        let client = Client::builder()
            .timeout(config.timeout)
            .connect_timeout(config.timeout.min(Duration::from_secs(10)))
            .pool_max_idle_per_host(2)
            .build()?;
        Ok(Self {
            client,
            base_url: config.base_url.trim_end_matches('?').to_owned(),
            api_key: config.api_key,
            gl: config.gl,
            hl: config.hl,
            zero_retention: config.zero_retention,
            channel_post_threshold: config.channel_post_threshold,
        })
    }

    pub async fn channel(&self, channel_id: &str) -> Result<Value, SearchApiError> {
        self.request(
            "youtube_channel",
            json!({"engine": "youtube_channel", "channel_id": channel_id,
                   "gl": self.gl, "hl": self.hl}),
            false,
        )
        .await
    }

    pub async fn channel_videos(
        &self,
        channel_id: &str,
        page_token: Option<&str>,
    ) -> Result<Value, SearchApiError> {
        let post = page_token.is_some_and(|token| token.len() >= self.channel_post_threshold);
        self.request(
            "youtube_channel_videos",
            json!({"engine": "youtube_channel_videos", "channel_id": channel_id,
                   "next_page_token": page_token, "gl": self.gl, "hl": self.hl}),
            post,
        )
        .await
    }

    pub async fn youtube(
        &self,
        query: &str,
        page_token: Option<&str>,
    ) -> Result<Value, SearchApiError> {
        self.request(
            "youtube",
            json!({"engine": "youtube", "q": query, "sp": page_token,
                   "gl": self.gl, "hl": self.hl}),
            false,
        )
        .await
    }

    pub async fn transcripts(
        &self,
        video_id: &str,
        language: &str,
        transcript_type: &str,
    ) -> Result<Value, SearchApiError> {
        match self
            .request(
                "youtube_transcripts",
                json!({"engine": "youtube_transcripts", "video_id": video_id,
                       "lang": language, "transcript_type": transcript_type}),
                false,
            )
            .await
        {
            Err(SearchApiError::Upstream {
                available_languages,
                ..
            }) if !available_languages.is_empty() => {
                Ok(json!({"available_languages": available_languages}))
            }
            result => result,
        }
    }

    async fn request(
        &self,
        operation: &'static str,
        mut parameters: Value,
        post: bool,
    ) -> Result<Value, SearchApiError> {
        let object = parameters
            .as_object_mut()
            .ok_or(SearchApiError::InvalidConfig)?;
        object.retain(|_, value| !value.is_null());
        if self.zero_retention {
            object.insert("zero_retention".to_owned(), Value::Bool(true));
        }
        let builder = self
            .client
            .request(
                if post {
                    reqwest::Method::POST
                } else {
                    reqwest::Method::GET
                },
                &self.base_url,
            )
            .bearer_auth(&self.api_key)
            .header(reqwest::header::ACCEPT, "application/json");
        let builder = if post {
            builder.json(&parameters)
        } else {
            builder.query(object)
        };
        let mut response = builder.send().await?;
        if response
            .content_length()
            .is_some_and(|length| length > MAX_RESPONSE_BYTES as u64)
        {
            return Err(SearchApiError::ResponseTooLarge);
        }
        let status = response.status();
        let mut bytes = Vec::new();
        while let Some(chunk) = response.chunk().await? {
            if bytes.len().saturating_add(chunk.len()) > MAX_RESPONSE_BYTES {
                return Err(SearchApiError::ResponseTooLarge);
            }
            bytes.extend_from_slice(&chunk);
        }
        let payload = serde_json::from_slice::<Value>(&bytes).unwrap_or(Value::Null);
        if status.is_success() {
            return Ok(payload);
        }
        Err(SearchApiError::Upstream {
            operation,
            status,
            error_code: safe_error_code(&payload),
            available_languages: language_options(&payload),
        })
    }
}

pub struct SearchApiConfig {
    pub api_key: String,
    pub base_url: String,
    pub timeout: Duration,
    pub gl: String,
    pub hl: String,
    pub zero_retention: bool,
    pub channel_post_threshold: usize,
}

fn safe_error_code(payload: &Value) -> String {
    payload
        .get("error")
        .and_then(|error| {
            error
                .get("code")
                .or_else(|| error.get("type"))
                .or_else(|| error.get("status"))
                .and_then(Value::as_str)
                .or_else(|| error.as_str())
        })
        .unwrap_or("upstream_error")
        .chars()
        .filter(|character| character.is_ascii_alphanumeric() || matches!(character, '_' | '-'))
        .take(80)
        .collect::<String>()
}

fn language_options(payload: &Value) -> Vec<Value> {
    payload
        .get("available_languages")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|item| {
            let language = item.get("lang").and_then(Value::as_str)?.trim();
            if language.is_empty() {
                return None;
            }
            Some(json!({
                "lang": language,
                "name": item.get("name").and_then(Value::as_str).unwrap_or("")
            }))
        })
        .collect()
}

#[derive(Debug, Error)]
pub enum SearchApiError {
    #[error("SearchAPI configuration is invalid")]
    InvalidConfig,
    #[error("SearchAPI transport failed")]
    Transport(#[from] reqwest::Error),
    #[error("SearchAPI response exceeded the configured bound")]
    ResponseTooLarge,
    #[error("SearchAPI {operation} failed with HTTP {status} ({error_code})")]
    Upstream {
        operation: &'static str,
        status: StatusCode,
        error_code: String,
        available_languages: Vec<Value>,
    },
}

impl SearchApiError {
    #[must_use]
    pub fn code(&self) -> &str {
        match self {
            Self::InvalidConfig => "invalid_config",
            Self::Transport(_) => "network_error",
            Self::ResponseTooLarge => "response_too_large",
            Self::Upstream { error_code, .. } => error_code,
        }
    }

    #[must_use]
    pub fn status_code(&self) -> u16 {
        match self {
            Self::Upstream { status, .. } => status.as_u16(),
            Self::InvalidConfig => 500,
            Self::Transport(_) => 503,
            Self::ResponseTooLarge => 502,
        }
    }

    #[must_use]
    pub fn is_retryable(&self) -> bool {
        let status = self.status_code();
        matches!(self, Self::Transport(_)) || status == 429 || status >= 500
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn provider_error_code_is_sanitized() {
        assert_eq!(
            safe_error_code(&json!({"error": {"code": "bad code: secret?"}})),
            "badcodesecret"
        );
    }

    #[test]
    fn language_options_drop_provider_metadata() {
        let options = language_options(&json!({"available_languages": [
            {"lang": "ko", "name": "Korean", "request_url": "secret"}
        ]}));
        assert_eq!(options, vec![json!({"lang": "ko", "name": "Korean"})]);
    }
}
