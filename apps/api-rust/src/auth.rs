//! Browser-session authentication compatible with the Python service.

use crate::AppState;
use axum::Json;
use axum::extract::{Extension, State};
use axum::http::header::{COOKIE, SET_COOKIE};
use axum::http::{HeaderMap, HeaderValue, Request, StatusCode};
use axum::middleware::Next;
use axum::response::{IntoResponse, Response};
use base64::Engine;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use chrono::{Duration, Utc};
use pbkdf2::pbkdf2_hmac;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use sqlx::PgPool;
use subtle::ConstantTimeEq;
use thiserror::Error;
use uuid::Uuid;

const SESSION_DAYS: i64 = 90;
const SESSION_MAX_AGE_SECONDS: i64 = 60 * 60 * 24 * SESSION_DAYS;
const SESSION_REFRESH_WINDOW_DAYS: i64 = 30;
const PBKDF2_ITERATIONS: u32 = 310_000;
const SALT_BYTES: usize = 16;
const SESSION_TOKEN_BYTES: usize = 32;

#[derive(Debug, Clone)]
pub struct AuthUser {
    pub id: Uuid,
    pub username: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LoginRequest {
    username: String,
    password: String,
}

#[derive(Debug, Serialize)]
pub struct AuthUserResponse {
    username: String,
}

impl From<AuthUser> for AuthUserResponse {
    fn from(user: AuthUser) -> Self {
        Self {
            username: user.username,
        }
    }
}

#[derive(Debug, Clone)]
pub struct AuthService {
    pool: PgPool,
}

impl AuthService {
    #[must_use]
    pub const fn new(pool: PgPool) -> Self {
        Self { pool }
    }

    async fn register(&self, request: LoginRequest) -> Result<AuthUser, AuthError> {
        let (username, password) = validate_login_request(&request)?;
        let password_hash = tokio::task::spawn_blocking(move || hash_password(&password))
            .await
            .map_err(AuthError::PasswordWorker)??;
        let mut transaction = self.pool.begin().await?;
        let inserted = sqlx::query_as::<_, (Uuid, String)>(
            "INSERT INTO app_users (username, password_hash) VALUES ($1, $2) RETURNING id, username",
        )
        .bind(&username)
        .bind(password_hash)
        .fetch_one(&mut *transaction)
        .await;
        let (id, username) = match inserted {
            Ok(row) => row,
            Err(error) if is_unique_violation(&error) => return Err(AuthError::UsernameConflict),
            Err(error) => return Err(AuthError::Database(error)),
        };

        if username == "psyche" {
            sqlx::query("UPDATE collection_sources SET owner_id = $1 WHERE owner_id IS NULL")
                .bind(id)
                .execute(&mut *transaction)
                .await?;
            sqlx::query("UPDATE collection_targets SET owner_id = $1 WHERE owner_id IS NULL")
                .bind(id)
                .execute(&mut *transaction)
                .await?;
            sqlx::query(
                r"
                INSERT INTO collection_subscriptions (
                    user_id, target_id, display_config, enabled, created_at, updated_at
                )
                SELECT $1, source.target_id, source.config, source.enabled,
                       source.created_at, source.updated_at
                FROM collection_sources source
                WHERE source.owner_id = $1
                  AND source.target_id IS NOT NULL
                ON CONFLICT (user_id, target_id) DO NOTHING
                ",
            )
            .bind(id)
            .execute(&mut *transaction)
            .await?;
        }

        transaction.commit().await?;
        Ok(AuthUser { id, username })
    }

    async fn authenticate(&self, request: LoginRequest) -> Result<Option<AuthUser>, AuthError> {
        let (username, password) = validate_login_request(&request)?;
        let row = sqlx::query_as::<_, (Uuid, String, String)>(
            "SELECT id, username, password_hash FROM app_users WHERE username = $1",
        )
        .bind(username)
        .fetch_optional(&self.pool)
        .await?;
        let Some((id, username, encoded)) = row else {
            return Ok(None);
        };
        let valid = tokio::task::spawn_blocking(move || verify_password(&password, &encoded))
            .await
            .map_err(AuthError::PasswordWorker)?;
        Ok(valid.then_some(AuthUser { id, username }))
    }

    async fn create_session(&self, user_id: Uuid) -> Result<String, AuthError> {
        let mut token_bytes = [0_u8; SESSION_TOKEN_BYTES];
        getrandom::fill(&mut token_bytes).map_err(AuthError::Random)?;
        let token = URL_SAFE_NO_PAD.encode(token_bytes);
        let token_hash = token_hash(&token);
        let expires_at = Utc::now() + Duration::days(SESSION_DAYS);
        sqlx::query(
            "INSERT INTO app_sessions (user_id, token_hash, expires_at) VALUES ($1, $2, $3)",
        )
        .bind(user_id)
        .bind(token_hash)
        .bind(expires_at)
        .execute(&self.pool)
        .await?;
        Ok(token)
    }

