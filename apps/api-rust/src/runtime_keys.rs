//! Server-authorized registration of encrypted `YouTube` runtime keys.

use crate::AppState;
use axum::Json;
use axum::extract::State;
use axum::http::{HeaderMap, StatusCode, header::AUTHORIZATION};
use axum::response::{IntoResponse, Response};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::HashSet;
use subtle::ConstantTimeEq;
use thiserror::Error;
use uuid::Uuid;

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
pub struct RuntimeKeyRegistration {
    api_keys: Vec<String>,
}

pub async fn register(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(payload): Json<RuntimeKeyRegistration>,
) -> Result<(StatusCode, Json<Value>), RuntimeKeyError> {
    let token = state
        .youtube_key_registration_token
        .as_deref()
        .ok_or(RuntimeKeyError::Unauthorized)?;
    let expected = format!("Bearer {token}");
    let provided = headers
        .get(AUTHORIZATION)
        .and_then(|value| value.to_str().ok())
        .ok_or(RuntimeKeyError::Unauthorized)?;
    if expected.as_bytes().ct_eq(provided.as_bytes()).unwrap_u8() != 1 {
        return Err(RuntimeKeyError::Unauthorized);
    }
    let encryption_key = state
        .youtube_api_key_encryption_key
        .as_deref()
        .ok_or(RuntimeKeyError::Unavailable)?;
    let keys = normalize_keys(payload.api_keys)?;
    let mut transaction = state.pool.begin().await?;
    let runtime_config_id = sqlx::query_scalar::<_, Uuid>(
        r"
        SELECT id FROM youtube_runtime_configs
        WHERE status = 'active'
        ORDER BY activated_at DESC
        LIMIT 1
        ",
    )
    .fetch_optional(&mut *transaction)
    .await?
    .ok_or(RuntimeKeyError::Unavailable)?;
    for key in &keys {
        let fingerprint = hex::encode(Sha256::digest(key.as_bytes()));
        sqlx::query(
            r"
            INSERT INTO youtube_runtime_keys (
              runtime_config_id, key_fingerprint, encrypted_key, status, unavailable_until
            )
            VALUES (
              $1, $2, pgp_sym_encrypt($3, $4, 'cipher-algo=aes256,compress-algo=0'),
              'active', NULL
            )
            ON CONFLICT (runtime_config_id, key_fingerprint) DO UPDATE
            SET encrypted_key = EXCLUDED.encrypted_key,
                status = 'active',
                unavailable_until = NULL,
                updated_at = now()
            ",
        )
        .bind(runtime_config_id)
        .bind(&fingerprint[..24])
        .bind(key)
        .bind(encryption_key)
        .execute(&mut *transaction)
        .await?;
    }
    transaction.commit().await?;
    Ok((StatusCode::CREATED, Json(json!({"accepted": keys.len()}))))
}

fn normalize_keys(keys: Vec<String>) -> Result<Vec<String>, RuntimeKeyError> {
    if keys.is_empty() || keys.len() > 32 {
        return Err(RuntimeKeyError::InvalidKeys);
    }
    let mut seen = HashSet::with_capacity(keys.len());
    let mut normalized = Vec::with_capacity(keys.len());
    for key in keys {
        let key = key.trim().to_owned();
        let length = key.chars().count();
        if !(20..=256).contains(&length) {
            return Err(RuntimeKeyError::InvalidKeys);
        }
        if seen.insert(key.clone()) {
            normalized.push(key);
        }
    }
    if normalized.is_empty() {
        return Err(RuntimeKeyError::InvalidKeys);
    }
    Ok(normalized)
}

#[derive(Debug, Error)]
pub enum RuntimeKeyError {
    #[error("runtime key registration is unauthorized")]
    Unauthorized,
    #[error("runtime key registration is unavailable")]
    Unavailable,
    #[error("runtime keys are invalid")]
    InvalidKeys,
    #[error("runtime key database operation failed")]
    Database(#[from] sqlx::Error),
}

impl IntoResponse for RuntimeKeyError {
    fn into_response(self) -> Response {
        let (status, detail, retryable) = match self {
            Self::Unauthorized => (StatusCode::UNAUTHORIZED, "Unauthorized", false),
            Self::Unavailable => (
                StatusCode::SERVICE_UNAVAILABLE,
                "Key registration is unavailable",
                true,
            ),
            Self::InvalidKeys => (
                StatusCode::UNPROCESSABLE_ENTITY,
                "Provide one to 32 valid API keys",
                false,
            ),
            Self::Database(error) => {
                let failure = crate::db_error::classify(&error, "runtime key registration");
                (failure.status, failure.detail, failure.retryable)
            }
        };
        (status, Json(RuntimeKeyErrorResponse { detail, retryable })).into_response()
    }
}

#[derive(Serialize)]
struct RuntimeKeyErrorResponse {
    detail: &'static str,
    #[serde(skip_serializing_if = "crate::is_false")]
    retryable: bool,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn registration_keys_are_trimmed_and_deduplicated() -> Result<(), RuntimeKeyError> {
        let key = "a-valid-runtime-key-123";
        let keys = normalize_keys(vec![format!(" {key} "), key.to_owned()])?;
        assert_eq!(keys, vec![key]);
        Ok(())
    }
}
