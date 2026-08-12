use monitube_contracts::{
    TOKENIZER_ANALYZER_VERSION, TOKENIZER_MAX_DOCUMENTS, TOKENIZER_MAX_SEGMENTS_PER_DOCUMENT,
    TOKENIZER_MAX_TOTAL_TEXT_BYTES, TokenizeRequest, TokenizeResponse,
};
use reqwest::{Client, StatusCode};
use std::collections::{HashMap, HashSet};
use std::time::Duration;
use thiserror::Error;

const MAX_RESPONSE_BYTES: usize = 16 * 1024 * 1024;

#[derive(Clone)]
pub struct TokenizerClient {
    client: Client,
    endpoint: String,
}

impl TokenizerClient {
    /// Creates a bounded HTTP client for the internal tokenizer service.
    ///
    /// # Errors
    ///
    /// Returns an error if the underlying HTTP client cannot be constructed.
    pub fn new(base_url: &str, timeout: Duration) -> Result<Self, TokenizerClientError> {
        let endpoint = format!("{}/internal/v1/tokenize", base_url.trim_end_matches('/'));
        let client = Client::builder()
            .connect_timeout(Duration::from_millis(500))
            .timeout(timeout)
            .build()
            .map_err(TokenizerClientError::Build)?;
        Ok(Self { client, endpoint })
    }

    /// Tokenizes a versioned, bounded document batch.
    ///
    /// # Errors
    ///
    /// Returns an error for invalid request bounds, transport or HTTP failures,
    /// oversized responses, malformed JSON, or analyzer-version mismatch.
    pub async fn tokenize(
        &self,
        request: &TokenizeRequest,
    ) -> Result<TokenizeResponse, TokenizerClientError> {
        validate_request(request)?;
        let mut response = self
            .client
            .post(&self.endpoint)
            .json(request)
            .send()
            .await
            .map_err(TokenizerClientError::Request)?;
        let status = response.status();
        if !status.is_success() {
            return Err(TokenizerClientError::Status(status));
        }
        if response.content_length().is_some_and(|length| {
            usize::try_from(length).map_or(true, |value| value > MAX_RESPONSE_BYTES)
        }) {
            return Err(TokenizerClientError::ResponseTooLarge {
                maximum: MAX_RESPONSE_BYTES,
            });
        }
        let mut bytes = Vec::new();
        while let Some(chunk) = response
            .chunk()
            .await
            .map_err(TokenizerClientError::Request)?
        {
            if bytes.len().saturating_add(chunk.len()) > MAX_RESPONSE_BYTES {
                return Err(TokenizerClientError::ResponseTooLarge {
                    maximum: MAX_RESPONSE_BYTES,
                });
            }
            bytes.extend_from_slice(&chunk);
        }
        let parsed: TokenizeResponse =
            serde_json::from_slice(&bytes).map_err(TokenizerClientError::Decode)?;
        if parsed.analyzer_version != TOKENIZER_ANALYZER_VERSION {
            return Err(TokenizerClientError::VersionMismatch {
                expected: TOKENIZER_ANALYZER_VERSION,
                actual: parsed.analyzer_version,
            });
        }
        validate_response_shape(request, &parsed)?;
        Ok(parsed)
    }
}

fn validate_request(request: &TokenizeRequest) -> Result<(), TokenizerClientError> {
    if request.analyzer_version != TOKENIZER_ANALYZER_VERSION {
        return Err(TokenizerClientError::VersionMismatch {
            expected: TOKENIZER_ANALYZER_VERSION,
            actual: request.analyzer_version.clone(),
        });
    }
    if request.documents.is_empty() || request.documents.len() > TOKENIZER_MAX_DOCUMENTS {
        return Err(TokenizerClientError::InvalidDocumentCount {
            maximum: TOKENIZER_MAX_DOCUMENTS,
        });
    }

    let mut total_bytes = 0_usize;
    let mut document_ids = HashSet::new();
    for document in &request.documents {
        if document.id.is_empty()
            || document.id.chars().count() > 255
            || !document_ids.insert(document.id.as_str())
        {
            return Err(TokenizerClientError::InvalidRequestShape);
        }
        if document.segments.len() > TOKENIZER_MAX_SEGMENTS_PER_DOCUMENT {
            return Err(TokenizerClientError::TooManySegments {
                maximum: TOKENIZER_MAX_SEGMENTS_PER_DOCUMENT,
            });
        }
        total_bytes = total_bytes.saturating_add(document.text.len());
        let mut sequences = HashSet::new();
        for segment in &document.segments {
            if segment.sequence < 0 || !sequences.insert(segment.sequence) {
                return Err(TokenizerClientError::InvalidRequestShape);
            }
            total_bytes = total_bytes.saturating_add(segment.text.len());
        }
    }
    if total_bytes > TOKENIZER_MAX_TOTAL_TEXT_BYTES {
        return Err(TokenizerClientError::RequestTooLarge {
            maximum: TOKENIZER_MAX_TOTAL_TEXT_BYTES,
        });
    }
    Ok(())
}

