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
            .build()
            .map_err(SearchApiError::ClientBuild)?;
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
        let payload = self
            .request(
                "youtube_channel",
                json!({"engine": "youtube_channel", "channel_id": channel_id,
                       "gl": self.gl, "hl": self.hl}),
                false,
            )
            .await?;
        require_payload("youtube_channel", payload, |value| {
            value
                .pointer("/channel/id")
                .and_then(Value::as_str)
                .is_some_and(|value| !value.trim().is_empty())
        })
    }

    pub async fn channel_videos(
        &self,
        channel_id: &str,
        page_token: Option<&str>,
    ) -> Result<Value, SearchApiError> {
        let post = page_token.is_some_and(|token| token.len() >= self.channel_post_threshold);
        let payload = self
            .request(
                "youtube_channel_videos",
                json!({"engine": "youtube_channel_videos", "channel_id": channel_id,
                       "next_page_token": page_token, "gl": self.gl, "hl": self.hl}),
                post,
            )
            .await?;
        validate_channel_videos_payload(payload, page_token.is_some())
    }

    pub async fn youtube(
        &self,
        query: &str,
        page_token: Option<&str>,
    ) -> Result<Value, SearchApiError> {
        let payload = self
            .request(
                "youtube",
                json!({"engine": "youtube", "q": query, "sp": page_token,
                       "gl": self.gl, "hl": self.hl}),
                false,
            )
            .await?;
        require_payload("youtube", payload, |value| {
            ["videos", "channels", "sections"]
                .iter()
                .any(|key| value.get(key).and_then(Value::as_array).is_some())
        })
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
            Ok(payload) => require_payload("youtube_transcripts", payload, |value| {
                value.get("transcripts").and_then(Value::as_array).is_some()
                    || value
                        .get("available_languages")
                        .and_then(Value::as_array)
                        .is_some()
            }),
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
        let mut response = builder
            .send()
            .await
            .map_err(|source| SearchApiError::Transport { operation, source })?;
        if response
            .content_length()
            .is_some_and(|length| length > MAX_RESPONSE_BYTES as u64)
        {
            return Err(SearchApiError::ResponseTooLarge);
        }
        let status = response.status();
        let mut bytes = Vec::new();
        while let Some(chunk) = response
            .chunk()
            .await
            .map_err(|source| SearchApiError::Transport { operation, source })?
        {
            if bytes.len().saturating_add(chunk.len()) > MAX_RESPONSE_BYTES {
                return Err(SearchApiError::ResponseTooLarge);
            }
            bytes.extend_from_slice(&chunk);
        }
        if status.is_success() {
            let payload = serde_json::from_slice::<Value>(&bytes)
                .map_err(|_| SearchApiError::InvalidJson { operation })?;
            if payload.get("error").is_some() {
                return Err(SearchApiError::ErrorPayload {
                    operation,
                    error_code: safe_error_code(&payload),
                });
            }
            return Ok(payload);
        }
        let payload = serde_json::from_slice::<Value>(&bytes).unwrap_or(Value::Null);
        Err(SearchApiError::Upstream {
            operation,
            status,
            error_code: safe_error_code(&payload),
            available_languages: language_options(&payload),
        })
    }
}

fn validate_channel_videos_payload(
    payload: Value,
    is_follow_up_page: bool,
) -> Result<Value, SearchApiError> {
    let item_arrays = ["videos", "sections"]
        .iter()
        .filter_map(|key| payload.get(key).and_then(Value::as_array))
        .collect::<Vec<_>>();
    let has_item_array = !item_arrays.is_empty();
    let has_items = item_arrays.iter().any(|items| !items.is_empty());
    let channel_id = payload
        .pointer("/channel/id")
        .and_then(Value::as_str)
        .filter(|value| !value.trim().is_empty());
    let has_next_page = payload
        .pointer("/pagination/next_page_token")
        .and_then(Value::as_str)
        .is_some_and(|value| !value.trim().is_empty());
    let reported_empty = payload.pointer("/channel/videos").and_then(numeric_count) == Some(0);
    if has_items
        || (has_next_page && (is_follow_up_page || channel_id.is_some()))
        || reported_empty
        || (is_follow_up_page && has_item_array)
    {
        Ok(payload)
    } else {
        Err(SearchApiError::IncompletePayload {
            operation: "youtube_channel_videos",
        })
    }
}

fn numeric_count(value: &Value) -> Option<i64> {
    value
        .as_i64()
        .or_else(|| value.as_u64().and_then(|number| i64::try_from(number).ok()))
        .or_else(|| {
            value.as_str().and_then(|text| {
                let digits = text
                    .chars()
                    .filter(char::is_ascii_digit)
                    .collect::<String>();
                (!digits.is_empty())
                    .then(|| digits.parse::<i64>().ok())
                    .flatten()
            })
        })
}

