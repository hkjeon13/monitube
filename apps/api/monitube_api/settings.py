"""Runtime configuration for server-managed YouTube access and persistence."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from typing import Mapping


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str | None
    youtube_api_key: str | None
    youtube_api_keys: tuple[str, ...]
    youtube_api_key_encryption_key: str | None
    youtube_key_registration_token: str | None
    youtube_api_base_url: str
    youtube_api_timeout_seconds: float
    youtube_api_secret_ref: str
    youtube_google_project_number: str
    environment: str
    worker_poll_seconds: float
    worker_lease_seconds: int

    @property
    def key_fingerprint(self) -> str | None:
        if not self.youtube_api_key:
            return None
        return hashlib.sha256(self.youtube_api_key.encode("utf-8")).hexdigest()[:24]

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> "Settings":
        values = environment or os.environ

        def optional(name: str) -> str | None:
            value = values.get(name, "").strip()
            return value or None

        raw_keys = values.get("YOUTUBE_API_KEYS", "")
        keys = tuple(dict.fromkeys(key.strip() for key in raw_keys.replace("\n", ",").split(",") if key.strip()))
        legacy_key = optional("YOUTUBE_API_KEY")
        if legacy_key and legacy_key not in keys:
            keys = (*keys, legacy_key)

        database_url = optional("DATABASE_URL")
        # SQLAlchemy-style URLs are common in existing compose files; psycopg itself
        # expects the plain PostgreSQL scheme.
        if database_url and database_url.startswith("postgresql+psycopg://"):
            database_url = "postgresql://" + database_url.removeprefix("postgresql+psycopg://")

        def positive_float(name: str, default: float) -> float:
            try:
                value = float(values.get(name, default))
            except (TypeError, ValueError):
                return default
            return value if value > 0 else default

        def positive_int(name: str, default: int) -> int:
            try:
                value = int(values.get(name, default))
            except (TypeError, ValueError):
                return default
            return value if value > 0 else default

        return cls(
            database_url=database_url,
            youtube_api_key=keys[0] if keys else None,
            youtube_api_keys=keys,
            youtube_api_key_encryption_key=optional("YOUTUBE_API_KEY_ENCRYPTION_KEY"),
            youtube_key_registration_token=optional("YOUTUBE_KEY_REGISTRATION_TOKEN"),
            youtube_api_base_url=(values.get("YOUTUBE_API_BASE_URL", "").strip() or "https://www.googleapis.com/youtube/v3").rstrip("/"),
            youtube_api_timeout_seconds=positive_float("YOUTUBE_API_TIMEOUT_SECONDS", 20.0),
            youtube_api_secret_ref=values.get("YOUTUBE_API_KEY_SECRET_REF", "env:YOUTUBE_API_KEY").strip() or "env:YOUTUBE_API_KEY",
            youtube_google_project_number=values.get("YOUTUBE_GOOGLE_PROJECT_NUMBER", "server-managed").strip() or "server-managed",
            environment=values.get("APP_ENV", "development").strip() or "development",
            worker_poll_seconds=positive_float("WORKER_POLL_SECONDS", 3.0),
            worker_lease_seconds=positive_int("WORKER_LEASE_SECONDS", 120),
        )


def create_repository(settings: Settings):
    """Build the configured repository and persist only the managed-secret reference."""

    from .postgres_repository import PostgresRepository
    from .repositories import InMemoryRepository

    repository = PostgresRepository(settings.database_url) if settings.database_url else InMemoryRepository()
    runtime_config_id = repository.bootstrap_runtime_config(
        environment=settings.environment,
        google_project_number=settings.youtube_google_project_number,
        secret_ref=settings.youtube_api_secret_ref,
        key_fingerprint=settings.key_fingerprint,
    )
    if settings.youtube_api_keys and settings.youtube_api_key_encryption_key and hasattr(repository, "sync_runtime_keys"):
        repository.sync_runtime_keys(
            runtime_config_id=runtime_config_id,
            api_keys=settings.youtube_api_keys,
            encryption_key=settings.youtube_api_key_encryption_key,
        )
    return repository, runtime_config_id
