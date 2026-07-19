"""Shared FastAPI dependencies for authenticated API routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request, Response, status

from ..auth import AuthStore, AuthUser, SESSION_MAX_AGE_SECONDS
from ..services import CollectionService
from ..settings import Settings


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
    """Require a browser session in PostgreSQL-backed deployments."""

    auth_store: AuthStore | None = request.app.state.auth_store
    if auth_store is None:
        return AuthUser(id="in-memory", username="psyche")
    token = request.cookies.get("monitube_session")
    user = auth_store.user_for_session(token)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="로그인이 필요합니다.",
        )
    auth_store.refresh_session(token)
    set_session_cookie(response, token, request.app.state.settings)
    return user


User = Annotated[AuthUser, Depends(get_current_user)]
