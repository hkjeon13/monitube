"""Ownership checks shared by HTTP resource routers."""

from fastapi import HTTPException, status

from ..auth import AuthUser
from ..contracts import CollectionSource
from ..ports import CollectionRepository
from ..services import CollectionService


def require_source_owner(
    repository: CollectionRepository,
    *,
    source_id: str,
    user: AuthUser,
) -> None:
    """Authorize a public Sources ID, which is a user subscription ID."""

    owns_source = getattr(repository, "source_owned_by", None)
    if owns_source and not owns_source(source_id=source_id, owner_id=user.id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Source was not found",
        )


def require_target_owner(
    repository: CollectionRepository,
    *,
    target_id: str,
    user: AuthUser,
) -> None:
    """Keep legacy target-pin routes from exposing another user's target."""

    owns_target = getattr(repository, "target_owned_by", None)
    if owns_target and not owns_target(target_id=target_id, owner_id=user.id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Collection target was not found",
        )


def require_target_subscription(
    *,
    target_id: str,
    service: CollectionService,
    user: AuthUser,
) -> CollectionSource:
    """Resolve a user's subscription without returning a worker source."""

    source = next(
        (
            item
            for item in service.list_sources(owner_id=user.id)
            if item.targetId == target_id
        ),
        None,
    )
    if source is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Collection target was not found",
        )
    return source
