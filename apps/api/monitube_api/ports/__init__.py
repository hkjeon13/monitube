"""Persistence capabilities required by application and worker use cases."""

from typing import Protocol

from .collection import CollectionWriteRepository, QuotaAuditRepository
from .jobs import JobLeaseRepository, JobRepository
from .results import ExploreReadRepository, ResultReadRepository
from .sources import (
    CollectionRequestRepository,
    SourceRepository,
    SubscriptionRepository,
)


class CollectionRepository(
    SourceRepository,
    SubscriptionRepository,
    CollectionRequestRepository,
    JobRepository,
    JobLeaseRepository,
    CollectionWriteRepository,
    QuotaAuditRepository,
    ResultReadRepository,
    ExploreReadRepository,
    Protocol,
):
    """Composite compatibility port for cross-capability entry points."""


__all__ = [
    "CollectionRepository",
    "CollectionRequestRepository",
    "CollectionWriteRepository",
    "ExploreReadRepository",
    "JobLeaseRepository",
    "JobRepository",
    "QuotaAuditRepository",
    "ResultReadRepository",
    "SourceRepository",
    "SubscriptionRepository",
]