fn require_payload(
    operation: &'static str,
    payload: Value,
    validator: impl FnOnce(&Value) -> bool,
) -> Result<Value, SearchApiError> {
    if validator(&payload) {
        Ok(payload)
    } else {
        Err(SearchApiError::InvalidPayload { operation })
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
    #[error("SearchAPI HTTP client configuration failed")]
    ClientBuild(#[source] reqwest::Error),
    #[error("SearchAPI transport failed")]
    Transport {
        operation: &'static str,
        #[source]
        source: reqwest::Error,
    },
    #[error("SearchAPI response exceeded the configured bound")]
    ResponseTooLarge,
    #[error("SearchAPI returned invalid JSON for {operation}")]
    InvalidJson { operation: &'static str },
    #[error("SearchAPI returned an error payload for {operation} ({error_code})")]
    ErrorPayload {
        operation: &'static str,
        error_code: String,
    },
    #[error("SearchAPI returned an invalid payload for {operation}")]
    InvalidPayload { operation: &'static str },
    #[error("SearchAPI returned an incomplete payload for {operation}")]
    IncompletePayload { operation: &'static str },
    #[error("SearchAPI pagination repeated a token for {operation}")]
    PaginationCycle { operation: &'static str },
    #[error("SearchAPI returned too many empty pages for {operation}")]
    EmptyPagination { operation: &'static str },
    #[error("SearchAPI could not resolve one exact channel candidate")]
    ChannelResolutionAmbiguous,
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
            Self::ClientBuild(_) => "client_build_failed",
            Self::Transport { .. } => "network_error",
            Self::ResponseTooLarge => "response_too_large",
            Self::InvalidJson { .. } => "provider_invalid_json",
            Self::ErrorPayload { .. } => "provider_error_payload",
            Self::InvalidPayload { .. } => "provider_invalid_payload",
            Self::IncompletePayload { .. } => "provider_incomplete_payload",
            Self::PaginationCycle { .. } => "provider_pagination_cycle",
            Self::EmptyPagination { .. } => "provider_empty_pagination",
            Self::ChannelResolutionAmbiguous => "channel_resolution_ambiguous",
            Self::Upstream { error_code, .. } => error_code,
        }
    }

    #[must_use]
    pub fn status_code(&self) -> u16 {
        match self {
            Self::Upstream { status, .. } => status.as_u16(),
            Self::InvalidConfig | Self::ClientBuild(_) => 500,
            Self::Transport { .. } => 503,
            Self::ChannelResolutionAmbiguous => 422,
            Self::ResponseTooLarge
            | Self::InvalidJson { .. }
            | Self::ErrorPayload { .. }
            | Self::InvalidPayload { .. }
            | Self::IncompletePayload { .. }
            | Self::PaginationCycle { .. }
            | Self::EmptyPagination { .. } => 502,
        }
    }

    #[must_use]
    pub fn is_retryable(&self) -> bool {
        match self {
            Self::Transport { .. }
            | Self::ResponseTooLarge
            | Self::InvalidJson { .. }
            | Self::ErrorPayload { .. }
            | Self::InvalidPayload { .. }
            | Self::IncompletePayload { .. } => true,
            Self::Upstream { status, .. } => status.as_u16() == 429 || status.is_server_error(),
            Self::InvalidConfig
            | Self::ClientBuild(_)
            | Self::PaginationCycle { .. }
            | Self::EmptyPagination { .. }
            | Self::ChannelResolutionAmbiguous => false,
        }
    }

    #[must_use]
    pub fn operation(&self) -> Option<&'static str> {
        match self {
            Self::Transport { operation, .. }
            | Self::InvalidJson { operation }
            | Self::ErrorPayload { operation, .. }
            | Self::InvalidPayload { operation }
            | Self::IncompletePayload { operation }
            | Self::PaginationCycle { operation }
            | Self::EmptyPagination { operation }
            | Self::Upstream { operation, .. } => Some(*operation),
            _ => None,
        }
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

    #[test]
    fn malformed_success_payload_is_retryable_and_structured() {
        assert!(matches!(
            require_payload("youtube_channel", json!({"channel": {}}), |_| false),
            Err(SearchApiError::InvalidPayload {
                operation: "youtube_channel"
            })
        ));
        let error = SearchApiError::InvalidPayload {
            operation: "youtube_channel",
        };
        assert_eq!(error.code(), "provider_invalid_payload");
        assert_eq!(error.operation(), Some("youtube_channel"));
        assert!(error.is_retryable());
    }

    #[test]
    fn channel_metadata_page_with_next_token_is_valid() {
        let payload = json!({
            "channel": {"id": "UCexample", "videos": 135},
            "pagination": {"next_page_token": "page-2"}
        });
        assert!(validate_channel_videos_payload(payload, false).is_ok());
    }

    #[test]
    fn genuinely_empty_channel_page_is_valid() {
        let payload = json!({"channel": {"id": "UCempty", "videos": 0}});
        assert!(validate_channel_videos_payload(payload, false).is_ok());
    }

    #[test]
    fn populated_channel_without_items_or_token_is_incomplete() {
        assert!(matches!(
            validate_channel_videos_payload(
                json!({
                    "channel": {"id": "UCmissing", "videos": 765}
                }),
                false,
            ),
            Err(error) if error.code() == "provider_incomplete_payload" && error.is_retryable()
        ));
    }

    #[test]
    fn pagination_safety_errors_are_not_retryable() {
        let cycle = SearchApiError::PaginationCycle {
            operation: "youtube_channel_videos",
        };
        let empty = SearchApiError::EmptyPagination {
            operation: "youtube_channel_videos",
        };
        assert_eq!(cycle.code(), "provider_pagination_cycle");
        assert_eq!(empty.code(), "provider_empty_pagination");
        assert!(!cycle.is_retryable());
        assert!(!empty.is_retryable());
    }
}
