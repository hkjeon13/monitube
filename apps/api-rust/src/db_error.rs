//! Consistent public classification for database failures.

use axum::http::StatusCode;

pub(crate) struct DatabaseFailure {
    pub status: StatusCode,
    pub detail: &'static str,
    pub retryable: bool,
}

pub(crate) fn classify(error: &sqlx::Error, operation: &'static str) -> DatabaseFailure {
    let retryable = matches!(
        error,
        sqlx::Error::PoolTimedOut | sqlx::Error::PoolClosed | sqlx::Error::Io(_)
    ) || matches!(
        error,
        sqlx::Error::Database(database)
            if database.code().as_deref().is_some_and(is_retryable_database_code)
    );

    if retryable {
        tracing::warn!(%error, operation, "retryable database operation failed");
        DatabaseFailure {
            status: StatusCode::SERVICE_UNAVAILABLE,
            detail: "Database is temporarily unavailable; retry shortly",
            retryable: true,
        }
    } else {
        tracing::error!(%error, operation, "non-retryable database operation failed");
        DatabaseFailure {
            status: StatusCode::INTERNAL_SERVER_ERROR,
            detail: "Database operation failed",
            retryable: false,
        }
    }
}

fn is_retryable_database_code(code: &str) -> bool {
    matches!(
        code,
        // serialization_failure, deadlock_detected, lock_not_available,
        // cannot_connect_now, admin_shutdown, too_many_connections
        "40001" | "40P01" | "55P03" | "57P03" | "57P01" | "53300"
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pool_timeout_is_retryable() {
        let failure = classify(&sqlx::Error::PoolTimedOut, "test");
        assert_eq!(failure.status, StatusCode::SERVICE_UNAVAILABLE);
        assert!(failure.retryable);
    }

    #[test]
    fn programming_failure_is_not_reported_as_pool_pressure() {
        let failure = classify(&sqlx::Error::RowNotFound, "test");
        assert_eq!(failure.status, StatusCode::INTERNAL_SERVER_ERROR);
        assert!(!failure.retryable);
    }
}
