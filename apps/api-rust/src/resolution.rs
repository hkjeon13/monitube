//! Pure `YouTube` channel and video input normalization.

use axum::Json;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use serde::{Deserialize, Serialize};
use thiserror::Error;
use unicode_general_category::{GeneralCategory, get_general_category};
use unicode_normalization::UnicodeNormalization;
use url::Url;

const MAX_INPUT_CHARACTERS: usize = 2_048;

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ResolutionRequest {
    input: String,
}

#[derive(Debug, Serialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct ChannelResolutionResponse {
    kind: ChannelKind,
    normalized: String,
    lookup: ChannelLookup,
    requires_search: bool,
}

#[derive(Debug, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
enum ChannelKind {
    ChannelId,
    Handle,
    LegacyUsername,
    AmbiguousName,
}

#[derive(Debug, Serialize, PartialEq, Eq)]
struct ChannelLookup {
    parameter: &'static str,
    value: String,
}

#[derive(Debug, Serialize, PartialEq, Eq)]
pub struct VideoResolutionResponse {
    kind: VideoKind,
    normalized: String,
}

#[derive(Debug, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
enum VideoKind {
    VideoId,
    WatchUrl,
    ShortUrl,
}

pub async fn resolve_channel(
    Json(request): Json<ResolutionRequest>,
) -> Result<Json<ChannelResolutionResponse>, ResolutionError> {
    resolve_channel_value(&request.input).map(Json)
}

pub async fn resolve_video(
    Json(request): Json<ResolutionRequest>,
) -> Result<Json<VideoResolutionResponse>, ResolutionError> {
    resolve_video_value(&request.input).map(Json)
}

fn resolve_channel_value(value: &str) -> Result<ChannelResolutionResponse, ResolutionError> {
    let cleaned = clean_channel(value)?;
    let (kind, normalized, parameter, requires_search) =
        if looks_like_host(&cleaned, &["youtube.com"]) {
            channel_from_url(&cleaned)?
        } else if has_uri_scheme(&cleaned) {
            return Err(ResolutionError::InvalidChannel(
                "Only YouTube channel URLs are accepted",
            ));
        } else if is_channel_id(&cleaned) {
            (ChannelKind::ChannelId, cleaned, "id", false)
        } else if cleaned.starts_with('@') {
            (
                ChannelKind::Handle,
                validate_handle(&cleaned)?,
                "forHandle",
                false,
            )
        } else {
            (ChannelKind::AmbiguousName, cleaned, "search", true)
        };

    Ok(ChannelResolutionResponse {
        kind,
        lookup: ChannelLookup {
            parameter,
            value: normalized.clone(),
        },
        normalized,
        requires_search,
    })
}

fn resolve_video_value(value: &str) -> Result<VideoResolutionResponse, ResolutionError> {
    let cleaned = clean_video(value)?;
    if looks_like_host(&cleaned, &["youtube.com", "youtu.be"]) {
        return video_from_url(&cleaned);
    }
    if has_uri_scheme(&cleaned) {
        return Err(ResolutionError::InvalidVideo(
            "Only youtube.com and youtu.be video URLs are accepted",
        ));
    }
    Ok(VideoResolutionResponse {
        kind: VideoKind::VideoId,
        normalized: validate_video_id(&cleaned)?,
    })
}

pub(crate) fn normalize_channel_input(value: &str) -> Result<String, ResolutionError> {
    resolve_channel_value(value).map(|resolution| resolution.normalized)
}

pub(crate) fn channel_identity(value: &str) -> Result<(&'static str, String), ResolutionError> {
    let resolution = resolve_channel_value(value)?;
    let kind = match resolution.kind {
        ChannelKind::ChannelId => "channel_id",
        ChannelKind::Handle => "handle",
        ChannelKind::LegacyUsername => "legacy_username",
        ChannelKind::AmbiguousName => "ambiguous_name",
    };
    Ok((kind, resolution.normalized))
}

