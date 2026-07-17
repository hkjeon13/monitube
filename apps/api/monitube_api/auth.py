"""Small PostgreSQL-backed password and session store for the browser console."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import secrets
from typing import Any

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover
    psycopg = None  # type: ignore[assignment]
    dict_row = None  # type: ignore[assignment]


SESSION_DAYS = 14
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
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    def _connection(self):
        if psycopg is None:
            raise RuntimeError("psycopg is required when DATABASE_URL is configured")
        return psycopg.connect(self.database_url, row_factory=dict_row)

    def register(self, username: str, password: str) -> AuthUser:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO app_users (username, password_hash) VALUES (%s, %s) RETURNING id::text, username",
                (username, _hash_password(password)),
            )
            row = cursor.fetchone()
            # Legacy rows have no owner until the first browser registration.
            # The WHERE clause makes the claim one-time and transactional.
            cursor.execute("UPDATE collection_sources SET owner_id = %s WHERE owner_id IS NULL", (row["id"],))
            cursor.execute("UPDATE collection_targets SET owner_id = %s WHERE owner_id IS NULL", (row["id"],))
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

    def revoke_session(self, token: str | None) -> None:
        if not token:
            return
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM app_sessions WHERE token_hash = %s", (hashlib.sha256(token.encode("utf-8")).hexdigest(),))
