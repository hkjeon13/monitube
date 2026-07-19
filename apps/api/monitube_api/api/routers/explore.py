"""Explore, channel history, and collected-content search routes."""

from typing import Literal

from fastapi import APIRouter, Depends, Query

from ...contracts import (
    ChannelSubscriberSnapshot,
    ExploreChannelsResponse,
    ExploreResponse,
    ExploreVideosPageResponse,
    UnifiedSearchResponse,
)
from ..dependencies import Service, User, get_current_user


router = APIRouter(prefix="/v1", dependencies=[Depends(get_current_user)])


@router.get("/explore", response_model=ExploreResponse, tags=["explore"])
def explore(
    service: Service,
    user: User,
    channel_id: str | None = Query(
        default=None,
        alias="channelId",
        min_length=1,
        max_length=64,
    ),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=60, ge=12, le=120),
) -> ExploreResponse:
    return service.explore(
        owner_id=user.id,
        channel_id=channel_id,
        offset=offset,
        limit=limit,
    )


@router.get(
    "/explore/channels",
    response_model=ExploreChannelsResponse,
    tags=["explore"],
)
def explore_channels(service: Service, user: User) -> ExploreChannelsResponse:
    return service.explore_channels(owner_id=user.id)


@router.get(
    "/explore/videos",
    response_model=ExploreVideosPageResponse,
    tags=["explore"],
)
def explore_videos_page(
    service: Service,
    user: User,
    channel_id: str | None = Query(
        default=None,
        alias="channelId",
        min_length=1,
        max_length=64,
    ),
    cursor: str | None = Query(default=None, max_length=768),
    limit: int = Query(default=60, ge=1, le=100),
) -> ExploreVideosPageResponse:
    return service.explore_videos_page(
        owner_id=user.id,
        channel_id=channel_id,
        cursor=cursor,
        limit=limit,
    )


@router.get(
    "/channels/{youtube_channel_id}/subscriber-history",
    response_model=list[ChannelSubscriberSnapshot],
    tags=["explore"],
)
def channel_subscriber_history(
    youtube_channel_id: str,
    service: Service,
    user: User,
) -> list[ChannelSubscriberSnapshot]:
    return service.channel_subscriber_history(
        youtube_channel_id,
        owner_id=user.id,
    )


@router.get("/search", response_model=UnifiedSearchResponse, tags=["search"])
def search_collected(
    service: Service,
    user: User,
    q: str = Query(min_length=2, max_length=200),
    limit: int = Query(default=20, ge=1, le=50),
    scope: Literal["all", "videos", "comments"] = Query(default="all"),
) -> UnifiedSearchResponse:
    return service.search_collected(
        q,
        owner_id=user.id,
        limit=limit,
        scope=scope,
    )
