"""Scalar sanitizers shared by PostgreSQL adapter modules."""

from typing import Any


def _strip_nul(value: Any) -> Any:
    """PostgreSQL text fields reject NUL bytes from upstream public metadata."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {key: _strip_nul(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_strip_nul(item) for item in value]
    return value


def _optional_nonnegative_int(value: Any) -> int | None:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None
