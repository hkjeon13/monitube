"""Collection source commands and source-scoped read routes."""

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status

from ...contracts import (
    CollectionRequestCreate,
    CollectionRequestResponse,
    CollectionSource,
    CollectionSourceCreate,
    CollectionSourceUpdate,
    JobCreate,
    JobStatus,
    SourceOverviewResponse,
    SourceResultsResponse,
    SourceVideosPageResponse,
)
from ...repositories import CollectionRepository
from ...settings import Settings
from ..authorization import require_source_owner
from ..dependencies import Service, User, get_current_user


def create_sources_router(
    *,
    repository: CollectionRepository,
    settings: Settings,
) -> APIRouter:
    router = APIRouter(prefix="/v1", dependencies=[Depends(get_current_user)])

    @router.post(
        "/sources",
        response_model=CollectionSource,
        status_code=status.HTTP_201_CREATED,
        tags=["sources"],
    )
    def create_source(
        payload: CollectionSourceCreate,
        service: Service,
        user: User,
    ) -> CollectionSource:
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
        user: User,
        idempotency_key: str | None = Header(
            default=None,
            alias="Idempotency-Key",
            max_length=255,
        ),
    ) -> CollectionRequestResponse:
        """Create or join a shared target collection job atomically."""

        return service.submit_collection_request(
            payload,
            owner_id=user.id,
            idempotency_key=idempotency_key,
        )

    @router.get("/sources", response_model=list[CollectionSource], tags=["sources"])
    def list_sources(service: Service, user: User) -> list[CollectionSource]:
        return service.list_sources(owner_id=user.id)

    @router.get(
        "/sources/{source_id}",
        response_model=CollectionSource,
        tags=["sources"],
    )
    def get_source(
        source_id: str,
        service: Service,
        user: User,
    ) -> CollectionSource:
        require_source_owner(repository, source_id=source_id, user=user)
        return service.get_source(source_id, owner_id=user.id)

    @router.get(
        "/sources/{source_id}/results",
        response_model=SourceResultsResponse,
        tags=["results"],
    )
    def get_source_results(
        source_id: str,
        service: Service,
        user: User,
    ) -> SourceResultsResponse:
        require_source_owner(repository, source_id=source_id, user=user)
        return service.get_source_results(source_id, owner_id=user.id)

    @router.get(
        "/sources/{source_id}/overview",
        response_model=SourceOverviewResponse,
        tags=["results"],
    )
    def get_source_overview(
        source_id: str,
        service: Service,
        user: User,
    ) -> SourceOverviewResponse:
        if not settings.enable_source_overview_v2:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Source overview endpoint is disabled",
            )
        require_source_owner(repository, source_id=source_id, user=user)
        return service.get_source_overview(source_id, owner_id=user.id)

    @router.get(
        "/sources/{source_id}/videos",
        response_model=SourceVideosPageResponse,
        tags=["results"],
    )
    def get_source_videos_page(
        source_id: str,
        service: Service,
        user: User,
        cursor: str | None = Query(default=None, max_length=512),
        limit: int = Query(default=60, ge=1, le=100),
    ) -> SourceVideosPageResponse:
        if not settings.enable_video_keyset_pagination:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Source video pagination endpoint is disabled",
            )
        require_source_owner(repository, source_id=source_id, user=user)
        return service.get_source_videos_page(
            source_id,
            owner_id=user.id,
            cursor=cursor,
            limit=limit,
        )

    @router.get(
        "/sources/{source_id}/jobs",
        response_model=list[JobStatus],
        tags=["jobs"],
    )
    def list_source_jobs(
        source_id: str,
        service: Service,
        user: User,
        limit: int = Query(default=20, ge=1, le=50),
    ) -> list[JobStatus]:
        require_source_owner(repository, source_id=source_id, user=user)
        return service.list_source_jobs(
            source_id,
            owner_id=user.id,
            limit=limit,
        )

    @router.patch(
        "/sources/{source_id}",
        response_model=CollectionSource,
        tags=["sources"],
    )
    def update_source(
        source_id: str,
        payload: CollectionSourceUpdate,
        service: Service,
        user: User,
    ) -> CollectionSource:
        require_source_owner(repository, source_id=source_id, user=user)
        return service.update_source(source_id, payload, owner_id=user.id)

    @router.delete(
        "/sources/{source_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["sources"],
    )
    def delete_source(source_id: str, service: Service, user: User) -> Response:
        require_source_owner(repository, source_id=source_id, user=user)
        service.delete_source(source_id, owner_id=user.id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post(
        "/sources/{source_id}/jobs",
        response_model=JobStatus,
        status_code=status.HTTP_201_CREATED,
        tags=["jobs"],
    )
    def create_job(
        source_id: str,
        payload: JobCreate,
        service: Service,
        user: User,
    ) -> JobStatus:
        require_source_owner(repository, source_id=source_id, user=user)
        return service.create_job(source_id, payload, owner_id=user.id)

    return router
