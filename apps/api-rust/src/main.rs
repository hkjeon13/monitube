mod analysis;
mod auth;
mod collection;
mod comments;
mod config;
mod db_error;
mod explore;
mod jobs;
mod overview;
mod resolution;
mod runtime_keys;
mod search;
mod sources;
mod transcripts;
mod workspace_analysis;

use auth::AuthService;
use axum::extract::State;
use axum::http::header::{ACCEPT, CONTENT_TYPE};
use axum::http::{HeaderName, Method, Request, StatusCode};
use axum::middleware::{self, Next};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use config::AppConfig;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sqlx::PgPool;
use std::error::Error;
use tower_http::catch_panic::CatchPanicLayer;
use tower_http::cors::{AllowOrigin, CorsLayer};
use tower_http::limit::RequestBodyLimitLayer;
use tower_http::timeout::TimeoutLayer;
use tower_http::trace::TraceLayer;

#[derive(Clone)]
pub(crate) struct AppState {
    pool: PgPool,
    auth: AuthService,
    secure_cookies: bool,
    maintenance_read_only: bool,
    youtube_api_key_encryption_key: Option<String>,
    youtube_key_registration_token: Option<String>,
    tokenizer_client: reqwest::Client,
    tokenizer_ready_url: Option<String>,
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn Error>> {
    monitube_observability::init("monitube-api-rust")?;
    let config = AppConfig::from_environment()?;
    let pool = monitube_postgres::connect(&config.database_url, config.pool).await?;
    let tokenizer_client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(3))
        .build()?;
    let tokenizer_ready_url = config
        .enable_transcript_search
        .then(|| format!("{}/ready", config.tokenizer_base_url));
    let app = build_router(
        AppState {
            auth: AuthService::new(pool.clone()),
            pool,
            secure_cookies: config.secure_cookies,
            maintenance_read_only: config.maintenance_read_only,
            youtube_api_key_encryption_key: config.youtube_api_key_encryption_key,
            youtube_key_registration_token: config.youtube_key_registration_token,
            tokenizer_client,
            tokenizer_ready_url,
        },
        CorsLayer::new()
            .allow_origin(AllowOrigin::list(config.cors_origins))
            .allow_credentials(true)
            .allow_methods([
                Method::GET,
                Method::POST,
                Method::PUT,
                Method::PATCH,
                Method::DELETE,
            ])
            .allow_headers([
                ACCEPT,
                CONTENT_TYPE,
                HeaderName::from_static("idempotency-key"),
            ]),
        config.request_timeout,
    );
    let listener = tokio::net::TcpListener::bind(config.listen_address).await?;

    tracing::info!(address = %config.listen_address, "Rust API foundation listening");
    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await?;
    Ok(())
}

