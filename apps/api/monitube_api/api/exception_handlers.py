"""Stable translation from domain/repository failures to HTTP responses."""

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from ..channel_resolution import ChannelInputError
from ..repositories import (
    InvalidCursorError,
    InvalidStateTransitionError,
    NotFoundError,
    RepositoryError,
    RepositoryUnavailableError,
)
from ..services import InvalidSearchQueryError
from ..video_resolution import VideoInputError


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(NotFoundError)
    async def not_found_handler(_: Request, exc: NotFoundError) -> Response:
        return JSONResponse(
            content={"detail": str(exc)},
            status_code=status.HTTP_404_NOT_FOUND,
        )

    @app.exception_handler(ChannelInputError)
    async def channel_input_handler(_: Request, exc: ChannelInputError) -> Response:
        return JSONResponse(
            content={"detail": str(exc)},
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    @app.exception_handler(VideoInputError)
    async def video_input_handler(_: Request, exc: VideoInputError) -> Response:
        return JSONResponse(
            content={"detail": str(exc)},
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    @app.exception_handler(InvalidSearchQueryError)
    async def invalid_search_query_handler(
        _: Request,
        exc: InvalidSearchQueryError,
    ) -> Response:
        return JSONResponse(
            content={"detail": str(exc)},
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    @app.exception_handler(ValidationError)
    async def validation_handler(_: Request, exc: ValidationError) -> Response:
        return JSONResponse(
            content={
                "detail": "Invalid source configuration",
                "errors": exc.errors(),
            },
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    @app.exception_handler(InvalidStateTransitionError)
    async def state_transition_handler(
        _: Request,
        exc: InvalidStateTransitionError,
    ) -> Response:
        return JSONResponse(
            content={"detail": str(exc)},
            status_code=status.HTTP_409_CONFLICT,
        )
    @app.exception_handler(InvalidCursorError)
    async def invalid_cursor_handler(_: Request, exc: InvalidCursorError) -> Response:
        return JSONResponse(
            content={"detail": str(exc)},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    @app.exception_handler(RepositoryUnavailableError)
    async def repository_unavailable_handler(
        _: Request,
        exc: RepositoryUnavailableError,
    ) -> Response:
        return JSONResponse(
            content={"detail": str(exc), "retryable": True},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    @app.exception_handler(RepositoryError)
    async def repository_handler(_: Request, exc: RepositoryError) -> Response:
        return JSONResponse(
            content={"detail": str(exc)},
            status_code=status.HTTP_409_CONFLICT,
        )
