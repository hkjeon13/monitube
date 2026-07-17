"""FastAPI application factory using server-managed YouTube credentials."""

from __future__ import annotations

import os
import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .channel_resolution import ChannelInputError
from .auth import AuthStore, AuthUser, SESSION_MAX_AGE_SECONDS
from .contracts import (
    AuthUserResponse,
    ChannelResolutionRequest,
    ChannelResolutionResponse,
    ChannelSubscriberSnapshot,
    CollectionSource,
    CollectionSourceCreate,
    CollectionSourceUpdate,
    CollectionRequestCreate,
    CollectionRequestResponse,
    ExploreResponse,
    HealthResponse,
    LoginRequest,
    RuntimeKeyRegistration,
    RuntimeKeyRegistrationResponse,
    JobCreate,
    JobStatus,
    SourceResultsResponse,
    UnifiedSearchResponse,
    TargetPin,
    TargetPinUpdate,
    VideoCommentsResponse,
    CommentDetailResponse,
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


def set_session_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        "monitube_session",
        token,
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=settings.environment != "development",
        path="/",
    )


def get_current_user(request: Request, response: Response) -> AuthUser:
    """Require a browser session in PostgreSQL deployments.

    The in-memory repository remains open for the existing unit-test harness.
    """
    auth_store: AuthStore | None = request.app.state.auth_store
    if auth_store is None:
        return AuthUser(id="in-memory", username="psyche")
    token = request.cookies.get("monitube_session")
    user = auth_store.user_for_session(token)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="로그인이 필요합니다.")
    auth_store.refresh_session(token)
    set_session_cookie(response, token, request.app.state.settings)
    return user


User = Annotated[AuthUser, Depends(get_current_user)]