    async fn user_for_session(&self, token: &str) -> Result<Option<AuthUser>, AuthError> {
        let row = sqlx::query_as::<_, (Uuid, String)>(
            r"
            SELECT app_users.id, app_users.username
            FROM app_sessions
            JOIN app_users ON app_users.id = app_sessions.user_id
            WHERE app_sessions.token_hash = $1
              AND app_sessions.expires_at > now()
            ",
        )
        .bind(token_hash(token))
        .fetch_optional(&self.pool)
        .await?;
        Ok(row.map(|(id, username)| AuthUser { id, username }))
    }

    async fn refresh_session(&self, token: &str) -> Result<(), AuthError> {
        sqlx::query(
            r"
            UPDATE app_sessions
            SET expires_at = now() + ($1 * interval '1 day')
            WHERE token_hash = $2
              AND expires_at > now()
              AND expires_at < now() + ($3 * interval '1 day')
            ",
        )
        .bind(SESSION_DAYS)
        .bind(token_hash(token))
        .bind(SESSION_REFRESH_WINDOW_DAYS)
        .execute(&self.pool)
        .await?;
        Ok(())
    }

    async fn revoke_session(&self, token: Option<&str>) -> Result<(), AuthError> {
        if let Some(token) = token {
            sqlx::query("DELETE FROM app_sessions WHERE token_hash = $1")
                .bind(token_hash(token))
                .execute(&self.pool)
                .await?;
        }
        Ok(())
    }
}

pub async fn register(
    State(state): State<AppState>,
    Json(request): Json<LoginRequest>,
) -> Result<Response, AuthError> {
    let user = state.auth.register(request).await?;
    let token = state.auth.create_session(user.id).await?;
    auth_response(StatusCode::CREATED, user, &token, state.secure_cookies)
}

pub async fn login(
    State(state): State<AppState>,
    Json(request): Json<LoginRequest>,
) -> Result<Response, AuthError> {
    let user = state
        .auth
        .authenticate(request)
        .await?
        .ok_or(AuthError::InvalidCredentials)?;
    let token = state.auth.create_session(user.id).await?;
    auth_response(StatusCode::OK, user, &token, state.secure_cookies)
}

pub async fn logout(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> Result<Response, AuthError> {
    state.auth.revoke_session(session_token(&headers)).await?;
    let mut response = StatusCode::NO_CONTENT.into_response();
    response.headers_mut().insert(
        SET_COOKIE,
        HeaderValue::from_static("monitube_session=; HttpOnly; Max-Age=0; Path=/; SameSite=Lax"),
    );
    Ok(response)
}

pub async fn me(Extension(user): Extension<AuthUser>) -> Json<AuthUserResponse> {
    Json(user.into())
}

pub async fn require_session(
    State(state): State<AppState>,
    mut request: Request<axum::body::Body>,
    next: Next,
) -> Result<Response, AuthError> {
    let token = session_token(request.headers())
        .map(str::to_owned)
        .ok_or(AuthError::LoginRequired)?;
    let user = state
        .auth
        .user_for_session(&token)
        .await?
        .ok_or(AuthError::LoginRequired)?;
    state.auth.refresh_session(&token).await?;
    request.extensions_mut().insert(user);
    let mut response = next.run(request).await;
    response
        .headers_mut()
        .append(SET_COOKIE, session_cookie(&token, state.secure_cookies)?);
    Ok(response)
}

fn auth_response(
    status: StatusCode,
    user: AuthUser,
    token: &str,
    secure: bool,
) -> Result<Response, AuthError> {
    let mut response = (status, Json(AuthUserResponse::from(user))).into_response();
    response
        .headers_mut()
        .insert(SET_COOKIE, session_cookie(token, secure)?);
    Ok(response)
}

fn session_cookie(token: &str, secure: bool) -> Result<HeaderValue, AuthError> {
    let secure_attribute = if secure { "; Secure" } else { "" };
    let value = format!(
        "monitube_session={token}; HttpOnly; Max-Age={SESSION_MAX_AGE_SECONDS}; Path=/; SameSite=Lax{secure_attribute}"
    );
    HeaderValue::from_str(&value).map_err(AuthError::InvalidHeader)
}

fn session_token(headers: &HeaderMap) -> Option<&str> {
    headers
        .get(COOKIE)?
        .to_str()
        .ok()?
        .split(';')
        .filter_map(|part| part.trim().split_once('='))
        .find_map(|(name, value)| (name == "monitube_session").then_some(value))
        .filter(|value| !value.is_empty())
}

fn validate_login_request(request: &LoginRequest) -> Result<(String, String), AuthError> {
    let username = request.username.trim().to_owned();
    let password = request.password.trim().to_owned();
    let valid_username = (3..=32).contains(&username.len())
        && username
            .bytes()
            .all(|value| value.is_ascii_alphanumeric() || matches!(value, b'_' | b'-'));
    if !valid_username || !(8..=256).contains(&password.len()) {
        return Err(AuthError::InvalidLoginInput);
    }
    Ok((username, password))
}

fn hash_password(password: &str) -> Result<String, AuthError> {
    let mut salt = [0_u8; SALT_BYTES];
    getrandom::fill(&mut salt).map_err(AuthError::Random)?;
    let mut digest = [0_u8; 32];
    pbkdf2_hmac::<Sha256>(password.as_bytes(), &salt, PBKDF2_ITERATIONS, &mut digest);
    Ok(format!(
        "pbkdf2_sha256${PBKDF2_ITERATIONS}${}${}",
        hex::encode(salt),
        hex::encode(digest)
    ))
}

fn verify_password(password: &str, encoded: &str) -> bool {
    let mut components = encoded.split('$');
    let (Some(algorithm), Some(iterations), Some(salt), Some(expected), None) = (
        components.next(),
        components.next(),
        components.next(),
        components.next(),
        components.next(),
    ) else {
        return false;
    };
    if algorithm != "pbkdf2_sha256" {
        return false;
    }
    let Ok(iterations) = iterations.parse::<u32>() else {
        return false;
    };
    if !(100_000..=1_000_000).contains(&iterations) {
        return false;
    }
    let (Ok(salt), Ok(expected)) = (hex::decode(salt), hex::decode(expected)) else {
        return false;
    };
    if expected.len() != 32 {
        return false;
    }
    let mut candidate = [0_u8; 32];
    pbkdf2_hmac::<Sha256>(password.as_bytes(), &salt, iterations, &mut candidate);
    bool::from(candidate.as_slice().ct_eq(&expected))
}

fn token_hash(token: &str) -> String {
    hex::encode(Sha256::digest(token.as_bytes()))
}

fn is_unique_violation(error: &sqlx::Error) -> bool {
    error
        .as_database_error()
        .and_then(sqlx::error::DatabaseError::code)
        .is_some_and(|code| code == "23505")
}

#[derive(Debug, Error)]
pub enum AuthError {
    #[error("login input is invalid")]
    InvalidLoginInput,
    #[error("username is already in use")]
    UsernameConflict,
    #[error("credentials are invalid")]
    InvalidCredentials,
    #[error("login is required")]
    LoginRequired,
    #[error("database operation failed")]
    Database(#[from] sqlx::Error),
    #[error("password worker failed")]
    PasswordWorker(#[source] tokio::task::JoinError),
    #[error("secure random generation failed")]
    Random(#[source] getrandom::Error),
    #[error("response header construction failed")]
    InvalidHeader(#[source] axum::http::header::InvalidHeaderValue),
}

impl IntoResponse for AuthError {
    fn into_response(self) -> Response {
        let (status, detail) = match self {
            Self::InvalidLoginInput => (
                StatusCode::UNPROCESSABLE_ENTITY,
                "아이디 또는 비밀번호 형식이 올바르지 않습니다.",
            ),
            Self::UsernameConflict => (StatusCode::CONFLICT, "이미 사용 중인 아이디입니다."),
            Self::InvalidCredentials => (
                StatusCode::UNAUTHORIZED,
                "아이디 또는 비밀번호가 올바르지 않습니다.",
            ),
            Self::LoginRequired => (StatusCode::UNAUTHORIZED, "로그인이 필요합니다."),
            Self::Database(error) => {
                let failure = crate::db_error::classify(&error, "authentication");
                (failure.status, "계정 저장소를 사용할 수 없습니다.")
            }
            Self::PasswordWorker(_) | Self::Random(_) | Self::InvalidHeader(_) => (
                StatusCode::SERVICE_UNAVAILABLE,
                "계정 저장소를 사용할 수 없습니다.",
            ),
        };
        (status, Json(AuthErrorResponse { detail })).into_response()
    }
}

#[derive(Serialize)]
struct AuthErrorResponse {
    detail: &'static str,
}

#[cfg(test)]
mod tests {
    use super::{LoginRequest, hash_password, validate_login_request, verify_password};

    #[test]
    fn password_hash_round_trip_matches_python_format() -> Result<(), super::AuthError> {
        let encoded = hash_password("correct horse battery staple")?;
        assert!(encoded.starts_with("pbkdf2_sha256$310000$"));
        assert!(verify_password("correct horse battery staple", &encoded));
        assert!(!verify_password("wrong password", &encoded));
        Ok(())
    }

    #[test]
    fn login_contract_rejects_non_ascii_username() {
        let result = validate_login_request(&LoginRequest {
            username: "사용자".to_owned(),
            password: "long-enough-password".to_owned(),
        });
        assert!(result.is_err());
    }
}