#[allow(clippy::too_many_lines)]
fn build_router(state: AppState, cors: CorsLayer, request_timeout: std::time::Duration) -> Router {
    let protected = Router::new()
        .route("/v1/auth/me", get(auth::me))
        .route("/v1/channel-resolutions", post(resolution::resolve_channel))
        .route("/v1/video-resolutions", post(resolution::resolve_video))
        .route(
            "/v1/videos/{video_id}/comments",
            get(comments::get_video_comments),
        )
        .route(
            "/v1/videos/{video_id}/comment-threads",
            get(comments::get_video_comment_threads),
        )
        .route(
            "/v1/comments/{comment_id}/replies",
            get(comments::get_comment_replies),
        )
        .route(
            "/v1/comments/{comment_id}",
            get(comments::get_comment_detail),
        )
        .route(
            "/v1/collection-targets/{target_id}/pin",
            get(explore::get_target_pin).put(explore::set_target_pin),
        )
        .route(
            "/v1/channels/{youtube_channel_id}/subscriber-history",
            get(explore::subscriber_history),
        )
        .route("/v1/explore", get(explore::explore))
        .route("/v1/explore/channels", get(explore::list_channels))
        .route("/v1/explore/videos", get(explore::list_videos))
        .route("/v1/search", get(search::search))
        .route(
            "/v1/analysis/excluded-terms",
            get(analysis::list_excluded_terms),
        )
        .route(
            "/v1/analysis/excluded-terms/{corpus_kind}",
            axum::routing::put(analysis::replace_excluded_terms),
        )
        .route("/v1/analysis/overview", get(workspace_analysis::overview))
        .route("/v1/analysis/insights", get(workspace_analysis::insights))
        .route(
            "/v1/sources",
            get(sources::list_sources).post(collection::create_source),
        )
        .route("/v1/collection-requests", post(collection::submit_request))
        .route(
            "/v1/sources/{source_id}",
            get(sources::get_source)
                .patch(sources::update_source)
                .delete(sources::delete_source),
        )
        .route(
            "/v1/sources/{source_id}/videos",
            get(sources::list_source_videos),
        )
        .route(
            "/v1/sources/{source_id}/overview",
            get(overview::get_source_overview),
        )
        .route(
            "/v1/sources/{source_id}/results",
            get(overview::get_source_results),
        )
        .route(
            "/v1/sources/{source_id}/refresh",
            post(collection::refresh_source),
        )
        .route(
            "/v1/videos/{youtube_video_id}/transcript",
            get(transcripts::get_video_transcript),
        )
        .route(
            "/v1/sources/{source_id}/jobs",
            get(jobs::list_source_jobs).post(jobs::create_job),
        )
        .route("/v1/jobs/active", get(jobs::list_active_jobs))
        .route("/v1/jobs/recent-failures", get(jobs::list_recent_failures))
        .route("/v1/jobs/{job_id}", get(jobs::get_job))
        .route_layer(middleware::from_fn_with_state(
            state.clone(),
            auth::require_session,
        ));

    Router::new()
        .route("/health", get(health))
        .route("/ready", get(ready))
        .route("/register/key", post(runtime_keys::register))
        .route("/v1/auth/register", post(auth::register))
        .route("/v1/auth/login", post(auth::login))
        .route("/v1/auth/logout", post(auth::logout))
        .merge(protected)
        .layer(RequestBodyLimitLayer::new(1024 * 1024))
        .layer(TimeoutLayer::with_status_code(
            StatusCode::REQUEST_TIMEOUT,
            request_timeout,
        ))
        .layer(CatchPanicLayer::new())
        .layer(TraceLayer::new_for_http())
        .layer(cors)
        .layer(middleware::from_fn_with_state(
            state.clone(),
            reject_mutating_requests,
        ))
        .with_state(state)
}

async fn reject_mutating_requests(
    State(state): State<AppState>,
    request: Request<axum::body::Body>,
    next: Next,
) -> Response {
    if state.maintenance_read_only && is_mutating_method(request.method()) {
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({
                "error": "maintenance_read_only",
                "message": "Writes are temporarily disabled for database maintenance"
            })),
        )
            .into_response();
    }
    next.run(request).await
}

const fn is_mutating_method(method: &Method) -> bool {
    matches!(
        *method,
        Method::POST | Method::PUT | Method::PATCH | Method::DELETE
    )
}

async fn health() -> Json<HealthResponse> {
    Json(HealthResponse {
        status: "ok",
        service: "monitube-api",
    })
}

