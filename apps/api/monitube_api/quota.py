"""Classify YouTube API failures for a server-managed credential binding."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Mapping, Any

from .domain import JobState, QuotaBucket


class YoutubeErrorCategory(str, Enum):
    QUOTA_EXHAUSTED = "quota_exhausted"
    RATE_LIMITED = "rate_limited"
    RETRYABLE = "retryable"
    AUTHENTICATION = "authentication"
    PERMISSION = "permission"
    INVALID_REQUEST = "invalid_request"
    RESOURCE_UNAVAILABLE = "resource_unavailable"
    NOT_FOUND = "not_found"
    FATAL = "fatal"


@dataclass(frozen=True, slots=True)
class YoutubeErrorClassification:
    category: YoutubeErrorCategory
    suggested_state: JobState
    retryable: bool
    quota_bucket: QuotaBucket | None = None


_QUOTA_REASONS = frozenset({"quotaExceeded", "dailyLimitExceeded", "dailyLimitExceededUnreg"})
_RATE_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded", "tooManyRequests"})
_AUTH_REASONS = frozenset({"authError", "invalidCredentials", "keyInvalid"})
_PERMISSION_REASONS = frozenset({"forbidden", "insufficientPermissions", "accountDelegationForbidden"})
_UNAVAILABLE_REASONS = frozenset({"commentsDisabled", "videoNotFound"})


def extract_youtube_error_reasons(payload: Mapping[str, Any] | None) -> tuple[str, ...]:
    """Extract reason strings from the standard Google JSON error envelope."""

    if not payload:
        return ()
    error = payload.get("error", payload)
    if not isinstance(error, Mapping):
        return ()
    errors = error.get("errors", [])
    if not isinstance(errors, list):
        return ()
    reasons = [item.get("reason") for item in errors if isinstance(item, Mapping) and isinstance(item.get("reason"), str)]
    return tuple(reasons)


def classify_youtube_error(
    status_code: int,
    reasons: Iterable[str] = (),
    *,
    quota_bucket: QuotaBucket = QuotaBucket.CORE,
) -> YoutubeErrorClassification:
    """Map an upstream failure to a safe, durable collection-job state.

    ``waiting_quota`` intentionally preserves its checkpoint and resumes with the
    same server-managed credential binding; it is never a signal to try another key
    or cloud project.
    """

    known = frozenset(reasons)
    if known & _QUOTA_REASONS:
        return YoutubeErrorClassification(YoutubeErrorCategory.QUOTA_EXHAUSTED, JobState.WAITING_QUOTA, True, quota_bucket)
    if known & _RATE_REASONS or status_code == 429:
        return YoutubeErrorClassification(YoutubeErrorCategory.RATE_LIMITED, JobState.WAITING_RETRY, True)
    if known & _UNAVAILABLE_REASONS:
        return YoutubeErrorClassification(YoutubeErrorCategory.RESOURCE_UNAVAILABLE, JobState.COMPLETED_WITH_WARNINGS, False)
    if known & _AUTH_REASONS or status_code == 401:
        return YoutubeErrorClassification(YoutubeErrorCategory.AUTHENTICATION, JobState.FAILED, False)
    if known & _PERMISSION_REASONS or status_code == 403:
        return YoutubeErrorClassification(YoutubeErrorCategory.PERMISSION, JobState.FAILED, False)
    if status_code == 404:
        return YoutubeErrorClassification(YoutubeErrorCategory.NOT_FOUND, JobState.COMPLETED_WITH_WARNINGS, False)
    if 500 <= status_code <= 599:
        return YoutubeErrorClassification(YoutubeErrorCategory.RETRYABLE, JobState.WAITING_RETRY, True)
    if 400 <= status_code <= 499:
        return YoutubeErrorClassification(YoutubeErrorCategory.INVALID_REQUEST, JobState.FAILED, False)
    return YoutubeErrorClassification(YoutubeErrorCategory.FATAL, JobState.FAILED, False)
