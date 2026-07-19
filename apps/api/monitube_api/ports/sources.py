"""Source, subscription, and canonical-target persistence ports."""

from typing import Any, Protocol

from ..domain import (
    CollectionSubmission,
    CollectionSubscriptionRecord,
    CollectionTargetRecord,
    SourceRecord,
    SourceType,
)


class SourceRepository(Protocol):
    def create_source(
        self,
        *,
        source_type: SourceType,
        config: dict[str, Any],
        owner_id: str | None = None,
    ) -> SourceRecord: ...

    def get_source(
        self,
        source_id: str,
        *,
        owner_id: str | None = None,
    ) -> SourceRecord: ...

    def list_sources(
        self,
        *,
        owner_id: str | None = None,
    ) -> list[SourceRecord]: ...

    def update_source(
        self,
        source_id: str,
        *,
        owner_id: str | None = None,
        **changes: Any,
    ) -> SourceRecord: ...

    def delete_source(
        self,
        source_id: str,
        *,
        owner_id: str | None = None,
    ) -> None: ...

    def source_owned_by(self, *, source_id: str, owner_id: str) -> bool: ...

    def target_owned_by(self, *, target_id: str, owner_id: str) -> bool: ...

    def job_owned_by(self, *, job_id: str, owner_id: str) -> bool: ...

    def owner_has_sources(self, *, owner_id: str) -> bool: ...


class SubscriptionRepository(Protocol):
    def get_subscription(
        self,
        subscription_id: str,
        *,
        owner_id: str | None = None,
    ) -> CollectionSubscriptionRecord: ...

    def list_subscriptions(
        self,
        *,
        owner_id: str,
    ) -> list[CollectionSubscriptionRecord]: ...

    def ensure_subscription(
        self,
        *,
        owner_id: str,
        target_id: str,
        display_config: dict[str, Any],
    ) -> CollectionSubscriptionRecord: ...

    def subscription_target_ids(
        self,
        *,
        owner_id: str,
        enabled_only: bool = True,
    ) -> set[str]: ...


class CollectionRequestRepository(Protocol):
    def submit_collection_request(
        self,
        *,
        source_type: SourceType,
        config: dict[str, Any],
        canonical_key: str,
        aliases: list[tuple[str, str]],
        force_refresh: bool,
        idempotency_key: str | None,
        owner_id: str | None = None,
        runtime_config_id: str | None = None,
    ) -> CollectionSubmission: ...

    def promote_channel_target(
        self,
        *,
        source_id: str,
        youtube_channel_id: str,
        handle: str | None = None,
    ) -> CollectionTargetRecord | None: ...

    def set_target_pin(
        self,
        *,
        target_id: str,
        enabled: bool,
        interval_minutes: int,
    ) -> dict[str, Any]: ...

    def get_target_pin(self, *, target_id: str) -> dict[str, Any] | None: ...

    def dispatch_due_pins(
        self,
        *,
        runtime_config_id: str | None = None,
        limit: int = 10,
    ) -> int: ...