fn validate_response_shape(
    request: &TokenizeRequest,
    response: &TokenizeResponse,
) -> Result<(), TokenizerClientError> {
    if request.documents.len() != response.documents.len() {
        return Err(TokenizerClientError::InvalidResponseShape);
    }
    let expected = request
        .documents
        .iter()
        .map(|document| (document.id.as_str(), document))
        .collect::<HashMap<_, _>>();
    let mut returned_ids = HashSet::new();
    for document in &response.documents {
        let Some(expected_document) = expected.get(document.id.as_str()) else {
            return Err(TokenizerClientError::InvalidResponseShape);
        };
        if !returned_ids.insert(document.id.as_str())
            || document.tokens.iter().any(String::is_empty)
            || document.segments.len() != expected_document.segments.len()
        {
            return Err(TokenizerClientError::InvalidResponseShape);
        }
        let expected_sequences = expected_document
            .segments
            .iter()
            .map(|segment| segment.sequence)
            .collect::<HashSet<_>>();
        let mut returned_sequences = HashSet::new();
        if document.segments.iter().any(|segment| {
            !expected_sequences.contains(&segment.sequence)
                || !returned_sequences.insert(segment.sequence)
                || segment.tokens.iter().any(String::is_empty)
        }) {
            return Err(TokenizerClientError::InvalidResponseShape);
        }
    }
    Ok(())
}

#[derive(Debug, Error)]
pub enum TokenizerClientError {
    #[error("could not build tokenizer client")]
    Build(#[source] reqwest::Error),
    #[error("tokenizer request failed")]
    Request(#[source] reqwest::Error),
    #[error("tokenizer returned HTTP {0}")]
    Status(StatusCode),
    #[error("tokenizer response could not be decoded")]
    Decode(#[source] serde_json::Error),
    #[error("tokenizer analyzer version mismatch: expected {expected}, got {actual}")]
    VersionMismatch {
        expected: &'static str,
        actual: String,
    },
    #[error("tokenizer document count must be between 1 and {maximum}")]
    InvalidDocumentCount { maximum: usize },
    #[error("tokenizer document exceeds {maximum} segments")]
    TooManySegments { maximum: usize },
    #[error("tokenizer request document identifiers or segments are invalid")]
    InvalidRequestShape,
    #[error("tokenizer request exceeds {maximum} text bytes")]
    RequestTooLarge { maximum: usize },
    #[error("tokenizer response exceeds {maximum} bytes")]
    ResponseTooLarge { maximum: usize },
    #[error("tokenizer response does not match the requested document shape")]
    InvalidResponseShape,
}

#[cfg(test)]
mod tests {
    use super::{validate_request, validate_response_shape};
    use monitube_contracts::{
        TOKENIZER_ANALYZER_VERSION, TokenizeDocument, TokenizeRequest, TokenizeResponse,
        TokenizedDocument,
    };

    #[test]
    fn response_must_preserve_requested_document_identity() {
        let request = TokenizeRequest {
            analyzer_version: TOKENIZER_ANALYZER_VERSION.to_owned(),
            documents: vec![TokenizeDocument {
                id: "expected".to_owned(),
                text: "데이터".to_owned(),
                segments: Vec::new(),
            }],
        };
        let response = TokenizeResponse {
            analyzer_version: TOKENIZER_ANALYZER_VERSION.to_owned(),
            documents: vec![TokenizedDocument {
                id: "different".to_owned(),
                tokens: vec!["데이터".to_owned()],
                segments: Vec::new(),
            }],
        };
        assert!(validate_response_shape(&request, &response).is_err());
    }

    #[test]
    fn request_rejects_duplicate_document_ids() {
        let document = TokenizeDocument {
            id: "same".to_owned(),
            text: "데이터".to_owned(),
            segments: Vec::new(),
        };
        let request = TokenizeRequest {
            analyzer_version: TOKENIZER_ANALYZER_VERSION.to_owned(),
            documents: vec![document.clone(), document],
        };
        assert!(validate_request(&request).is_err());
    }
}
