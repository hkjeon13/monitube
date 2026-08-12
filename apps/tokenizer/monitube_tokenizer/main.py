"""Internal-only stateless tokenizer HTTP application."""

from __future__ import annotations

from contextlib import asynccontextmanager
import os
from threading import BoundedSemaphore
from typing import Protocol

from fastapi import FastAPI, HTTPException, Request, status

from .analyzer import (
    ANALYZER_VERSION,
    MecabNltkNounAnalyzer,
    analyzer_health,
)
from .contracts import (
    HealthResponse,
    TokenizeRequest,
    TokenizeResponse,
    TokenizedDocument,
    TokenizedSegment,
)


class Analyzer(Protocol):
    version: str

    def extract(self, text: str | None) -> list[str]: ...


def create_app(analyzer: Analyzer | None = None) -> FastAPI:
    configured_concurrency = _configured_concurrency()
    semaphore = BoundedSemaphore(configured_concurrency)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.analyzer = analyzer or MecabNltkNounAnalyzer()
        app.state.semaphore = semaphore
        yield

    app = FastAPI(
        title="Monitube Internal Tokenizer",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(status="ok", service="monitube-tokenizer")

    @app.get("/ready")
    def ready(request: Request) -> dict[str, object]:
        current = request.app.state.analyzer
        if isinstance(current, MecabNltkNounAnalyzer):
            check = analyzer_health(current)
        else:
            check = {"status": "ok", "version": current.version}
        return {"status": "ready", "checks": {"analyzer": check}}

    @app.post("/internal/v1/tokenize", response_model=TokenizeResponse)
    def tokenize(payload: TokenizeRequest, request: Request) -> TokenizeResponse:
        acquired = request.app.state.semaphore.acquire(timeout=0.05)
        if not acquired:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Tokenizer concurrency limit reached",
                headers={"Retry-After": "1"},
            )
        try:
            current: Analyzer = request.app.state.analyzer
            if current.version != ANALYZER_VERSION:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Tokenizer analyzer version mismatch",
                )
            return TokenizeResponse(
                analyzerVersion=ANALYZER_VERSION,
                documents=[
                    TokenizedDocument(
                        id=document.id,
                        tokens=current.extract(document.text),
                        segments=[
                            TokenizedSegment(
                                sequence=segment.sequence,
                                tokens=current.extract(segment.text),
                            )
                            for segment in document.segments
                        ],
                    )
                    for document in payload.documents
                ],
            )
        finally:
            request.app.state.semaphore.release()

    return app


def _configured_concurrency() -> int:
    raw_value = os.getenv("TOKENIZER_MAX_CONCURRENCY", "2").strip()
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError("TOKENIZER_MAX_CONCURRENCY must be an integer") from exc
    if not 1 <= value <= 32:
        raise RuntimeError("TOKENIZER_MAX_CONCURRENCY must be between 1 and 32")
    return value


app = create_app()
