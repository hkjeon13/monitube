"""FastAPI application factory using server-managed YouTube credentials."""

from __future__ import annotations

import os
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Header, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .channel_resolution import ChannelInputError
from .contracts import (
    ChannelResolutionRequest,
    ChannelResolutionResponse,
    CollectionSource,
    CollectionSourceCreate,
    CollectionSourceUpdate,
    CollectionRequestCreate,
    CollectionRequestResponse,
    HealthResponse,
    JobCreate,
    JobStatus,
    SourceResultsResponse,
    VideoCommentsResponse,
    VideoResolutionRequest,
    VideoResolutionResponse,
)
from .repositories import CollectionRepository, InMemoryRepository, InvalidStateTransitionError, NotFoundError, RepositoryError
from .settings import Settings, create_repository
from .services import CollectionService
from .video_resolution import VideoInputError


def get_service(request: Request) -> CollectionService:
    return request.app.state.collection_service


Service = Annotated[CollectionService, Depends(get_service)]


def create_app(repository: CollectionRepository | None = None, settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="Monitube API", version="0.1.0")
    configured_settings = settings or Settings.from_environment()
    if repository is None:
        repository, runtime_config_id = create_repository(configured_settings)
    else:
        runtime_config_id = None
    app.state.collection_service = CollectionService(repository, runtime_config_id=runtime_config_id)
    app.state.settings = configured_settings
    cors_origins = [
        origin.strip()
        for origin in os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",")
        if origin.strip()
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "Accept", "Idempotency-Key"],
    )

    @app.exception_handler(NotFoundError)
    async def not_found_handler(_: Request, exc: NotFoundError) -> Response:
        return JSONResponse(content={"detail": str(exc)}, status_code=status.HTTP_404_NOT_FOUND)

    @app.exception_handler(ChannelInputError)
    async def channel_input_handler(_: Request, exc: ChannelInputError) -> Response:
        return JSONResponse(content={"detail": str(exc)}, status_code=status.HTTP_422_UNPROCESSABLE_ENTITY)

    @app.exception_handler(VideoInputError)
    async def video_input_handler(_: Request, exc: VideoInputError) -> Response:
        return JSONResponse(content={"detail": str(exc)}, status_code=status.HTTP_422_UNPROCESSABLE_ENTITY)

    @app.exception_handler(ValidationError)
    async def validation_handler(_: Request, exc: ValidationError) -> Response:
        return JSONResponse(content={"detail": "Invalid source configuration", "errors": exc.errors()}, status_code=status.HTTP_422_UNPROCESSABLE_ENTITY)

    @app.exception_handler(InvalidStateTransitionError)
    async def state_transition_handler(_: Request, exc: InvalidStateTransitionError) -> Response:
        return JSONResponse(content={"detail": str(exc)}, status_code=status.HTTP_409_CONFLICT)

    @app.exception_handler(RepositoryError)
    async def repository_handler(_: Request, exc: RepositoryError) -> Response:
        return JSONResponse(content={"detail": str(exc)}, status_code=status.HTTP_409_CONFLICT)

    @app.get("/health", response_model=HealthResponse, tags=["health"])
    def health() -> HealthResponse:
        return HealthResponse()

    router = APIRouter(prefix="/v1")

    @router.post("/channel-resolutions", response_model=ChannelResolutionResponse, tags=["channels"])
    def resolve_channel(payload: ChannelResolutionRequest, service: Service) -> ChannelResolutionResponse:
        return service.resolve_channel(payload.input)

    @router.post("/video-resolutions", response_model=VideoResolutionResponse, tags=["videos"])
    def resolve_video(payload: VideoResolutionRequest) -> VideoResolutionResponse:
        from .video_resolution import resolve_video_input

        resolution = resolve_video_input(payload.input)
        return VideoResolutionResponse(kind=resolution.kind.value, normalized=resolution.normalized)

    @router.post(
        "/sources",
        response_model=CollectionSource,
        status_code=status.HTTP_201_CREATED,
        tags=["sources"],
    )
    def create_source(payload: CollectionSourceCreate, service: Service) -> CollectionSource:
        return service.create_source(payload)

    @router.post(
        "/collection-requests",
        response_model=CollectionRequestResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["collection"],
    )
    def submit_collection_request(
        payload: CollectionRequestCreate,
        service: Service,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255),
    ) -> CollectionRequestResponse:
        """Create or join a shared target collection job in one atomic command."""

        return service.submit_collection_request(payload, idempotency_key=idempotency_key)

    @router.get("/sources", response_model=list[CollectionSource], tags=["sources"])
    def list_sources(service: Service) -> list[CollectionSource]:
        return service.list_sources()

    @router.get("/sources/{source_id}", response_model=CollectionSource, tags=["sources"])
    def get_source(source_id: str, service: Service) -> CollectionSource:
        return service.get_source(source_id)

    @router.get("/sources/{source_id}/results", response_model=SourceResultsResponse, tags=["results"])
    def get_source_results(source_id: str, service: Service) -> SourceResultsResponse:
        return service.get_source_results(source_id)

    @router.patch("/sources/{source_id}", response_model=CollectionSource, tags=["sources"])
    def update_source(source_id: str, payload: CollectionSourceUpdate, service: Service) -> CollectionSource:
        return service.update_source(source_id, payload)

    @router.delete("/sources/{source_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["sources"])
    def delete_source(source_id: str, service: Service) -> Response:
        service.delete_source(source_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post(
        "/sources/{source_id}/jobs",
        response_model=JobStatus,
        status_code=status.HTTP_201_CREATED,
        tags=["jobs"],
    )
    def create_job(source_id: str, payload: JobCreate, service: Service) -> JobStatus:
        return service.create_job(source_id, payload)

    @router.get("/jobs/{job_id}", response_model=JobStatus, tags=["jobs"])
    def get_job(job_id: str, service: Service) -> JobStatus:
        return service.get_job(job_id)

    @router.get("/videos/{video_id}/comments", response_model=VideoCommentsResponse, tags=["results"])
    def get_video_comments(video_id: str, service: Service) -> VideoCommentsResponse:
        return service.get_video_comments(video_id)

    app.include_router(router)
    return app


app = create_app()
