"""Pure, shared policy for monotonically widening collection coverage."""

from copy import deepcopy
from typing import Any

from .domain import JobRecord, SourceType


def desired_coverage(
    source_type: SourceType,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Return collection breadth without target identity or display fields."""

    desired: dict[str, Any] = {
        "complete": False,
        "includeComments": bool(config.get("includeComments", False)),
        "collectAllComments": bool(
            config.get("includeComments", False)
            and config.get("collectAllComments", False)
        ),
        "maxCommentPagesPerVideo": int(
            config.get("maxCommentPagesPerVideo") or 1
        ),
    }
    if source_type is SourceType.CHANNEL:
        desired["collectAllVideos"] = bool(
            config.get("collectAllVideos", False)
        )
        desired["maxVideos"] = int(config.get("maxVideos") or 50)
    elif source_type is SourceType.KEYWORD:
        desired["maxPagesPerRun"] = int(config.get("maxPagesPerRun") or 1)
    return desired


def merge_collection_config(
    source_type: SourceType,
    current: dict[str, Any],
    incoming: dict[str, Any],
) -> dict[str, Any]:
    """Monotonically widen coverage while retaining canonical identity fields."""

    merged = deepcopy(current)
    for key, value in incoming.items():
        merged.setdefault(key, deepcopy(value))
    merged["includeComments"] = bool(
        current.get("includeComments", False)
        or incoming.get("includeComments", False)
    )
    merged["collectAllComments"] = bool(
        current.get("collectAllComments", False)
        or incoming.get("collectAllComments", False)
    )
    merged["maxCommentPagesPerVideo"] = max(
        int(current.get("maxCommentPagesPerVideo") or 1),
        int(incoming.get("maxCommentPagesPerVideo") or 1),
    )
    if source_type is SourceType.CHANNEL:
        merged["collectAllVideos"] = bool(
            current.get("collectAllVideos", False)
            or incoming.get("collectAllVideos", False)
        )
        merged["maxVideos"] = max(
            int(current.get("maxVideos") or 1),
            int(incoming.get("maxVideos") or 1),
        )
    elif source_type is SourceType.KEYWORD:
        merged["maxPagesPerRun"] = max(
            int(current.get("maxPagesPerRun") or 1),
            int(incoming.get("maxPagesPerRun") or 1),
        )
    return merged


def coverage_satisfies(
    coverage: dict[str, Any],
    desired: dict[str, Any],
) -> bool:
    if not coverage.get("complete"):
        return False
    if desired.get("includeComments") and not coverage.get("includeComments"):
        return False
    if desired.get("collectAllComments") and not coverage.get(
        "collectAllComments"
    ):
        return False
    if desired.get("collectAllVideos") and not coverage.get("collectAllVideos"):
        return False
    for key in ("maxVideos", "maxPagesPerRun"):
        if key in desired and int(coverage.get(key) or 0) < int(desired[key]):
            return False
    return not desired.get("includeComments") or int(
        coverage.get("maxCommentPagesPerVideo") or 0
    ) >= int(desired.get("maxCommentPagesPerVideo") or 1)


def job_coverage(
    job: JobRecord,
    source_type: SourceType,
    source_config: dict[str, Any],
) -> dict[str, Any]:
    coverage = {
        "complete": False,
        "includeComments": bool(job.include_comments),
        "collectAllComments": bool(
            job.include_comments and source_config.get("collectAllComments")
        ),
        "maxCommentPagesPerVideo": int(
            job.max_comments_per_video
            or source_config.get("maxCommentPagesPerVideo")
            or 1
        ),
    }
    if source_type is SourceType.CHANNEL:
        coverage["collectAllVideos"] = bool(
            source_config.get("collectAllVideos")
        )
        coverage["maxVideos"] = int(
            job.max_videos or source_config.get("maxVideos") or 50
        )
    elif source_type is SourceType.KEYWORD:
        coverage["maxPagesPerRun"] = int(
            source_config.get("maxPagesPerRun") or 1
        )
    return coverage
