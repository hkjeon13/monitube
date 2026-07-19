"""Health, readiness, and server-managed runtime routes."""

import secrets

from fastapi import APIRouter, Header, HTTPException, status

from ...cache import DerivedCache
from ...contracts import (
    HealthResponse,
    RuntimeKeyRegistration,
    RuntimeKeyRegistrationResponse,
)
from ...repositories import CollectionRepository, RepositoryUnavailableError
from ...settings import Settings


def create_system_router(
    *,
    repository: CollectionRepository,
    derived_cache: DerivedCache,
    settings: Settings,
    runtime_config_id: str | None,
) -> APIRouter:
    router = APIRouter()

    @router.get("/health", response_model=HealthResponse, tags=["health"])
    def health() -> HealthResponse:
        return HealthResponse()

    @router.get("/ready", tags=["health"])
    def ready() -> dict[str, object]:
        checker = getattr(repository, "check_readiness", None)
        try:
            checks = checker() if checker else {"repository": "in-memory"}
        except RepositoryUnavailableError:
            raise
        except Exception as exc:
            raise RepositoryUnavailableError(
                "Database readiness check failed"
            ) from exc
        if checks.get("migrationCurrent") is False:
            raise RepositoryUnavailableError(
                "Required database migration is not applied"
            )
        return {
            "status": "ready",
            "checks": {**checks, "derivedCache": derived_cache.health()},
        }

    @router.post(
        "/register/key",
        response_model=RuntimeKeyRegistrationResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["runtime"],
    )
    def register_runtime_keys(
        payload: RuntimeKeyRegistration,
        authorization: str | None = Header(default=None),
    ) -> RuntimeKeyRegistrationResponse:
        token = settings.youtube_key_registration_token
        expected = f"Bearer {token}" if token else ""
        if (
            not token
            or not authorization
            or not secrets.compare_digest(authorization, expected)
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized",
            )
        if (
            not settings.youtube_api_key_encryption_key
            or runtime_config_id is None
            or not hasattr(repository, "sync_runtime_keys")
        ):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Key registration is unavailable",
            )
        repository.sync_runtime_keys(
            runtime_config_id=runtime_config_id,
            api_keys=tuple(payload.apiKeys),
            encryption_key=settings.youtube_api_key_encryption_key,
        )
        return RuntimeKeyRegistrationResponse(accepted=len(payload.apiKeys))

    return router
