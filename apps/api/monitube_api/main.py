"""FastAPI application factory using server-managed YouTube credentials."""

from __future__ import annotations

from contextlib import asynccontextmanager
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.dependencies import get_current_user
from .api.exception_handlers import register_exception_handlers
from .api.routers.auth import create_auth_router
from .api.routers.comments import router as comments_router
from .api.routers.explore import router as explore_router
from .api.routers.jobs import router as jobs_router
from .api.routers.pins import create_pins_router
from .api.routers.resolutions import router as resolutions_router
from .api.routers.sources import create_sources_router
from .api.routers.system import create_system_router
from .auth import AuthStore
from .cache import DerivedCache
from .ports import CollectionRepository
from .services import CollectionService
from .settings import Settings, create_repository

__all__ = ["app", "create_app", "get_current_user"]


def create_app(
    repository: CollectionRepository | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    """Create and wire the HTTP application around one repository instance."""

    configured_settings = settings or Settings.from_environment()
    if repository is None:
        repository, runtime_config_id = create_repository(configured_settings)
    else:
        runtime_config_id = None

    derived_cache = DerivedCache(
        configured_settings.redis_url,
        enabled=configured_settings.enable_redis_derived_cache,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            yield
        finally:
            derived_cache.close()
            close = getattr(repository, "close", None)
            if close:
                close()

    app = FastAPI(title="Monitube API", version="0.1.0", lifespan=lifespan)
    app.state.collection_service = CollectionService(
        repository,
        runtime_config_id=runtime_config_id,
        derived_cache=derived_cache,
    )
    app.state.derived_cache = derived_cache
    app.state.repository = repository
    app.state.runtime_config_id = runtime_config_id
    app.state.settings = configured_settings
    app.state.auth_store = (
        AuthStore(
            configured_settings.database_url,
            pool=getattr(repository, "pool", None),
        )
        if configured_settings.database_url
        else None
    )

    cors_origins = [
        origin.strip()
        for origin in os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",")
        if origin.strip()
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "Accept", "Idempotency-Key"],
    )

    register_exception_handlers(app)
    app.include_router(
        create_system_router(
            repository=repository,
            derived_cache=derived_cache,
            settings=configured_settings,
            runtime_config_id=runtime_config_id,
        )
    )
    app.include_router(create_auth_router(configured_settings))
    app.include_router(resolutions_router)
    app.include_router(
        create_sources_router(
            repository=repository,
            settings=configured_settings,
        )
    )
    app.include_router(create_pins_router(repository))
    app.include_router(explore_router)
    app.include_router(jobs_router)
    app.include_router(comments_router)
    return app


app = create_app()
