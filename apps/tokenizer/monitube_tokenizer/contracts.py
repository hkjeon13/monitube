"""Strict internal tokenizer request and response contracts."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .analyzer import ANALYZER_VERSION


MAX_DOCUMENTS = 16
MAX_SEGMENTS_PER_DOCUMENT = 1_000
MAX_TOTAL_TEXT_BYTES = 4 * 1024 * 1024


class InternalModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TokenizeSegment(InternalModel):
    sequence: int = Field(ge=0)
    text: str


class TokenizeDocument(InternalModel):
    id: str = Field(min_length=1, max_length=255)
    text: str
    segments: list[TokenizeSegment] = Field(
        default_factory=list, max_length=MAX_SEGMENTS_PER_DOCUMENT
    )


class TokenizeRequest(InternalModel):
    analyzerVersion: str
    documents: list[TokenizeDocument] = Field(
        min_length=1, max_length=MAX_DOCUMENTS
    )

    @model_validator(mode="after")
    def validate_payload(self) -> "TokenizeRequest":
        if self.analyzerVersion != ANALYZER_VERSION:
            raise ValueError("Unsupported analyzer version")
        total_bytes = sum(
            len(document.text.encode("utf-8"))
            + sum(len(segment.text.encode("utf-8")) for segment in document.segments)
            for document in self.documents
        )
        if total_bytes > MAX_TOTAL_TEXT_BYTES:
            raise ValueError(
                f"Tokenizer text payload exceeds {MAX_TOTAL_TEXT_BYTES} bytes"
            )
        return self


class TokenizedSegment(InternalModel):
    sequence: int
    tokens: list[str]


class TokenizedDocument(InternalModel):
    id: str
    tokens: list[str]
    segments: list[TokenizedSegment]


class TokenizeResponse(InternalModel):
    analyzerVersion: str
    documents: list[TokenizedDocument]


class HealthResponse(InternalModel):
    status: str
    service: str
