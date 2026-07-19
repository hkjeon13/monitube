"""Legacy collection-target pin compatibility routes."""

from fastapi import APIRouter, Depends, HTTPException, status

from ...contracts import CollectionSourceUpdate, TargetPin, TargetPinUpdate
from ...repositories import CollectionRepository
from ..authorization import require_target_owner, require_target_subscription
from ..dependencies import Service, User, get_current_user


def create_pins_router(repository: CollectionRepository) -> APIRouter:
    router = APIRouter(prefix="/v1", dependencies=[Depends(get_current_user)])

    @router.put(
        "/collection-targets/{target_id}/pin",
        response_model=TargetPin,
        tags=["pins"],
    )
    def set_target_pin(
        target_id: str,
        payload: TargetPinUpdate,
        service: Service,
        user: User,
    ) -> TargetPin:
        """Map the legacy shared pin control to the caller's subscription."""

        require_target_owner(repository, target_id=target_id, user=user)
        subscription = require_target_subscription(
            target_id=target_id,
            service=service,
            user=user,
        )
        service.update_source(
            subscription.id,
            CollectionSourceUpdate(enabled=payload.enabled),
            owner_id=user.id,
        )
        pin = service.get_target_pin(target_id)
        if pin is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Collection target has no refresh pin",
            )
        return pin

    @router.get(
        "/collection-targets/{target_id}/pin",
        response_model=TargetPin | None,
        tags=["pins"],
    )
    def get_target_pin(
        target_id: str,
        service: Service,
        user: User,
    ) -> TargetPin | None:
        require_target_owner(repository, target_id=target_id, user=user)
        return service.get_target_pin(target_id)

    return router
