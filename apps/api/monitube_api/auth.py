"""Small PostgreSQL-backed password and session store for the browser console."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import secrets
from typing import Any, Iterator

try:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg_pool import PoolTimeout
except ImportError:  # pragma: no cover
    psycopg = None  # type: ignore[assignment]
    dict_row = None  # type: ignore[assignment]

    class PoolTimeout(Exception):
        pass

from .repositories import RepositoryUnavailableError


SESSION_DAYS = 90
SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * SESSION_DAYS
# Renew only once per month at most. The browser cookie itself is re-issued on
# authenticated requests, so active users retain the session without creating
# a database write for every API call.
SESSION_REFRESH_WINDOW_DAYS = 30
PBKDF2_ITERATIONS = 310_000


@dataclass(frozen=True, slots=True)
class AuthUser:
    id: str
    username: str


def _hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def _verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations))
        return hmac.compare_digest(candidate.hex(), digest_hex)
    except (TypeError, ValueError):
        return False


class AuthStore:
    def __init__(self, database_url: str, *, pool: Any | None = None) -> None:
        self.database_url = database_url
        self._pool = pool

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        try:
            if self._pool is not None:
                with self._pool.connection() as connection:
                    yield connection
                return
            if psycopg is None:
                raise RuntimeError("psycopg is required when DATABASE_URL is configured")
            with psycopg.connect(self.database_url, row_factory=dict_row) as connection:
                yield connection
        except PoolTimeout as exc:
            raise RepositoryUnavailableError(
                "Database connection pool is busy; retry shortly"
            ) from exc

    def register(self, username: str, password: str) -> AuthUser:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO app_users (username, password_hash) VALUES (%s, %s) RETURNING id::text, username",
                (username, _hash_password(password)),
            )
            row = cursor.fetchone()
            # Historical data is intentionally reserved for the established
            # ``psyche`` account.  Do not let the first later registration claim
            # all previously collected public data just because it happened to
            # arrive before that account.  New users create their own
            # subscriptions through the collection flow instead.
            if row["username"] == "psyche":
                cursor.execute("UPDATE collection_sources SET owner_id = %s WHERE owner_id IS NULL", (row["id"],))
                cursor.execute("UPDATE collection_targets SET owner_id = %s WHERE owner_id IS NULL", (row["id"],))
                # During a rolling upgrade the psyche account may be registered
                # after migration 010 ran. Backfill its claimed legacy rows here
                # so subscription-based Sources reads work immediately.  This is
                # idempotent and does not copy targets, videos, comments, or jobs.
                cursor.execute(
                    """
                    INSERT INTO collection_subscriptions (
                        user_id, target_id, display_config, enabled, created_at, updated_at
                    )
                    SELECT
                        %s,
                        source.target_id,
                        source.config,
                        source.enabled,
                        source.created_at,
                        source.updated_at
                    FROM collection_sources source
                    WHERE source.owner_id = %s
                      AND source.target_id IS NOT NULL
                    ON CONFLICT (user_id, target_id) DO NOTHING
                    """,
                    (row["id"], row["id"]),
                )
            return AuthUser(id=str(row["id"]), username=str(row["username"]))

    def authenticate(self, username: str, password: str) -> AuthUser | None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id::text, username, password_hash FROM app_users WHERE username = %s", (username,))
            row = cursor.fetchone()
            if not row or not _verify_password(password, str(row["password_hash"])):
                return None
            return AuthUser(id=str(row["id"]), username=str(row["username"]))

    def create_session(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        expires_at = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("INSERT INTO app_sessions (user_id, token_hash, expires_at) VALUES (%s, %s, %s)", (user_id, token_hash, expires_at))
        return token

    def user_for_session(self, token: str | None) -> AuthUser | None:
        if not token:
            return None
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT u.id::text, u.username FROM app_sessions s
                   JOIN app_users u ON u.id = s.user_id
                   WHERE s.token_hash = %s AND s.expires_at > now()""",
                (token_hash,),
            )
            row = cursor.fetchone()
            return AuthUser(id=str(row["id"]), username=str(row["username"])) if row else None

    def refresh_session(self, token: str | None) -> None:
        """Extend an active session when it is within its renewal window."""
        if not token:
            return
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """UPDATE app_sessions
                   SET expires_at = now() + (%s * interval '1 day')
                   WHERE token_hash = %s
                     AND expires_at > now()
                     AND expires_at < now() + (%s * interval '1 day')""",
                (SESSION_DAYS, token_hash, SESSION_REFRESH_WINDOW_DAYS),
            )

    def revoke_session(self, token: str | None) -> None:
        if not token:
            return
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM app_sessions WHERE token_hash = %s", (hashlib.sha256(token.encode("utf-8")).hexdigest(),))
