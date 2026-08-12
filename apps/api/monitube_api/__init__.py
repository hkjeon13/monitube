"""Monitube's API application and framework-independent collection domain.

The application factory is imported lazily so domain and tokenizer modules do
not initialize the database-backed HTTP application as an import side effect.
"""

from __future__ import annotations

from typing import Any


def create_app(*args: Any, **kwargs: Any) -> Any:
    """Load and invoke the FastAPI application factory on demand."""

    from .main import create_app as factory

    return factory(*args, **kwargs)

__all__ = ["create_app"]
