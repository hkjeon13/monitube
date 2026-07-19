"""Collected comment and comment-thread read routes."""

from typing import Literal

from fastapi import APIRouter, Depends, Query

from ...contracts import (
    CommentDetailResponse,
    CommentRepliesResponse,
    VideoCommentThreadsResponse,
    VideoCommentsResponse,
)
from ..dependencies import Service, User, get_current_user


router = APIRouter(prefix="/v1", dependencies=[Depends(get_current_user)])


@router.get(
    "/videos/{video_id}/comments",
    response_model=VideoCommentsResponse,
    tags=["results"],
)
def get_video_comments(
    video_id: str,
    service: Service,
    user: User,
) -> VideoCommentsResponse:
    return service.get_video_comments(video_id, owner_id=user.id)


@router.get(
    "/videos/{video_id}/comment-threads",
    response_model=VideoCommentThreadsResponse,
    tags=["results"],
)
def get_video_comment_threads(
    video_id: str,
    service: Service,
    user: User,
    cursor: str | None = Query(default=None, max_length=512),
    limit: int = Query(default=20, ge=1, le=100),
    sort: Literal["newest", "oldest", "recommended"] = Query(default="newest"),
) -> VideoCommentThreadsResponse:
    return service.get_video_comment_threads(
        video_id,
        owner_id=user.id,
        cursor=cursor,
        limit=limit,
        sort=sort,
    )


@router.get(
    "/comments/{comment_id}/replies",
    response_model=CommentRepliesResponse,
    tags=["results"],
)
def get_comment_replies(
    comment_id: str,
    service: Service,
    user: User,
    cursor: str | None = Query(default=None, max_length=512),
    limit: int = Query(default=20, ge=1, le=100),
) -> CommentRepliesResponse:
    return service.get_comment_replies(
        comment_id,
        owner_id=user.id,
        cursor=cursor,
        limit=limit,
    )


@router.get(
    "/comments/{comment_id}",
    response_model=CommentDetailResponse,
    tags=["results"],
)
def get_comment_detail(
    comment_id: str,
    service: Service,
    user: User,
) -> CommentDetailResponse:
    return service.get_comment_detail(comment_id, owner_id=user.id)
