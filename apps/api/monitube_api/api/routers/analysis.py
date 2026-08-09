"""Workspace-wide video and public-comment analysis routes."""

from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from ...contracts import AnalysisInsightsResponse, AnalysisOverviewResponse
from ..dependencies import Service, User, get_current_user


router = APIRouter(prefix="/v1", dependencies=[Depends(get_current_user)])


@router.get(
    "/analysis/insights",
    response_model=AnalysisInsightsResponse,
    tags=["analysis"],
)
def analysis_insights(
    service: Service,
    user: User,
    scope: Literal["all", "channel", "keyword"] = Query(default="all"),
    target_ids: list[UUID] = Query(default=[], alias="targetId"),
    channel_ids: list[str] = Query(default=[], alias="channelId", max_length=64),
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
    limit: int = Query(default=20, ge=1, le=50),
) -> AnalysisInsightsResponse:
    from_at = (
        datetime.combine(from_date, time.min, tzinfo=UTC)
        if from_date
        else None
    )
    to_at = (
        datetime.combine(to_date + timedelta(days=1), time.min, tzinfo=UTC)
        if to_date
        else None
    )
    return service.get_analysis_insights(
        owner_id=user.id,
        scope=scope,
        target_ids=[str(identifier) for identifier in target_ids],
        channel_ids=channel_ids,
        from_at=from_at,
        to_at=to_at,
        limit=limit,
    )


@router.get(
    "/analysis/overview",
    response_model=AnalysisOverviewResponse,
    tags=["analysis"],
)
def analysis_overview(
    service: Service,
    user: User,
    scope: Literal["all", "channel", "keyword"] = Query(default="all"),
    target_ids: list[UUID] = Query(default=[], alias="targetId"),
    channel_ids: list[str] = Query(default=[], alias="channelId", max_length=64),
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
    comment_type: Literal["all", "top_level", "reply"] = Query(
        default="all",
        alias="commentType",
    ),
    section: Literal["all", "core", "content"] = Query(default="all"),
    limit: int = Query(default=10, ge=1, le=25),
) -> AnalysisOverviewResponse:
    from_at = (
        datetime.combine(from_date, time.min, tzinfo=UTC)
        if from_date
        else None
    )
    to_at = (
        datetime.combine(to_date + timedelta(days=1), time.min, tzinfo=UTC)
        if to_date
        else None
    )
    return service.get_analysis_overview(
        owner_id=user.id,
        scope=scope,
        target_ids=[str(identifier) for identifier in target_ids],
        channel_ids=channel_ids,
        from_at=from_at,
        to_at=to_at,
        comment_type=comment_type,
        section=section,
        limit=limit,
    )
