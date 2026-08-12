//! Versioned contracts shared across Rust components and the Python tokenizer.

use serde::{Deserialize, Serialize};

pub const TOKENIZER_ANALYZER_VERSION: &str = "mecab-nltk-v1";
pub const TOKENIZER_MAX_DOCUMENTS: usize = 16;
pub const TOKENIZER_MAX_SEGMENTS_PER_DOCUMENT: usize = 1_000;
pub const TOKENIZER_MAX_TOTAL_TEXT_BYTES: usize = 4 * 1024 * 1024;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
pub struct TokenizeRequest {
    pub analyzer_version: String,
    pub documents: Vec<TokenizeDocument>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
pub struct TokenizeDocument {
    pub id: String,
    pub text: String,
    #[serde(default)]
    pub segments: Vec<TokenizeSegment>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
pub struct TokenizeSegment {
    pub sequence: i32,
    pub text: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
pub struct TokenizeResponse {
    pub analyzer_version: String,
    pub documents: Vec<TokenizedDocument>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
pub struct TokenizedDocument {
    pub id: String,
    pub tokens: Vec<String>,
    pub segments: Vec<TokenizedSegment>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
pub struct TokenizedSegment {
    pub sequence: i32,
    pub tokens: Vec<String>,
}

#[cfg(test)]
mod tests {
    use super::{TOKENIZER_ANALYZER_VERSION, TokenizeRequest};

    #[test]
    fn tokenizer_contract_rejects_unknown_fields() {
        let payload = format!(
            r#"{{"analyzerVersion":"{TOKENIZER_ANALYZER_VERSION}","documents":[],"unexpected":true}}"#
        );
        assert!(serde_json::from_str::<TokenizeRequest>(&payload).is_err());
    }
}
