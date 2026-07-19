"""Pure construction and interpretation of durable collection checkpoints."""

from typing import Any, Mapping

from .parsing import as_int


DURABLE_CHECKPOINT_KEYS = (
    "jobKind",
    "youtubeVideoId",
    "fanoutDiscovered",
    "fanoutVideoCount",
    "phaseProgress",
    "quotaRetryAttempt",
    "keywordExpectedTotal",
)


def checkpoint_payload(
    active: Mapping[str, Any],
    *,
    stage: str,
    scope_key: str,
    page_token: str | None,
    batch_cursor: int = 0,
) -> dict[str, Any]:
    """Replace a cursor while retaining durable job identity and progress."""

    preserved = {
        key: active[key] for key in DURABLE_CHECKPOINT_KEYS if key in active
    }
    return {
        **preserved,
        "stage": stage,
        "scopeKey": scope_key,
        "pageToken": page_token,
        "batchCursor": batch_cursor,
    }


def resume_cursor(
    checkpoint: Mapping[str, Any],
    *,
    stage: str,
    scope_key: str,
) -> tuple[str | None, int]:
    if checkpoint.get("stage") != stage or checkpoint.get("scopeKey") != scope_key:
        return None, 0
    page_token = checkpoint.get("pageToken")
    return (
        str(page_token) if page_token else None,
        as_int(checkpoint.get("batchCursor")),
    )


def with_phase_progress(
    active: Mapping[str, Any],
    *,
    phase: str,
    completed: int,
    total: int | None,
) -> dict[str, Any]:
    checkpoint = dict(active)
    existing = checkpoint.get("phaseProgress")
    phases = dict(existing) if isinstance(existing, dict) else {}
    phases[phase] = {
        "completed": max(0, completed),
        "total": max(0, total) if total is not None else None,
    }
    checkpoint["phaseProgress"] = phases
    return checkpoint