async fn ready(State(state): State<AppState>) -> Result<Json<Value>, ApiError> {
    match monitube_postgres::check_required_schema(&state.pool).await {
        Ok(true) => {}
        Ok(false) => {
            return Err(ApiError::Unavailable(
                "Required database migration is not applied",
            ));
        }
        Err(error) => {
            tracing::warn!(error = %error, "Rust API readiness database check failed");
            return Err(ApiError::Unavailable("Database readiness check failed"));
        }
    }

    let mut checks = json!({
        "database": "ok",
        "migrationCurrent": true,
        "pool": "enabled",
        "maintenance": {"readOnly": state.maintenance_read_only},
        "poolStats": {
            "pool_size": state.pool.size(),
            "pool_available": state.pool.num_idle(),
            "requests_waiting": 0,
            "requests_errors": 0
        },
        "derivedCache": {"enabled": false, "status": "disabled"}
    });
    if let Some(url) = &state.tokenizer_ready_url {
        let response =
            state.tokenizer_client.get(url).send().await.map_err(|_| {
                ApiError::Unavailable("Required MeCab/NLTK analyzer is unavailable")
            })?;
        if !response.status().is_success() {
            return Err(ApiError::Unavailable(
                "Required MeCab/NLTK analyzer is unavailable",
            ));
        }
        let payload = response
            .json::<TokenizerReady>()
            .await
            .map_err(|_| ApiError::Unavailable("Required MeCab/NLTK analyzer is unavailable"))?;
        if payload.status != "ready" || payload.checks.analyzer.status != "ok" {
            return Err(ApiError::Unavailable(
                "Required MeCab/NLTK analyzer is unavailable",
            ));
        }
        checks["nounAnalyzer"] = serde_json::to_value(payload.checks.analyzer)
            .map_err(|_| ApiError::Unavailable("Required MeCab/NLTK analyzer is unavailable"))?;
    }
    Ok(Json(json!({"status": "ready", "checks": checks})))
}

async fn shutdown_signal() {
    let ctrl_c = async {
        if let Err(error) = tokio::signal::ctrl_c().await {
            tracing::error!(error = %error, "could not install Ctrl-C handler");
            std::future::pending::<()>().await;
        }
    };

    #[cfg(unix)]
    let terminate = async {
        match tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()) {
            Ok(mut signal) => {
                signal.recv().await;
            }
            Err(error) => {
                tracing::error!(error = %error, "could not install SIGTERM handler");
                std::future::pending::<()>().await;
            }
        }
    };

    #[cfg(unix)]
    tokio::select! {
        () = ctrl_c => {},
        () = terminate => {},
    }

    #[cfg(not(unix))]
    ctrl_c.await;

    tracing::info!("shutdown signal received");
}

#[derive(Serialize)]
struct HealthResponse {
    status: &'static str,
    service: &'static str,
}

#[derive(Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
struct AnalyzerReadiness {
    status: String,
    version: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    fixture_hash: Option<String>,
}

#[derive(Deserialize)]
struct TokenizerChecks {
    analyzer: AnalyzerReadiness,
}

#[derive(Deserialize)]
struct TokenizerReady {
    status: String,
    checks: TokenizerChecks,
}

enum ApiError {
    Unavailable(&'static str),
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        match self {
            Self::Unavailable(detail) => (
                StatusCode::SERVICE_UNAVAILABLE,
                Json(ErrorResponse {
                    detail,
                    retryable: true,
                }),
            )
                .into_response(),
        }
    }
}

#[derive(Serialize)]
struct ErrorResponse {
    detail: &'static str,
    #[serde(skip_serializing_if = "crate::is_false")]
    retryable: bool,
}

#[allow(clippy::trivially_copy_pass_by_ref)]
pub(crate) const fn is_false(value: &bool) -> bool {
    !*value
}

#[cfg(test)]
mod tests {
    use super::{health, is_mutating_method};
    use axum::http::Method;

    #[tokio::test]
    async fn health_contract_matches_python_api() {
        let response = health().await;
        assert_eq!(response.status, "ok");
        assert_eq!(response.service, "monitube-api");
    }

    #[test]
    fn maintenance_fence_only_rejects_mutating_methods() {
        for method in [Method::POST, Method::PUT, Method::PATCH, Method::DELETE] {
            assert!(is_mutating_method(&method));
        }
        for method in [Method::GET, Method::HEAD, Method::OPTIONS] {
            assert!(!is_mutating_method(&method));
        }
    }
}
