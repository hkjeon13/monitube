"""Pure mapping from YouTube failures to worker retry decisions."""

from dataclasses import dataclass
from typing import Literal, Mapping, Any

from monitube_api.domain import QuotaBucket
from monitube_api.quota import YoutubeErrorCategory, classify_youtube_error

from ..youtube_data import YouTubeApiError
from .parsing import quota_retry_delay_seconds


@dataclass(frozen=True, slots=True)
class CollectionErrorDecision:
    action: Literal["quota", "retry", "raise"]
    quota_bucket: QuotaBucket | None = None
    retry_after_seconds: int | None = None


def decide_collection_error(
    error: YouTubeApiError,
    checkpoint: Mapping[str, Any],
) -> CollectionErrorDecision:
    classification = classify_youtube_error(
        error.status_code,
        error.reasons,
        quota_bucket=error.bucket,
    )
    if classification.category is YoutubeErrorCategory.QUOTA_EXHAUSTED:
        return CollectionErrorDecision(
            action="quota",
            quota_bucket=classification.quota_bucket or error.bucket,
            retry_after_seconds=quota_retry_delay_seconds(checkpoint),
        )
    if classification.retryable:
        return CollectionErrorDecision(action="retry", retry_after_seconds=60)
    return CollectionErrorDecision(action="raise")
