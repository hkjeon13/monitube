"""Defensive conversion of YouTube response scalar values."""

from datetime import UTC, datetime
import re
from typing import Any, Mapping


_DURATION = re.compile(
    r"^P(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?"
    r"(?:(?P<seconds>\d+)S)?)?$"
)


def parse_rfc3339(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def parse_duration_seconds(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    matched = _DURATION.fullmatch(value)
    if not matched:
        return None
    parts = {
        name: int(raw or 0) for name, raw in matched.groupdict().items()
    }
    return (
        parts["days"] * 86_400
        + parts["hours"] * 3_600
        + parts["minutes"] * 60
        + parts["seconds"]
    )


def as_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def quota_retry_delay_seconds(checkpoint: Mapping[str, Any]) -> int:
    """Back off quota-paused work at 1h, 2h, then 3h intervals."""

    prior_attempts = as_int(checkpoint.get("quotaRetryAttempt"))
    return min(10_800, 3_600 * (prior_attempts + 1))
