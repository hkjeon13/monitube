"""Collection job status routes."""

from fastapi import APIRouter, Depends, Query

from ...contracts import (
    ActiveParentJobsResponse,
    JobStatus,
    RecentJobFailuresResponse,
)
from ..dependencies import Service, User, get_current_user


router = APIRouter(prefix="/v1", dependencies=[Depends(get_current_user)])


@router.get("/jobs/active", response_model=ActiveParentJobsResponse, tags=["jobs"])
def list_active_parent_jobs(
    service: Service,
    user: User,
) -> ActiveParentJobsResponse:
    return service.list_active_parent_jobs(owner_id=user.id)


@router.get(
    "/jobs/recent-failures",
    response_model=RecentJobFailuresResponse,
    tags=["jobs"],
)
def list_recent_job_failures(
    service: Service,
    user: User,
    limit: int = Query(default=10, ge=1, le=50),
) -> RecentJobFailuresResponse:
    return service.list_recent_failed_parent_jobs(owner_id=user.id, limit=limit)


@router.get("/jobs/{job_id}", response_model=JobStatus, tags=["jobs"])
def get_job(job_id: str, service: Service, user: User) -> JobStatus:
    return service.get_job(job_id, owner_id=user.id)
