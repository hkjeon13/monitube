"""YouTube channel and video input normalization routes."""

from fastapi import APIRouter, Depends

from ...contracts import (
    ChannelResolutionRequest,
    ChannelResolutionResponse,
    VideoResolutionRequest,
    VideoResolutionResponse,
)
from ...video_resolution import resolve_video_input
from ..dependencies import Service, get_current_user


router = APIRouter(prefix="/v1", dependencies=[Depends(get_current_user)])


@router.post(
    "/channel-resolutions",
    response_model=ChannelResolutionResponse,
    tags=["channels"],
)
def resolve_channel(
    payload: ChannelResolutionRequest,
    service: Service,
) -> ChannelResolutionResponse:
    return service.resolve_channel(payload.input)


@router.post(
    "/video-resolutions",
    response_model=VideoResolutionResponse,
    tags=["videos"],
)
def resolve_video(payload: VideoResolutionRequest) -> VideoResolutionResponse:
    resolution = resolve_video_input(payload.input)
    return VideoResolutionResponse(
        kind=resolution.kind.value,
        normalized=resolution.normalized,
    )