def create_app(repository: CollectionRepository | None = None, settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="Monitube API", version="0.1.0")
    configured_settings = settings or Settings.from_environment()
    if repository is None:
        repository, runtime_config_id = create_repository(configured_settings)
    else:
        runtime_config_id = None
    app.state.collection_service = CollectionService(repository, runtime_config_id=runtime_config_id)
    app.state.repository = repository
    app.state.runtime_config_id = runtime_config_id
    app.state.settings = configured_settings
    app.state.auth_store = AuthStore(configured_settings.database_url) if configured_settings.database_url else None
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

    @app.post("/register/key", response_model=RuntimeKeyRegistrationResponse, status_code=status.HTTP_201_CREATED, tags=["runtime"])
    def register_runtime_keys(
        payload: RuntimeKeyRegistration,
        authorization: str | None = Header(default=None),
    ) -> RuntimeKeyRegistrationResponse:
        token = configured_settings.youtube_key_registration_token
        expected = f"Bearer {token}" if token else ""
        if not token or not authorization or not secrets.compare_digest(authorization, expected):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
        if not configured_settings.youtube_api_key_encryption_key or runtime_config_id is None or not hasattr(repository, "sync_runtime_keys"):
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Key registration is unavailable")
        repository.sync_runtime_keys(
            runtime_config_id=runtime_config_id,
            api_keys=tuple(payload.apiKeys),
            encryption_key=configured_settings.youtube_api_key_encryption_key,
        )
        return RuntimeKeyRegistrationResponse(accepted=len(payload.apiKeys))

    auth_router = APIRouter(prefix="/v1/auth", tags=["auth"])

    @auth_router.get("/me", response_model=AuthUserResponse)
    def current_user(user: User) -> AuthUserResponse:
        return AuthUserResponse(username=user.username)

    @auth_router.post("/register", response_model=AuthUserResponse, status_code=status.HTTP_201_CREATED)
    def register(payload: LoginRequest, response: Response) -> AuthUserResponse:
        if app.state.auth_store is None:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="계정 저장소를 사용할 수 없습니다.")
        try:
            user = app.state.auth_store.register(payload.username, payload.password)
        except Exception as exc:
            if "unique" in str(exc).lower():
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="이미 사용 중인 아이디입니다.") from exc
            raise
        token = app.state.auth_store.create_session(user.id)
        set_session_cookie(response, token, configured_settings)
        return AuthUserResponse(username=user.username)

    @auth_router.post("/login", response_model=AuthUserResponse)
    def login(payload: LoginRequest, response: Response) -> AuthUserResponse:
        if app.state.auth_store is None:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="계정 저장소를 사용할 수 없습니다.")
        user = app.state.auth_store.authenticate(payload.username, payload.password)
        if not user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="아이디 또는 비밀번호가 올바르지 않습니다.")
        token = app.state.auth_store.create_session(user.id)
        set_session_cookie(response, token, configured_settings)
        return AuthUserResponse(username=user.username)

    @auth_router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
    def logout(request: Request, response: Response) -> Response:
        if app.state.auth_store is not None:
            app.state.auth_store.revoke_session(request.cookies.get("monitube_session"))
        response.delete_cookie("monitube_session", path="/")
        response.status_code = status.HTTP_204_NO_CONTENT
        return response

    router = APIRouter(prefix="/v1", dependencies=[Depends(get_current_user)])

    def require_source_owner(source_id: str, user: AuthUser) -> None:
        owns_source = getattr(repository, "source_owned_by", None)
        if owns_source and not owns_source(source_id=source_id, owner_id=user.id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source was not found")

    def owns_collection_data(user: AuthUser) -> bool:
        has_sources = getattr(repository, "owner_has_sources", None)
        return bool(has_sources(owner_id=user.id)) if has_sources else True

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
    def create_source(payload: CollectionSourceCreate, service: Service, user: User) -> CollectionSource:
        return service.create_source(payload, owner_id=user.id)

    @router.post(
        "/collection-requests",
        response_model=CollectionRequestResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["collection"],
    )
    def submit_collection_request(
        payload: CollectionRequestCreate,
        service: Service,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255), user: User = None,
    ) -> CollectionRequestResponse:
        """Create or join a shared target collection job in one atomic command."""

        submission = service.submit_collection_request(payload, idempotency_key=idempotency_key)
        assign_owner = getattr(repository, "assign_source_owner", None)
        if assign_owner:
            assign_owner(source_id=submission.source.id, owner_id=user.id)
        return submission

    @router.get("/sources", response_model=list[CollectionSource], tags=["sources"])
    def list_sources(service: Service, user: User) -> list[CollectionSource]:
        return service.list_sources(owner_id=user.id)

    @router.get("/explore", response_model=ExploreResponse, tags=["explore"])
    def explore(
        service: Service, user: User,
        channel_id: str | None = Query(default=None, alias="channelId", min_length=1, max_length=64),
    ) -> ExploreResponse:
        if not owns_collection_data(user):
            # Explore is populated by source results. An account that has not
            # claimed or collected any source has no data to render yet.
            return ExploreResponse(channels=[], videos=[])
        return service.explore(channel_id=channel_id)

    @router.get("/channels/{youtube_channel_id}/subscriber-history", response_model=list[ChannelSubscriberSnapshot], tags=["explore"])
    def channel_subscriber_history(youtube_channel_id: str, service: Service, user: User) -> list[ChannelSubscriberSnapshot]:
        if not owns_collection_data(user):
            return []
        return service.channel_subscriber_history(youtube_channel_id)

    @router.get("/search", response_model=UnifiedSearchResponse, tags=["search"])
    def search_collected(
        service: Service,
        q: str = Query(min_length=2, max_length=200),
        limit: int = Query(default=20, ge=1, le=50), user: User = None,
    ) -> UnifiedSearchResponse:
        if not owns_collection_data(user):
            return UnifiedSearchResponse(query=q, videos=[], comments=[])
        return service.search_collected(q, limit=limit)

    @router.put("/collection-targets/{target_id}/pin", response_model=TargetPin, tags=["pins"])
    def set_target_pin(target_id: str, payload: TargetPinUpdate, service: Service) -> TargetPin:
        return service.set_target_pin(target_id, payload)

    @router.get("/collection-targets/{target_id}/pin", response_model=TargetPin | None, tags=["pins"])
    def get_target_pin(target_id: str, service: Service) -> TargetPin | None:
        return service.get_target_pin(target_id)

    @router.get("/sources/{source_id}", response_model=CollectionSource, tags=["sources"])
    def get_source(source_id: str, service: Service, user: User) -> CollectionSource:
        require_source_owner(source_id, user)
        return service.get_source(source_id)

    @router.get("/sources/{source_id}/results", response_model=SourceResultsResponse, tags=["results"])
    def get_source_results(source_id: str, service: Service, user: User) -> SourceResultsResponse:
        require_source_owner(source_id, user)
        return service.get_source_results(source_id)

    @router.get("/sources/{source_id}/jobs", response_model=list[JobStatus], tags=["jobs"])
    def list_source_jobs(source_id: str, service: Service, user: User, limit: int = Query(default=20, ge=1, le=50)) -> list[JobStatus]:
        require_source_owner(source_id, user)
        return service.list_source_jobs(source_id, limit=limit)

    @router.patch("/sources/{source_id}", response_model=CollectionSource, tags=["sources"])
    def update_source(source_id: str, payload: CollectionSourceUpdate, service: Service, user: User) -> CollectionSource:
        require_source_owner(source_id, user)
        return service.update_source(source_id, payload)

    @router.delete("/sources/{source_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["sources"])
    def delete_source(source_id: str, service: Service, user: User) -> Response:
        require_source_owner(source_id, user)
        service.delete_source(source_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post(
        "/sources/{source_id}/jobs",
        response_model=JobStatus,
        status_code=status.HTTP_201_CREATED,
        tags=["jobs"],
    )
    def create_job(source_id: str, payload: JobCreate, service: Service, user: User) -> JobStatus:
        require_source_owner(source_id, user)
        return service.create_job(source_id, payload)

    @router.get("/jobs/{job_id}", response_model=JobStatus, tags=["jobs"])
    def get_job(job_id: str, service: Service, user: User) -> JobStatus:
        if not owns_collection_data(user):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job was not found")
        return service.get_job(job_id)

    @router.get("/videos/{video_id}/comments", response_model=VideoCommentsResponse, tags=["results"])
    def get_video_comments(video_id: str, service: Service, user: User) -> VideoCommentsResponse:
        if not owns_collection_data(user):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video was not found")
        return service.get_video_comments(video_id)

    @router.get("/comments/{comment_id}", response_model=CommentDetailResponse, tags=["results"])
    def get_comment_detail(comment_id: str, service: Service, user: User) -> CommentDetailResponse:
        if not owns_collection_data(user):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Comment was not found")
        return service.get_comment_detail(comment_id)

    app.include_router(auth_router)
    app.include_router(router)
    return app


app = create_app()
