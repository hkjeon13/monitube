"""Compatibility facade over resource-focused application services."""

from .application.explore_service import ExploreService, InvalidSearchQueryError
from .application.job_service import JobService
from .application.result_service import ResultService
from .application.source_service import SourceService

__all__ = ["CollectionService", "InvalidSearchQueryError"]


class CollectionService(
    SourceService,
    JobService,
    ResultService,
    ExploreService,
):
    """Application facade retained for existing entry points.

    Each use-case family lives in its own service module. Multiple inheritance is
    used only to preserve the existing dependency contract while routers migrate
    independently; all services share the single dependency initializer from
    ``ApplicationService``.
    """