pub(crate) fn normalize_video_input(value: &str) -> Result<String, ResolutionError> {
    resolve_video_value(value).map(|resolution| resolution.normalized)
}

fn clean_channel(value: &str) -> Result<String, ResolutionError> {
    validate_input_length(value, ResolutionError::InvalidChannel)?;
    let cleaned = value.split_whitespace().collect::<Vec<_>>().join(" ");
    if cleaned.is_empty() {
        return Err(ResolutionError::InvalidChannel(
            "Channel input cannot be empty",
        ));
    }
    Ok(cleaned)
}

fn clean_video(value: &str) -> Result<String, ResolutionError> {
    validate_input_length(value, ResolutionError::InvalidVideo)?;
    let cleaned = value.trim().to_owned();
    if cleaned.is_empty() {
        return Err(ResolutionError::InvalidVideo("Video input cannot be empty"));
    }
    Ok(cleaned)
}

fn validate_input_length(
    value: &str,
    error: fn(&'static str) -> ResolutionError,
) -> Result<(), ResolutionError> {
    if value.chars().count() > MAX_INPUT_CHARACTERS {
        return Err(error("Input exceeds 2048 characters"));
    }
    Ok(())
}

fn channel_from_url(
    value: &str,
) -> Result<(ChannelKind, String, &'static str, bool), ResolutionError> {
    let parsed = parse_url(value, ResolutionError::InvalidChannel)?;
    if !matches!(
        parsed.host_str(),
        Some("youtube.com" | "www.youtube.com" | "m.youtube.com")
    ) {
        return Err(ResolutionError::InvalidChannel(
            "Only youtube.com channel URLs are accepted",
        ));
    }
    let pieces = decoded_path_segments(&parsed, ResolutionError::InvalidChannel)?;
    let Some(first) = pieces.first() else {
        return Err(ResolutionError::InvalidChannel(
            "A YouTube channel URL must include a channel identifier",
        ));
    };
    if first == "channel" {
        let channel_id = pieces.get(1).ok_or(ResolutionError::InvalidChannel(
            "Invalid YouTube channel ID in URL",
        ))?;
        if !is_channel_id(channel_id) {
            return Err(ResolutionError::InvalidChannel(
                "Invalid YouTube channel ID in URL",
            ));
        }
        return Ok((ChannelKind::ChannelId, channel_id.clone(), "id", false));
    }
    if first.starts_with('@') {
        return Ok((
            ChannelKind::Handle,
            validate_handle(first)?,
            "forHandle",
            false,
        ));
    }
    if first == "user" {
        let username = pieces.get(1).ok_or(ResolutionError::InvalidChannel(
            "Invalid legacy YouTube username",
        ))?;
        if !is_legacy_username(username) {
            return Err(ResolutionError::InvalidChannel(
                "Invalid legacy YouTube username",
            ));
        }
        return Ok((
            ChannelKind::LegacyUsername,
            username.clone(),
            "forUsername",
            false,
        ));
    }
    if first == "c" {
        let name = pieces.get(1).ok_or(ResolutionError::InvalidChannel(
            "Unsupported YouTube channel URL format",
        ))?;
        return Ok((ChannelKind::AmbiguousName, name.clone(), "search", true));
    }
    Err(ResolutionError::InvalidChannel(
        "Unsupported YouTube channel URL format",
    ))
}

fn video_from_url(value: &str) -> Result<VideoResolutionResponse, ResolutionError> {
    let parsed = parse_url(value, ResolutionError::InvalidVideo)?;
    let pieces = decoded_path_segments(&parsed, ResolutionError::InvalidVideo)?;
    match parsed.host_str() {
        Some("youtu.be" | "www.youtu.be") => {
            let video_id = pieces.first().ok_or(ResolutionError::InvalidVideo(
                "A youtu.be URL must include a video ID",
            ))?;
            Ok(VideoResolutionResponse {
                kind: VideoKind::ShortUrl,
                normalized: validate_video_id(video_id)?,
            })
        }
        Some("youtube.com" | "www.youtube.com" | "m.youtube.com") => {
            if parsed.path().trim_end_matches('/') == "/watch" {
                let video_id = parsed
                    .query_pairs()
                    .find_map(|(name, value)| (name == "v").then(|| value.into_owned()))
                    .ok_or(ResolutionError::InvalidVideo(
                        "A YouTube watch URL must include its v parameter",
                    ))?;
                return Ok(VideoResolutionResponse {
                    kind: VideoKind::WatchUrl,
                    normalized: validate_video_id(&video_id)?,
                });
            }
            if matches!(
                pieces.first().map(String::as_str),
                Some("shorts" | "embed" | "live")
            ) {
                let video_id = pieces.get(1).ok_or(ResolutionError::InvalidVideo(
                    "Unsupported YouTube video URL format",
                ))?;
                return Ok(VideoResolutionResponse {
                    kind: VideoKind::WatchUrl,
                    normalized: validate_video_id(video_id)?,
                });
            }
            Err(ResolutionError::InvalidVideo(
                "Unsupported YouTube video URL format",
            ))
        }
        _ => Err(ResolutionError::InvalidVideo(
            "Only youtube.com and youtu.be video URLs are accepted",
        )),
    }
}

fn parse_url(
    value: &str,
    error: fn(&'static str) -> ResolutionError,
) -> Result<Url, ResolutionError> {
    let candidate = if value.contains("://") {
        value.to_owned()
    } else {
        format!("https://{value}")
    };
    Url::parse(&candidate).map_err(|_| error("URL could not be parsed"))
}

fn decoded_path_segments(
    url: &Url,
    error: fn(&'static str) -> ResolutionError,
) -> Result<Vec<String>, ResolutionError> {
    url.path_segments()
        .ok_or_else(|| error("URL path could not be parsed"))?
        .filter(|piece| !piece.is_empty())
        .map(|piece| {
            percent_encoding::percent_decode_str(piece)
                .decode_utf8()
                .map(std::borrow::Cow::into_owned)
                .map_err(|_| error("URL path is not valid UTF-8"))
        })
        .collect()
}

fn validate_handle(value: &str) -> Result<String, ResolutionError> {
    let normalized = value.nfc().collect::<String>();
    let Some(body) = normalized.strip_prefix('@') else {
        return Err(ResolutionError::InvalidChannel("Invalid YouTube handle"));
    };
    let separators = ['.', '_', '-', '·'];
    let body_length = body.chars().count();
    let valid = (1..=30).contains(&body_length)
        && body
            .chars()
            .next()
            .is_some_and(|character| !separators.contains(&character))
        && body
            .chars()
            .last()
            .is_some_and(|character| !separators.contains(&character))
        && body.chars().all(|character| {
            character.is_alphanumeric()
                || separators.contains(&character)
                || matches!(
                    get_general_category(character),
                    GeneralCategory::NonspacingMark
                        | GeneralCategory::SpacingMark
                        | GeneralCategory::EnclosingMark
                )
        });
    if !valid {
        return Err(ResolutionError::InvalidChannel("Invalid YouTube handle"));
    }
    Ok(normalized)
}

fn validate_video_id(value: &str) -> Result<String, ResolutionError> {
    let valid = value.len() == 11
        && value
            .bytes()
            .all(|character| character.is_ascii_alphanumeric() || matches!(character, b'_' | b'-'));
    if !valid {
        return Err(ResolutionError::InvalidVideo(
            "A YouTube video ID must contain exactly 11 URL-safe characters",
        ));
    }
    Ok(value.to_owned())
}

fn is_channel_id(value: &str) -> bool {
    value.len() == 24
        && value.starts_with("UC")
        && value[2..]
            .bytes()
            .all(|character| character.is_ascii_alphanumeric() || matches!(character, b'_' | b'-'))
}

fn is_legacy_username(value: &str) -> bool {
    (1..=100).contains(&value.len())
        && value.bytes().all(|character| {
            character.is_ascii_alphanumeric() || matches!(character, b'.' | b'_' | b'-')
        })
}

fn looks_like_host(value: &str, hosts: &[&str]) -> bool {
    let lowercase = value.to_ascii_lowercase();
    let without_scheme = lowercase
        .strip_prefix("https://")
        .or_else(|| lowercase.strip_prefix("http://"))
        .unwrap_or(&lowercase);
    let normalized = without_scheme
        .strip_prefix("www.")
        .or_else(|| without_scheme.strip_prefix("m."))
        .unwrap_or(without_scheme);
    hosts.iter().any(|host| {
        normalized == *host
            || normalized
                .strip_prefix(*host)
                .is_some_and(|remainder| remainder.starts_with('/'))
    })
}

fn has_uri_scheme(value: &str) -> bool {
    let Some((scheme, _)) = value.split_once("://") else {
        return false;
    };
    let mut bytes = scheme.bytes();
    bytes
        .next()
        .is_some_and(|first| first.is_ascii_alphabetic())
        && bytes.all(|character| {
            character.is_ascii_alphanumeric() || matches!(character, b'+' | b'.' | b'-')
        })
}

#[derive(Debug, Error)]
pub enum ResolutionError {
    #[error("{0}")]
    InvalidChannel(&'static str),
    #[error("{0}")]
    InvalidVideo(&'static str),
}

impl IntoResponse for ResolutionError {
    fn into_response(self) -> Response {
        let detail = match self {
            Self::InvalidChannel(detail) | Self::InvalidVideo(detail) => detail,
        };
        (
            StatusCode::UNPROCESSABLE_ENTITY,
            Json(ErrorResponse { detail }),
        )
            .into_response()
    }
}

#[derive(Serialize)]
struct ErrorResponse {
    detail: &'static str,
}

#[cfg(test)]
mod tests {
    use super::{ChannelKind, VideoKind, resolve_channel_value, resolve_video_value};

    #[test]
    fn channel_resolution_matches_python_characterization() -> Result<(), super::ResolutionError> {
        let channel =
            resolve_channel_value("https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv")?;
        let handle = resolve_channel_value("youtube.com/@%EC%9A%B0%EC%A0%95%EC%9E%89/videos")?;
        assert_eq!(channel.kind, ChannelKind::ChannelId);
        assert_eq!(channel.lookup.parameter, "id");
        assert_eq!(handle.kind, ChannelKind::Handle);
        assert_eq!(handle.normalized, "@우정잉");
        Ok(())
    }

    #[test]
    fn video_resolution_matches_python_characterization() -> Result<(), super::ResolutionError> {
        let direct = resolve_video_value("dQw4w9WgXcQ")?;
        let watch =
            resolve_video_value("https://www.youtube.com/watch?v=dQw4w9WgXcQ&feature=share")?;
        let short = resolve_video_value("youtu.be/dQw4w9WgXcQ?t=42")?;
        assert_eq!(direct.kind, VideoKind::VideoId);
        assert_eq!(watch.kind, VideoKind::WatchUrl);
        assert_eq!(short.kind, VideoKind::ShortUrl);
        assert_eq!(direct.normalized, watch.normalized);
        assert_eq!(watch.normalized, short.normalized);
        Ok(())
    }

    #[test]
    fn external_urls_and_malformed_handles_are_rejected() {
        assert!(
            resolve_channel_value("https://example.com/channel/UCabcdefghijklmnopqrstuv").is_err()
        );
        assert!(resolve_channel_value("@ invalid").is_err());
        assert!(resolve_video_value("https://example.com/watch?v=dQw4w9WgXcQ").is_err());
    }
}
