"""Persistence contracts and a fully usable in-memory implementation for local tests."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
import hashlib
from threading import RLock
from typing import Any, Iterable, Protocol

from .analysis import build_summary
from .domain import (
    CollectionRequestRecord,
    CollectionSubmission,
    CollectionSubscriptionRecord,
    CollectionTargetRecord,
    CommentRecord,
    JobRecord,
    JobState,
    QuotaBucket,
    SourceRecord,
    SourceType,
    VideoRecord,
    new_id,
    utcnow,
)
from .fuzzy_search import rank_text_fields


class RepositoryError(RuntimeError):
    pass


class NotFoundError(RepositoryError):
    pass


class InvalidStateTransitionError(RepositoryError):
    pass


class SourceRepository(Protocol):
    def create_source(self, *, source_type: SourceType, config: dict[str, Any], owner_id: str | None = None) -> SourceRecord: ...

    def get_source(self, source_id: str, *, owner_id: str | None = None) -> SourceRecord: ...

    def list_sources(self, *, owner_id: str | None = None) -> list[SourceRecord]: ...

    def update_source(self, source_id: str, *, owner_id: str | None = None, **changes: Any) -> SourceRecord: ...

    def delete_source(self, source_id: str, *, owner_id: str | None = None) -> None: ...


class JobRepository(Protocol):
    def create_job(
        self,
        *,
        source_id: str,
        include_comments: bool,
        max_videos: int | None,
        max_comments_per_video: int | None,
        owner_id: str | None = None,
        runtime_config_id: str | None = None,
    ) -> JobRecord: ...

    def get_job(self, job_id: str, *, owner_id: str | None = None) -> JobRecord: ...

    def list_jobs_for_source(self, source_id: str, *, limit: int = 20, owner_id: str | None = None) -> list[JobRecord]: ...

    def transition_job(self, job_id: str, state: JobState, **changes: Any) -> JobRecord: ...


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

    def promote_channel_target(self, *, source_id: str, youtube_channel_id: str, handle: str | None = None) -> CollectionTargetRecord | None: ...

    def set_target_pin(self, *, target_id: str, enabled: bool, interval_minutes: int) -> dict[str, Any]: ...

    def get_target_pin(self, *, target_id: str) -> dict[str, Any] | None: ...

    def dispatch_due_pins(self, *, runtime_config_id: str | None = None, limit: int = 10) -> int: ...

    def list_explore(self, *, limit: int = 60, channel_id: str | None = None, owner_id: str | None = None) -> dict[str, Any]: ...

    def list_channel_subscriber_history(
        self, *, youtube_channel_id: str, limit: int = 180, owner_id: str | None = None
    ) -> list[dict[str, Any]]: ...

    def search_collected(self, *, query: str, limit: int = 20, owner_id: str | None = None, scope: str = "all") -> dict[str, Any]: ...


class CollectionRepository(SourceRepository, JobRepository, CollectionRequestRepository, Protocol):
    """Methods used by the API service and the polling collection worker."""

    def get_subscription(
        self, subscription_id: str, *, owner_id: str | None = None
    ) -> CollectionSubscriptionRecord: ...

    def list_subscriptions(self, *, owner_id: str) -> list[CollectionSubscriptionRecord]: ...

    def ensure_subscription(
        self, *, owner_id: str, target_id: str, display_config: dict[str, Any]
    ) -> CollectionSubscriptionRecord: ...

    def bootstrap_runtime_config(
        self, *, environment: str, google_project_number: str, secret_ref: str, key_fingerprint: str | None
    ) -> str: ...

    def claim_next_job(self, *, worker_id: str, lease_seconds: int = 120) -> JobRecord | None: ...

    def renew_job_lease(self, *, job_id: str, worker_id: str, lease_seconds: int = 120) -> bool: ...

    def checkpoint_job(self, job_id: str, checkpoint: dict[str, Any]) -> JobRecord: ...

    def update_job_progress(
        self, job_id: str, *, completed: int, total: int | None, unit: str, current_stage: str | None = None
    ) -> JobRecord: ...

    def upsert_channel(self, channel: dict[str, Any]) -> dict[str, Any]: ...

    def upsert_video(self, video: VideoRecord) -> VideoRecord: ...

    def get_videos_by_youtube_ids(self, youtube_video_ids: Iterable[str]) -> dict[str, VideoRecord]: ...

    def count_videos_by_channel(self, youtube_channel_id: str) -> int: ...

    def link_source_video(self, source_id: str, youtube_video_id: str) -> None: ...

    def source_video_ids(self, source_id: str, youtube_video_ids: Iterable[str]) -> set[str]: ...

    def count_source_videos(self, source_id: str) -> int: ...

    def upsert_comment(self, comment: CommentRecord) -> CommentRecord: ...

    def existing_comment_ids(self, youtube_comment_ids: Iterable[str]) -> set[str]: ...

    def comment_counts_by_video(self, youtube_video_ids: Iterable[str]) -> dict[str, int]: ...

    def record_api_request(self, *, job_id: str, bucket: QuotaBucket, endpoint: str, status_code: int, error_reason: str | None = None) -> None: ...

    def save_analysis_summary(self, source_id: str) -> dict[str, Any]: ...

    def get_source_results(self, source_id: str, *, owner_id: str | None = None) -> dict[str, Any]: ...

    def get_video_comments(self, video_id: str, *, owner_id: str | None = None) -> dict[str, Any]: ...

    def get_comment_detail(self, comment_id: str, *, owner_id: str | None = None) -> dict[str, Any]: ...

    def source_owned_by(self, *, source_id: str, owner_id: str) -> bool: ...

    def target_owned_by(self, *, target_id: str, owner_id: str) -> bool: ...

    def job_owned_by(self, *, job_id: str, owner_id: str) -> bool: ...

    def owner_has_sources(self, *, owner_id: str) -> bool: ...

    def subscription_target_ids(self, *, owner_id: str, enabled_only: bool = True) -> set[str]: ...


_ALLOWED_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.QUEUED: frozenset({JobState.RUNNING, JobState.WAITING_RETRY, JobState.WAITING_QUOTA, JobState.CANCELLED}),
    JobState.RUNNING: frozenset(
        {
            JobState.WAITING_RETRY,
            JobState.WAITING_QUOTA,
            JobState.COMPLETED,
            JobState.COMPLETED_WITH_WARNINGS,
            JobState.FAILED,
            JobState.CANCELLED,
        }
    ),
    JobState.WAITING_RETRY: frozenset({JobState.QUEUED, JobState.RUNNING, JobState.CANCELLED, JobState.FAILED}),
    JobState.WAITING_QUOTA: frozenset({JobState.QUEUED, JobState.RUNNING, JobState.CANCELLED, JobState.FAILED}),
    JobState.FAILED: frozenset({JobState.QUEUED, JobState.CANCELLED}),
    JobState.CANCELLED: frozenset({JobState.QUEUED}),
    JobState.COMPLETED: frozenset(),
    JobState.COMPLETED_WITH_WARNINGS: frozenset(),
}


class InMemoryRepository(CollectionRepository):
    """Thread-safe test/local repository with the same durable boundary as PostgreSQL.

    It is deliberately useful enough to run the API and fake collector tests without a
    database. Runtime config records contain only a reference and fingerprint; a raw
    API key is never accepted or retained by this repository.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._sources: dict[str, SourceRecord] = {}
        # Physical sources remain worker-facing compatibility records.  Sources
        # returned to an authenticated browser are subscriptions below.
        self._source_owners: dict[str, str] = {}
        self._subscriptions: dict[str, CollectionSubscriptionRecord] = {}
        self._subscription_ids_by_user_target: dict[tuple[str, str], str] = {}
        self._jobs: dict[str, JobRecord] = {}
        self._targets: dict[str, CollectionTargetRecord] = {}
        self._target_ids_by_key: dict[tuple[SourceType, str], str] = {}
        self._target_aliases: dict[tuple[SourceType, str, str], str] = {}
        self._requests: dict[str, CollectionRequestRecord] = {}
        self._target_videos: dict[str, set[str]] = {}
        self._pins: dict[str, dict[str, Any]] = {}
        self._runtime_configs: dict[str, dict[str, Any]] = {}
        self._channels: dict[str, dict[str, Any]] = {}
        self._videos: dict[str, VideoRecord] = {}
        self._comments: dict[str, CommentRecord] = {}
        self._source_videos: dict[str, set[str]] = {}
        self._analysis: dict[str, dict[str, Any]] = {}
        self._request_logs: list[dict[str, Any]] = []

    @staticmethod
    def _clone_source(record: SourceRecord) -> SourceRecord:
        return replace(
            record,
            config=deepcopy(record.config),
            coverage=deepcopy(record.coverage),
            latest_job=InMemoryRepository._clone_job(record.latest_job) if record.latest_job else None,
        )

    @staticmethod
    def _clone_job(record: JobRecord) -> JobRecord:
        return replace(record, checkpoint=deepcopy(record.checkpoint), partial_errors=deepcopy(record.partial_errors))

    @staticmethod
    def _clone_target(record: CollectionTargetRecord) -> CollectionTargetRecord:
        return replace(record, config=deepcopy(record.config), coverage=deepcopy(record.coverage))

    @staticmethod
    def _clone_request(record: CollectionRequestRecord) -> CollectionRequestRecord:
        return replace(record, request_config=deepcopy(record.request_config))

    @staticmethod
    def _clone_subscription(record: CollectionSubscriptionRecord) -> CollectionSubscriptionRecord:
        return replace(record, display_config=deepcopy(record.display_config))

    def _source_with_target(self, record: SourceRecord) -> SourceRecord:
        latest_candidates = [
            job
            for job in self._jobs.values()
            if (record.target_id and job.target_id == record.target_id)
            or (not record.target_id and job.source_id == record.id)
        ]
        latest_job = max(latest_candidates, key=lambda job: job.created_at, default=None)
        source = replace(self._clone_source(record), latest_job=self._clone_job(latest_job) if latest_job else None)
        if not record.target_id or record.target_id not in self._targets:
            return source
        target = self._targets[record.target_id]
        return replace(
            source,
            canonical_key=target.canonical_key,
            coverage=deepcopy(target.coverage),
            last_completed_at=target.last_completed_at,
        )

    def _subscription_source_locked(self, subscription: CollectionSubscriptionRecord) -> SourceRecord:
        """Project a user subscription into the stable public ``SourceRecord`` DTO."""

        target = self._targets.get(subscription.target_id)
        if not target:
            raise NotFoundError(f"Collection target '{subscription.target_id}' was not found")
        latest_job = max(
            (job for job in self._jobs.values() if job.target_id == target.id),
            key=lambda job: job.created_at,
            default=None,
        )
        # Store the complete request config in display_config.  Older/backfilled
        # rows can be empty, in which case target config is the safe fallback.
        config = subscription.display_config or target.config
        return SourceRecord(
            id=subscription.id,
            type=target.type,
            config=deepcopy(config),
            enabled=subscription.enabled,
            created_at=subscription.created_at,
            updated_at=subscription.updated_at,
            target_id=target.id,
            canonical_key=target.canonical_key,
            coverage=deepcopy(target.coverage),
            last_completed_at=target.last_completed_at,
            latest_job=self._clone_job(latest_job) if latest_job else None,
        )

    def _subscription_for_source_locked(
        self, source_id: str, *, owner_id: str | None = None
    ) -> CollectionSubscriptionRecord | None:
        subscription = self._subscriptions.get(source_id)
        if subscription and (owner_id is None or subscription.user_id == owner_id):
            return subscription
        return None

    def _worker_source_id_locked(self, source_id: str, *, owner_id: str | None = None) -> str:
        subscription = self._subscription_for_source_locked(source_id, owner_id=owner_id)
        if subscription:
            primary = self._primary_source_for_target_locked(subscription.target_id)
            if primary:
                return primary
            raise RepositoryError(f"Target '{subscription.target_id}' has no worker source")
        if source_id not in self._sources:
            raise NotFoundError(f"Source '{source_id}' was not found")
        if owner_id is not None and (
            self._sources[source_id].target_id is not None
            or self._source_owners.get(source_id) not in {None, owner_id}
        ):
            raise NotFoundError(f"Source '{source_id}' was not found")
        return source_id

    def _sync_pin_for_target_locked(self, target_id: str) -> None:
        """Keep target refresh active only while someone has it enabled."""

        target = self._targets.get(target_id)
        if not target:
            return
        has_enabled_subscription = any(
            subscription.target_id == target_id and subscription.enabled
            for subscription in self._subscriptions.values()
        )
        pin = self._pins.get(target_id)
        if not has_enabled_subscription:
            if pin:
                pin["enabled"] = False
            return
        if pin:
            pin["enabled"] = True
            pin["next_run_at"] = utcnow()
        elif target.type is SourceType.CHANNEL:
            self._pins[target_id] = {
                "target_id": target_id,
                "enabled": True,
                "interval_minutes": 360,
                "next_run_at": utcnow(),
                "last_dispatched_at": None,
            }

    @staticmethod
    def _source_coverage_rank(record: SourceRecord) -> tuple[int, int, int, int, datetime]:
        config = record.config
        return (
            int(bool(config.get("includeComments", False))),
            int(config.get("maxVideos") or 0),
            int(config.get("maxPagesPerRun") or 0),
            int(config.get("maxCommentPagesPerVideo") or 0),
            record.created_at,
        )

    def _primary_source_for_target_locked(self, target_id: str) -> str | None:
        candidate_ids = [
            request.source_id
            for request in self._requests.values()
            if request.target_id == target_id and request.source_id and request.source_id in self._sources
        ]
        if not candidate_ids:
            candidate_ids = [source.id for source in self._sources.values() if source.target_id == target_id]
        return max(candidate_ids, key=lambda identifier: self._source_coverage_rank(self._sources[identifier]), default=None)

    def bootstrap_runtime_config(
        self, *, environment: str, google_project_number: str, secret_ref: str, key_fingerprint: str | None
    ) -> str:
        with self._lock:
            for identifier, config in self._runtime_configs.items():
                if config["environment"] == environment and config["google_project_number"] == google_project_number:
                    config.update(secret_ref=secret_ref, key_fingerprint=key_fingerprint, status="active")
                    return identifier
            identifier = new_id()
            self._runtime_configs[identifier] = {
                "environment": environment,
                "google_project_number": google_project_number,
                "secret_ref": secret_ref,
                "key_fingerprint": key_fingerprint,
                "status": "active",
            }
            return identifier

    def sync_runtime_keys(self, *, runtime_config_id: str, api_keys: tuple[str, ...], encryption_key: str) -> None:
        # Test repository intentionally retains fingerprints only.
        for key in api_keys:
            self._runtime_configs.setdefault(runtime_config_id, {}).setdefault("key_fingerprints", []).append(hashlib.sha256(key.encode()).hexdigest()[:24])

    def record_runtime_key_state(self, *, runtime_config_id: str | None, key_fingerprint: str, error_reason: str | None = None) -> None:
        return None

    def get_subscription(
        self, subscription_id: str, *, owner_id: str | None = None
    ) -> CollectionSubscriptionRecord:
        with self._lock:
            subscription = self._subscription_for_source_locked(subscription_id, owner_id=owner_id)
            if not subscription:
                raise NotFoundError(f"Subscription '{subscription_id}' was not found")
            return self._clone_subscription(subscription)

    def list_subscriptions(self, *, owner_id: str) -> list[CollectionSubscriptionRecord]:
        with self._lock:
            return [
                self._clone_subscription(subscription)
                for subscription in sorted(self._subscriptions.values(), key=lambda item: item.created_at)
                if subscription.user_id == owner_id
            ]

    def ensure_subscription(
        self, *, owner_id: str, target_id: str, display_config: dict[str, Any]
    ) -> CollectionSubscriptionRecord:
        with self._lock:
            if target_id not in self._targets:
                raise NotFoundError(f"Collection target '{target_id}' was not found")
            key = (owner_id, target_id)
            subscription_id = self._subscription_ids_by_user_target.get(key)
            if subscription_id:
                existing = self._subscriptions[subscription_id]
                # Preserve a user's original display choices unless a new request
                # supplies a non-empty config (for example a normalized handle).
                updated = replace(
                    existing,
                    display_config=deepcopy(display_config) if display_config else existing.display_config,
                    enabled=True,
                    updated_at=utcnow(),
                )
                self._subscriptions[subscription_id] = updated
                self._sync_pin_for_target_locked(target_id)
                return self._clone_subscription(updated)
            now = utcnow()
            created = CollectionSubscriptionRecord(
                id=new_id(),
                user_id=owner_id,
                target_id=target_id,
                display_config=deepcopy(display_config),
                enabled=True,
                created_at=now,
                updated_at=now,
            )
            self._subscriptions[created.id] = created
            self._subscription_ids_by_user_target[key] = created.id
            self._sync_pin_for_target_locked(target_id)
            return self._clone_subscription(created)

    def create_source(self, *, source_type: SourceType, config: dict[str, Any], owner_id: str | None = None) -> SourceRecord:
        with self._lock:
            now = utcnow()
            record = SourceRecord(
                id=new_id(), type=source_type, config=deepcopy(config), enabled=True, created_at=now, updated_at=now
            )
            self._sources[record.id] = record
            if owner_id:
                self._source_owners[record.id] = owner_id
            self._source_videos[record.id] = set()
            return self._clone_source(record)

    def get_source(self, source_id: str, *, owner_id: str | None = None) -> SourceRecord:
        with self._lock:
            subscription = self._subscription_for_source_locked(source_id, owner_id=owner_id)
            if subscription:
                return self._subscription_source_locked(subscription)
            if source_id in self._subscriptions:
                raise NotFoundError(f"Source '{source_id}' was not found")
            try:
                if owner_id is not None and (
                    self._sources[source_id].target_id is not None
                    or self._source_owners.get(source_id) not in {None, owner_id}
                ):
                    raise KeyError(source_id)
                return self._source_with_target(self._sources[source_id])
            except KeyError as exc:
                raise NotFoundError(f"Source '{source_id}' was not found") from exc

    def list_sources(self, *, owner_id: str | None = None) -> list[SourceRecord]:
        with self._lock:
            if owner_id is not None:
                visible = [
                    self._subscription_source_locked(subscription)
                    for subscription in sorted(self._subscriptions.values(), key=lambda item: item.created_at)
                    if subscription.user_id == owner_id
                ]
                # Untargeted legacy sources are still user-owned objects.  A
                # target-backed worker source is intentionally never exposed by
                # an authenticated Source route; its subscription is the DTO.
                legacy = [
                    record
                    for record in self._sources.values()
                    if self._source_owners.get(record.id) == owner_id
                    and record.target_id is None
                ]
                visible.extend(
                    self._source_with_target(record)
                    for record in sorted(legacy, key=lambda item: item.created_at)
                )
                return visible
            records = sorted(self._sources.values(), key=lambda item: item.created_at)
            # A target may retain several legacy source rows for audit history.  The
            # first source linked to it is the stable compatibility source exposed to
            # the dashboard; raw legacy sources without a target remain visible.
            primary_by_target: dict[str, str] = {}
            for record in records:
                if not record.target_id:
                    continue
                candidate = primary_by_target.get(record.target_id)
                if candidate is None or self._source_coverage_rank(record) > self._source_coverage_rank(self._sources[candidate]):
                    primary_by_target[record.target_id] = record.id
            seen_targets: set[str] = set()
            visible: list[SourceRecord] = []
            for record in records:
                if record.target_id:
                    if record.target_id in seen_targets or primary_by_target.get(record.target_id) != record.id:
                        continue
                    seen_targets.add(record.target_id)
                visible.append(self._source_with_target(record))
            return visible

    def update_source(self, source_id: str, *, owner_id: str | None = None, **changes: Any) -> SourceRecord:
        allowed = {"enabled", "config", "next_run_at"}
        unknown = changes.keys() - allowed
        if unknown:
            raise RepositoryError(f"Unsupported source changes: {', '.join(sorted(unknown))}")
        with self._lock:
            subscription = self._subscription_for_source_locked(source_id, owner_id=owner_id)
            if subscription:
                values: dict[str, Any] = {"updated_at": utcnow()}
                if "enabled" in changes:
                    values["enabled"] = bool(changes["enabled"])
                if "config" in changes:
                    values["display_config"] = deepcopy(changes["config"])
                # next_run_at is target scheduling state, never a per-user
                # subscription setting.  Preserve API compatibility as a no-op.
                updated_subscription = replace(subscription, **values)
                self._subscriptions[source_id] = updated_subscription
                self._sync_pin_for_target_locked(subscription.target_id)
                return self._subscription_source_locked(updated_subscription)
            if source_id in self._subscriptions:
                raise NotFoundError(f"Source '{source_id}' was not found")
            record = self.get_source(source_id, owner_id=owner_id)
            values = dict(changes)
            if "config" in values:
                values["config"] = deepcopy(values["config"])
            values["updated_at"] = utcnow()
            updated = replace(record, **values)
            self._sources[source_id] = updated
            return self._source_with_target(updated)

    def delete_source(self, source_id: str, *, owner_id: str | None = None) -> None:
        with self._lock:
            subscription = self._subscription_for_source_locked(source_id, owner_id=owner_id)
            if subscription:
                # A removed subscription must not let a later browser retry
                # replay the old request into a now-inaccessible worker source.
                # Keep the audit row and user attribution, but consume its
                # idempotency key so an explicit re-add creates a fresh
                # subscription/request pair.
                for identifier, request in list(self._requests.items()):
                    if request.subscription_id == subscription.id:
                        self._requests[identifier] = replace(
                            request,
                            subscription_id=None,
                            idempotency_key=None,
                            updated_at=utcnow(),
                        )
                self._subscriptions.pop(source_id, None)
                self._subscription_ids_by_user_target.pop((subscription.user_id, subscription.target_id), None)
                self._sync_pin_for_target_locked(subscription.target_id)
                return
            if source_id in self._subscriptions:
                raise NotFoundError(f"Source '{source_id}' was not found")
            source = self._sources.get(source_id)
            if not source:
                raise NotFoundError(f"Source '{source_id}' was not found")
            if owner_id is not None and (source.target_id is not None or self._source_owners.get(source_id) not in {None, owner_id}):
                raise NotFoundError(f"Source '{source_id}' was not found")
            source_ids = [
                identifier for identifier, candidate in self._sources.items()
                if candidate.target_id == source.target_id
            ] if source.target_id else [source_id]
            for identifier in source_ids:
                self._sources.pop(identifier, None)
                self._source_owners.pop(identifier, None)
                self._source_videos.pop(identifier, None)
                self._analysis.pop(identifier, None)
            for job_id in [job.id for job in self._jobs.values() if job.source_id in source_ids]:
                del self._jobs[job_id]
            if source.target_id:
                target = self._targets.pop(source.target_id, None)
                self._target_videos.pop(source.target_id, None)
                self._pins.pop(source.target_id, None)
                if target:
                    self._target_ids_by_key.pop((target.type, target.canonical_key), None)
                for alias, target_id in list(self._target_aliases.items()):
                    if target_id == source.target_id:
                        del self._target_aliases[alias]
                for request_id, request in list(self._requests.items()):
                    if request.target_id == source.target_id:
                        del self._requests[request_id]

    def source_owned_by(self, *, source_id: str, owner_id: str) -> bool:
        with self._lock:
            subscription = self._subscriptions.get(source_id)
            if subscription:
                return subscription.user_id == owner_id
            source = self._sources.get(source_id)
            return bool(source and source.target_id is None and self._source_owners.get(source_id) == owner_id)

    def target_owned_by(self, *, target_id: str, owner_id: str) -> bool:
        with self._lock:
            return (owner_id, target_id) in self._subscription_ids_by_user_target

    def job_owned_by(self, *, job_id: str, owner_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return False
            if job.target_id:
                return self.target_owned_by(target_id=job.target_id, owner_id=owner_id)
            source = self._sources.get(job.source_id)
            return bool(source and source.target_id is None and self._source_owners.get(job.source_id) == owner_id)

    def owner_has_sources(self, *, owner_id: str) -> bool:
        with self._lock:
            return any(subscription.user_id == owner_id for subscription in self._subscriptions.values()) or any(
                value == owner_id and self._sources.get(source_id) and self._sources[source_id].target_id is None
                for source_id, value in self._source_owners.items()
            )

    def subscription_target_ids(self, *, owner_id: str, enabled_only: bool = True) -> set[str]:
        with self._lock:
            return {
                subscription.target_id
                for subscription in self._subscriptions.values()
                if subscription.user_id == owner_id and (subscription.enabled or not enabled_only)
            }

    def assign_source_owner(self, *, source_id: str, owner_id: str) -> None:
        """Compatibility shim for older route code.

        New submissions create the subscription atomically.  We retain this for
        legacy ``POST /sources`` callers without changing shared target ownership.
        """

        with self._lock:
            if source_id in self._subscriptions:
                return
            if source_id in self._sources:
                self._source_owners.setdefault(source_id, owner_id)

    def create_job(
        self,
        *,
        source_id: str,
        include_comments: bool,
        max_videos: int | None,
        max_comments_per_video: int | None,
        owner_id: str | None = None,
        runtime_config_id: str | None = None,
    ) -> JobRecord:
        with self._lock:
            worker_source_id = self._worker_source_id_locked(source_id, owner_id=owner_id)
            self.get_source(worker_source_id)
            target_id = self._sources[worker_source_id].target_id
            if target_id:
                active = next(
                    (
                        job
                        for job in sorted(self._jobs.values(), key=lambda item: item.created_at)
                        if job.target_id == target_id and not job.state.is_terminal
                    ),
                    None,
                )
                if active:
                    return self._clone_job(active)
            now = utcnow()
            record = JobRecord(
                id=new_id(),
                source_id=worker_source_id,
                state=JobState.QUEUED,
                current_stage="queued",
                progress_completed=0,
                progress_total=None,
                progress_unit="sources",
                include_comments=include_comments,
                max_videos=max_videos,
                max_comments_per_video=max_comments_per_video,
                runtime_config_id=runtime_config_id,
                created_at=now,
                updated_at=now,
                target_id=target_id,
            )
            self._jobs[record.id] = record
            return self._clone_job(record)

    @staticmethod
    def _desired_coverage(source_type: SourceType, config: dict[str, Any]) -> dict[str, Any]:
        """Return only collection breadth, never target identity or display filters."""

        desired: dict[str, Any] = {
            "complete": False,
            "includeComments": bool(config.get("includeComments", False)),
            "collectAllComments": bool(config.get("includeComments", False) and config.get("collectAllComments", False)),
            "maxCommentPagesPerVideo": int(config.get("maxCommentPagesPerVideo") or 1),
        }
        if source_type is SourceType.CHANNEL:
            desired["collectAllVideos"] = bool(config.get("collectAllVideos", False))
            desired["maxVideos"] = int(config.get("maxVideos") or 50)
        elif source_type is SourceType.KEYWORD:
            desired["maxPagesPerRun"] = int(config.get("maxPagesPerRun") or 1)
        return desired

    @staticmethod
    def _merge_config(source_type: SourceType, current: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
        """Monotonically widen shared collection coverage without changing identity."""

        merged = deepcopy(current)
        # The original canonical input is retained so worker compatibility remains
        # stable.  All non-coverage fields are part of the target canonical key.
        for key, value in incoming.items():
            merged.setdefault(key, deepcopy(value))
        merged["includeComments"] = bool(current.get("includeComments", False) or incoming.get("includeComments", False))
        merged["collectAllComments"] = bool(
            current.get("collectAllComments", False) or incoming.get("collectAllComments", False)
        )
        merged["maxCommentPagesPerVideo"] = max(
            int(current.get("maxCommentPagesPerVideo") or 1), int(incoming.get("maxCommentPagesPerVideo") or 1)
        )
        if source_type is SourceType.CHANNEL:
            merged["collectAllVideos"] = bool(current.get("collectAllVideos", False) or incoming.get("collectAllVideos", False))
            merged["maxVideos"] = max(int(current.get("maxVideos") or 1), int(incoming.get("maxVideos") or 1))
        elif source_type is SourceType.KEYWORD:
            merged["maxPagesPerRun"] = max(int(current.get("maxPagesPerRun") or 1), int(incoming.get("maxPagesPerRun") or 1))
        return merged

    @staticmethod
    def _coverage_satisfies(coverage: dict[str, Any], desired: dict[str, Any]) -> bool:
        if not coverage.get("complete"):
            return False
        if desired.get("includeComments") and not coverage.get("includeComments"):
            return False
        if desired.get("collectAllComments") and not coverage.get("collectAllComments"):
            return False
        if desired.get("collectAllVideos") and not coverage.get("collectAllVideos"):
            return False
        for key in ("maxVideos", "maxPagesPerRun"):
            if key in desired and int(coverage.get(key) or 0) < int(desired[key]):
                return False
        if desired.get("includeComments") and int(coverage.get("maxCommentPagesPerVideo") or 0) < int(
            desired.get("maxCommentPagesPerVideo") or 1
        ):
            return False
        return True

    @staticmethod
    def _job_coverage(job: JobRecord, source_type: SourceType, source_config: dict[str, Any]) -> dict[str, Any]:
        coverage = {
            "complete": False,
            "includeComments": bool(job.include_comments),
            "collectAllComments": bool(job.include_comments and source_config.get("collectAllComments")),
            "maxCommentPagesPerVideo": int(job.max_comments_per_video or source_config.get("maxCommentPagesPerVideo") or 1),
        }
        if source_type is SourceType.CHANNEL:
            coverage["collectAllVideos"] = bool(source_config.get("collectAllVideos"))
            coverage["maxVideos"] = int(job.max_videos or source_config.get("maxVideos") or 50)
        elif source_type is SourceType.KEYWORD:
            coverage["maxPagesPerRun"] = int(source_config.get("maxPagesPerRun") or 1)
        return coverage

    def _create_target_job_locked(
        self, *, target_id: str, source: SourceRecord, runtime_config_id: str | None
    ) -> JobRecord:
        desired = self._desired_coverage(source.type, source.config)
        now = utcnow()
        record = JobRecord(
            id=new_id(),
            source_id=source.id,
            target_id=target_id,
            state=JobState.QUEUED,
            current_stage="queued",
            progress_completed=0,
            progress_total=None,
            progress_unit="sources",
            include_comments=bool(desired["includeComments"]),
            max_videos=desired.get("maxVideos"),
            max_comments_per_video=desired.get("maxCommentPagesPerVideo"),
            runtime_config_id=runtime_config_id,
            created_at=now,
            updated_at=now,
        )
        self._jobs[record.id] = record
        return record

    def _submission_from_request_locked(self, request: CollectionRequestRecord) -> CollectionSubmission:
        target = self._targets[request.target_id]
        if request.subscription_id:
            subscription = self._subscriptions.get(request.subscription_id)
            if not subscription:
                raise RepositoryError(f"Request '{request.id}' points to a missing subscription")
            source = self._subscription_source_locked(subscription)
        else:
            source_id = request.source_id
            if source_id is None:
                source_id = self._primary_source_for_target_locked(target.id)
            if source_id is None:
                raise RepositoryError(f"Target '{target.id}' has no worker source")
            source = self._source_with_target(self._sources[source_id])
        job = self._clone_job(self._jobs[request.job_id]) if request.job_id and request.job_id in self._jobs else None
        if request.job_id is None and request.status == "queued":
            disposition = "successor_queued"
        elif request.status == "completed":
            disposition = "cached"
        elif request.status == "joined":
            disposition = "joined"
        else:
            disposition = "queued"
        return CollectionSubmission(
            request=self._clone_request(request),
            target=self._clone_target(target),
            source=source,
            job=job,
            disposition=disposition,
        )

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
    ) -> CollectionSubmission:
        with self._lock:
            target_id = next(
                (self._target_aliases[(source_type, kind, value)] for kind, value in aliases if (source_type, kind, value) in self._target_aliases),
                None,
            )
            target_id = target_id or self._target_ids_by_key.get((source_type, canonical_key))
            if target_id is None:
                now = utcnow()
                target = CollectionTargetRecord(
                    id=new_id(),
                    type=source_type,
                    canonical_key=canonical_key,
                    config=deepcopy(config),
                    coverage={},
                    resolved_channel_id=None,
                    last_completed_at=None,
                    created_at=now,
                    updated_at=now,
                )
                self._targets[target.id] = target
                self._target_ids_by_key[(source_type, canonical_key)] = target.id
            else:
                target = self._targets[target_id]

            # Idempotency is scoped to a user's logical action for one canonical
            # target.  The same header value must not replay a different target
            # or another user's request.
            if idempotency_key:
                existing = next(
                    (
                        item
                        for item in self._requests.values()
                        if item.idempotency_key == idempotency_key
                        and item.user_id == owner_id
                        and item.target_id == target.id
                    ),
                    None,
                )
                if existing:
                    return self._submission_from_request_locked(existing)

            for kind, value in aliases:
                self._target_aliases[(source_type, kind, value)] = target.id

            primary_source_id = self._primary_source_for_target_locked(target.id)
            if primary_source_id is None:
                now = utcnow()
                primary = SourceRecord(
                    id=new_id(),
                    type=source_type,
                    config=deepcopy(config),
                    enabled=True,
                    created_at=now,
                    updated_at=now,
                    target_id=target.id,
                )
                self._sources[primary.id] = primary
                self._source_videos[primary.id] = set()
                primary_source_id = primary.id
            primary = self._sources[primary_source_id]
            prior_config = deepcopy(primary.config)
            merged_config = self._merge_config(source_type, prior_config, target.config)
            merged_config = self._merge_config(source_type, merged_config, config)
            primary = replace(primary, config=merged_config, updated_at=utcnow())
            self._sources[primary.id] = primary
            target = replace(target, config=deepcopy(merged_config), updated_at=utcnow())
            self._targets[target.id] = target

            subscription: CollectionSubscriptionRecord | None = None
            if owner_id:
                # This runs under the target lock, mirroring the database's
                # INSERT .. ON CONFLICT (user_id, target_id) contract.
                subscription = self.ensure_subscription(
                    owner_id=owner_id,
                    target_id=target.id,
                    display_config=config,
                )

            desired = self._desired_coverage(source_type, config)
            active = next(
                (job for job in sorted(self._jobs.values(), key=lambda item: item.created_at) if job.target_id == target.id and not job.state.is_terminal),
                None,
            )
            now = utcnow()
            request = CollectionRequestRecord(
                id=new_id(),
                target_id=target.id,
                source_id=primary.id,
                request_config=deepcopy(config),
                idempotency_key=idempotency_key,
                job_id=None,
                status="queued",
                created_at=now,
                updated_at=now,
                user_id=owner_id,
                subscription_id=subscription.id if subscription else None,
            )

            if not force_refresh and self._coverage_satisfies(target.coverage, desired):
                request = replace(request, status="completed")
            elif active and self._coverage_satisfies(
                self._job_coverage(active, source_type, prior_config), desired
            ):
                request = replace(request, job_id=active.id, status="joined")
            elif active and active.state is JobState.QUEUED:
                active_desired = self._desired_coverage(source_type, self._sources[active.source_id].config)
                active = replace(
                    active,
                    include_comments=bool(active_desired["includeComments"]),
                    max_videos=active_desired.get("maxVideos"),
                    max_comments_per_video=active_desired.get("maxCommentPagesPerVideo"),
                    updated_at=utcnow(),
                )
                self._jobs[active.id] = active
                request = replace(request, job_id=active.id, status="queued")
            elif active:
                # A running job has an immutable scope.  The queued request is
                # materialized as one successor when that job becomes terminal.
                request = replace(request, status="queued")
            else:
                job = self._create_target_job_locked(target_id=target.id, source=primary, runtime_config_id=runtime_config_id)
                request = replace(request, job_id=job.id, status="queued")

            self._requests[request.id] = request
            return self._submission_from_request_locked(request)

    def promote_channel_target(
        self, *, source_id: str, youtube_channel_id: str, handle: str | None = None
    ) -> CollectionTargetRecord | None:
        with self._lock:
            source = self._sources.get(source_id)
            if not source or not source.target_id:
                return None
            current = self._targets.get(source.target_id)
            if not current or current.type is not SourceType.CHANNEL:
                return None
            canonical_key = f"channel:{youtube_channel_id}"
            existing_id = self._target_ids_by_key.get((SourceType.CHANNEL, canonical_key))
            target = current
            if existing_id and existing_id != current.id:
                target = self._targets[existing_id]
                existing_active = any(job.target_id == target.id and not job.state.is_terminal for job in self._jobs.values())
                for identifier, candidate in list(self._sources.items()):
                    if candidate.target_id == current.id:
                        self._sources[identifier] = replace(candidate, target_id=target.id, updated_at=utcnow())
                merged_subscription_ids: dict[str, str] = {}
                for identifier, subscription in list(self._subscriptions.items()):
                    if subscription.target_id != current.id:
                        continue
                    existing_identifier = self._subscription_ids_by_user_target.get((subscription.user_id, target.id))
                    if existing_identifier and existing_identifier != identifier:
                        existing = self._subscriptions[existing_identifier]
                        self._subscriptions[existing_identifier] = replace(
                            existing,
                            enabled=existing.enabled or subscription.enabled,
                            updated_at=utcnow(),
                        )
                        self._subscriptions.pop(identifier, None)
                        self._subscription_ids_by_user_target.pop((subscription.user_id, current.id), None)
                        merged_subscription_ids[identifier] = existing_identifier
                    else:
                        self._subscriptions[identifier] = replace(subscription, target_id=target.id, updated_at=utcnow())
                        self._subscription_ids_by_user_target.pop((subscription.user_id, current.id), None)
                        self._subscription_ids_by_user_target[(subscription.user_id, target.id)] = identifier
                canonical_idempotency_keys = {
                    (candidate.user_id, candidate.idempotency_key)
                    for candidate in self._requests.values()
                    if candidate.target_id == target.id and candidate.idempotency_key
                }
                for identifier, candidate in list(self._requests.items()):
                    if candidate.target_id == current.id:
                        # A handle target and an already-resolved UC target can
                        # each have accepted the same user retry key before
                        # promotion.  Keep the canonical target's key and make
                        # the older provisional audit row non-replayable, just
                        # as the PostgreSQL promotion transaction does.
                        idempotency_key = candidate.idempotency_key
                        if (candidate.user_id, idempotency_key) in canonical_idempotency_keys:
                            idempotency_key = None
                        self._requests[identifier] = replace(
                            candidate,
                            target_id=target.id,
                            subscription_id=merged_subscription_ids.get(candidate.subscription_id, candidate.subscription_id),
                            idempotency_key=idempotency_key,
                            updated_at=utcnow(),
                        )
                for identifier, candidate in list(self._jobs.items()):
                    if candidate.target_id == current.id:
                        self._jobs[identifier] = replace(
                            candidate,
                            target_id=None if existing_active and not candidate.state.is_terminal else target.id,
                            updated_at=utcnow(),
                        )
                for alias, value in list(self._target_aliases.items()):
                    if value == current.id:
                        self._target_aliases[alias] = target.id
                self._target_videos.setdefault(target.id, set()).update(self._target_videos.pop(current.id, set()))
                current_pin = self._pins.pop(current.id, None)
                if current_pin and target.id not in self._pins:
                    self._pins[target.id] = {**current_pin, "target_id": target.id}
                self._target_ids_by_key.pop((current.type, current.canonical_key), None)
                self._targets.pop(current.id, None)
                self._sync_pin_for_target_locked(target.id)
            else:
                self._target_ids_by_key.pop((current.type, current.canonical_key), None)
                target = replace(current, canonical_key=canonical_key, updated_at=utcnow())
                self._targets[target.id] = target
                self._target_ids_by_key[(SourceType.CHANNEL, canonical_key)] = target.id
            self._target_aliases[(SourceType.CHANNEL, "channel_id", youtube_channel_id)] = target.id
            if handle:
                self._target_aliases[(SourceType.CHANNEL, "handle", handle.casefold())] = target.id
            return self._clone_target(target)

    def get_job(self, job_id: str, *, owner_id: str | None = None) -> JobRecord:
        with self._lock:
            try:
                if owner_id is not None and not self.job_owned_by(job_id=job_id, owner_id=owner_id):
                    raise KeyError(job_id)
                return self._clone_job(self._jobs[job_id])
            except KeyError as exc:
                raise NotFoundError(f"Job '{job_id}' was not found") from exc

    def list_jobs_for_source(
        self, source_id: str, *, limit: int = 20, owner_id: str | None = None
    ) -> list[JobRecord]:
        with self._lock:
            source = self.get_source(source_id, owner_id=owner_id)
            jobs = [
                job for job in self._jobs.values()
                if (source.target_id and job.target_id == source.target_id)
                or (not source.target_id and job.source_id == source_id)
            ]
            return [self._clone_job(job) for job in sorted(jobs, key=lambda item: item.updated_at, reverse=True)[:limit]]

    def transition_job(self, job_id: str, state: JobState, **changes: Any) -> JobRecord:
        allowed = {
            "current_stage",
            "progress_completed",
            "progress_total",
            "progress_unit",
            "pause_reason",
            "quota_bucket",
            "resume_at",
            "resume_is_automatic",
            "checkpoint",
            "partial_errors",
            "lease_owner",
            "lease_expires_at",
        }
        unknown = changes.keys() - allowed
        if unknown:
            raise RepositoryError(f"Unsupported job changes: {', '.join(sorted(unknown))}")
        with self._lock:
            record = self.get_job(job_id)
            if state != record.state and state not in _ALLOWED_TRANSITIONS[record.state]:
                raise InvalidStateTransitionError(f"Cannot transition job '{job_id}' from {record.state.value} to {state.value}")
            values = dict(changes)
            for key in ("checkpoint", "partial_errors"):
                if key in values:
                    values[key] = deepcopy(values[key])
            values.update(state=state, updated_at=utcnow())
            updated = replace(record, **values)
            self._jobs[job_id] = updated
            if state.is_terminal:
                for identifier, request in list(self._requests.items()):
                    if request.job_id == job_id:
                        self._requests[identifier] = replace(request, status=state.value, updated_at=utcnow())
                if updated.target_id and updated.target_id in self._targets:
                    target = self._targets[updated.target_id]
                    if state is JobState.COMPLETED:
                        source = self._sources[updated.source_id]
                        coverage = self._job_coverage(updated, source.type, source.config)
                        coverage["complete"] = True
                        self._targets[target.id] = replace(
                            target, coverage=coverage, last_completed_at=utcnow(), updated_at=utcnow()
                        )
                    pending = [
                        request
                        for request in self._requests.values()
                        if request.target_id == updated.target_id and request.job_id is None and request.status == "queued"
                    ]
                    if pending:
                        primary_source_id = self._primary_source_for_target_locked(updated.target_id)
                        source = self._sources.get(primary_source_id) if primary_source_id else None
                        if source:
                            successor = self._create_target_job_locked(
                                target_id=updated.target_id, source=source, runtime_config_id=updated.runtime_config_id
                            )
                            for request in pending:
                                self._requests[request.id] = replace(
                                    request, job_id=successor.id, status="queued", updated_at=utcnow()
                                )
            return self._clone_job(updated)

    def claim_next_job(self, *, worker_id: str, lease_seconds: int = 120) -> JobRecord | None:
        with self._lock:
            now = utcnow()
            candidates = sorted(self._jobs.values(), key=lambda item: item.created_at)
            for record in candidates:
                due_wait = record.state in {JobState.WAITING_RETRY, JobState.WAITING_QUOTA} and record.resume_at is not None and record.resume_at <= now
                recoverable_running = record.state is JobState.RUNNING and record.lease_expires_at is not None and record.lease_expires_at <= now
                available_lease = record.lease_expires_at is None or record.lease_expires_at <= now
                if (record.state is JobState.QUEUED or due_wait or recoverable_running) and available_lease:
                    updated = replace(
                        record,
                        state=JobState.RUNNING,
                        current_stage="reclaimed" if recoverable_running else "claimed",
                        pause_reason=None,
                        quota_bucket=None,
                        resume_at=None,
                        resume_is_automatic=False,
                        lease_owner=worker_id,
                        lease_expires_at=now + timedelta(seconds=lease_seconds),
                        updated_at=now,
                    )
                    self._jobs[record.id] = updated
                    return self._clone_job(updated)
            return None

    def renew_job_lease(self, *, job_id: str, worker_id: str, lease_seconds: int = 120) -> bool:
        with self._lock:
            record = self._jobs.get(job_id)
            if not record or record.state is not JobState.RUNNING or record.lease_owner != worker_id:
                return False
            self._jobs[job_id] = replace(record, lease_expires_at=utcnow() + timedelta(seconds=lease_seconds), updated_at=utcnow())
            return True

    def checkpoint_job(self, job_id: str, checkpoint: dict[str, Any]) -> JobRecord:
        record = self.get_job(job_id)
        return self.transition_job(job_id, record.state, checkpoint=checkpoint)

    def update_job_progress(
        self, job_id: str, *, completed: int, total: int | None, unit: str, current_stage: str | None = None
    ) -> JobRecord:
        record = self.get_job(job_id)
        changes: dict[str, Any] = {"progress_completed": completed, "progress_total": total, "progress_unit": unit}
        if current_stage:
            changes["current_stage"] = current_stage
        return self.transition_job(job_id, record.state, **changes)

    def upsert_channel(self, channel: dict[str, Any]) -> dict[str, Any]:
        youtube_channel_id = str(channel["youtube_channel_id"])
        with self._lock:
            current = deepcopy(self._channels.get(youtube_channel_id, {}))
            current.update({key: value for key, value in deepcopy(channel).items() if value is not None or key not in current})
            self._channels[youtube_channel_id] = current
            return deepcopy(current)

    def upsert_video(self, video: VideoRecord) -> VideoRecord:
        with self._lock:
            current = self._videos.get(video.youtube_video_id)
            stored = replace(video, id=current.id) if current else video
            self._videos[video.youtube_video_id] = stored
            return stored

    def get_videos_by_youtube_ids(self, youtube_video_ids: Iterable[str]) -> dict[str, VideoRecord]:
        with self._lock:
            return {
                video_id: self._videos[video_id]
                for video_id in set(youtube_video_ids)
                if video_id in self._videos
            }

    def count_videos_by_channel(self, youtube_channel_id: str) -> int:
        with self._lock:
            return sum(video.youtube_channel_id == youtube_channel_id for video in self._videos.values())

    def link_source_video(self, source_id: str, youtube_video_id: str) -> None:
        with self._lock:
            source = self.get_source(source_id)
            if youtube_video_id not in self._videos:
                raise NotFoundError(f"Video '{youtube_video_id}' was not found")
            self._source_videos.setdefault(source_id, set()).add(youtube_video_id)
            if source.target_id:
                self._target_videos.setdefault(source.target_id, set()).add(youtube_video_id)

    def source_video_ids(self, source_id: str, youtube_video_ids: Iterable[str]) -> set[str]:
        with self._lock:
            return set(youtube_video_ids).intersection(self._source_videos.get(source_id, set()))

    def count_source_videos(self, source_id: str) -> int:
        with self._lock:
            return len(self._source_videos.get(source_id, set()))

    def upsert_comment(self, comment: CommentRecord) -> CommentRecord:
        with self._lock:
            current = self._comments.get(comment.youtube_comment_id)
            stored = replace(comment, id=current.id) if current else comment
            self._comments[comment.youtube_comment_id] = stored
            return stored

    def get_comment_detail(self, comment_id: str, *, owner_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            comment = self._comments.get(comment_id)
            if not comment:
                raise NotFoundError(f"Comment '{comment_id}' was not found")
            visible_video_ids = self._visible_video_ids_locked(owner_id)
            if visible_video_ids is not None and comment.youtube_video_id not in visible_video_ids:
                raise NotFoundError(f"Comment '{comment_id}' was not found")
            video = self._videos.get(comment.youtube_video_id)
            if not video:
                raise NotFoundError(f"Video '{comment.youtube_video_id}' was not found")
            replies = [
                item
                for item in self._comments.values()
                if item.youtube_parent_comment_id == comment.youtube_comment_id
                and (visible_video_ids is None or item.youtube_video_id in visible_video_ids)
            ]
            # Render a thread in conversation order.  ``source_fetched_at`` is
            # a deterministic fallback for older records without a publish date.
            replies.sort(key=lambda item: (item.published_at or item.source_fetched_at, item.youtube_comment_id))
            reply_ids = {item.youtube_comment_id for item in replies}
            author_comments = []
            if comment.author_channel_id:
                for item in self._comments.values():
                    if (
                        item.youtube_comment_id == comment.youtube_comment_id
                        or item.youtube_comment_id in reply_ids
                        or item.author_channel_id != comment.author_channel_id
                    ):
                        continue
                    if visible_video_ids is not None and item.youtube_video_id not in visible_video_ids:
                        continue
                    related_video = self._videos.get(item.youtube_video_id)
                    if related_video:
                        channel = self._channels.get(related_video.youtube_channel_id or "", {})
                        author_comments.append({"comment": item, "video": related_video, "channel_title": channel.get("title")})
            author_comments.sort(key=lambda item: item["comment"].published_at or utcnow(), reverse=True)
            return {
                "comment": comment,
                "video": video,
                "replies": replies,
                "author_comments": author_comments[:50],
            }

    def existing_comment_ids(self, youtube_comment_ids: Iterable[str]) -> set[str]:
        with self._lock:
            return set(youtube_comment_ids).intersection(self._comments)

    def comment_counts_by_video(self, youtube_video_ids: Iterable[str]) -> dict[str, int]:
        """Return persisted comment totals for the requested YouTube videos."""

        with self._lock:
            requested = set(youtube_video_ids)
            counts = {video_id: 0 for video_id in requested}
            for comment in self._comments.values():
                if comment.youtube_video_id in counts:
                    counts[comment.youtube_video_id] += 1
            return counts

    def record_api_request(
        self, *, job_id: str, bucket: QuotaBucket, endpoint: str, status_code: int, error_reason: str | None = None
    ) -> None:
        with self._lock:
            self._request_logs.append(
                {
                    "job_id": job_id,
                    "bucket": bucket.value,
                    "endpoint": endpoint,
                    "status_code": status_code,
                    "error_reason": error_reason,
                    "occurred_at": utcnow(),
                }
            )

    def set_target_pin(self, *, target_id: str, enabled: bool, interval_minutes: int) -> dict[str, Any]:
        with self._lock:
            if target_id not in self._targets:
                raise NotFoundError(f"Collection target '{target_id}' was not found")
            current = self._pins.get(target_id, {})
            now = utcnow()
            pin = {
                "target_id": target_id, "enabled": enabled, "interval_minutes": interval_minutes,
                "next_run_at": now if enabled else current.get("next_run_at", now),
                "last_dispatched_at": current.get("last_dispatched_at"),
            }
            self._pins[target_id] = pin
            return deepcopy(pin)

    def get_target_pin(self, *, target_id: str) -> dict[str, Any] | None:
        with self._lock:
            pin = self._pins.get(target_id)
            return deepcopy(pin) if pin else None

    def dispatch_due_pins(self, *, runtime_config_id: str | None = None, limit: int = 10) -> int:
        with self._lock:
            now = utcnow()
            dispatched = 0
            for target_id, pin in sorted(self._pins.items(), key=lambda item: item[1]["next_run_at"]):
                if dispatched >= limit or not pin["enabled"] or pin["next_run_at"] > now:
                    continue
                if not any(
                    subscription.target_id == target_id and subscription.enabled
                    for subscription in self._subscriptions.values()
                ):
                    pin["enabled"] = False
                    continue
                active = any(job.target_id == target_id and not job.state.is_terminal for job in self._jobs.values())
                if not active:
                    source_id = self._primary_source_for_target_locked(target_id)
                    if source_id:
                        self._create_target_job_locked(target_id=target_id, source=self._sources[source_id], runtime_config_id=runtime_config_id)
                        pin["last_dispatched_at"] = now
                        dispatched += 1
                pin["next_run_at"] = now + timedelta(minutes=int(pin["interval_minutes"]))
            return dispatched

    def list_explore(
        self, *, limit: int = 60, channel_id: str | None = None, owner_id: str | None = None
    ) -> dict[str, Any]:
        with self._lock:
            visible_video_ids = self._visible_video_ids_locked(owner_id)
            visible_target_ids = (
                self.subscription_target_ids(owner_id=owner_id, enabled_only=False) if owner_id is not None else None
            )
            channels: list[dict[str, Any]] = []
            for current_channel_id, channel in self._channels.items():
                channel_videos = [
                    video
                    for video in self._videos.values()
                    if video.youtube_channel_id == current_channel_id
                    and (visible_video_ids is None or video.youtube_video_id in visible_video_ids)
                ]
                if owner_id is not None and not channel_videos:
                    continue
                ids = {video.youtube_video_id for video in channel_videos}
                target = next(
                    (
                        target
                        for target in self._targets.values()
                        if target.resolved_channel_id == channel.get("id")
                        and (visible_target_ids is None or target.id in visible_target_ids)
                    ),
                    None,
                )
                pin = self._pins.get(target.id) if target else None
                collected_video_count = len(channel_videos)
                youtube_video_count = int((channel.get("statistics") or {}).get("videoCount") or 0)
                collected_comment_count = sum(1 for comment in self._comments.values() if comment.youtube_video_id in ids)
                youtube_comment_count = sum(int((video.statistics or {}).get("commentCount") or 0) for video in channel_videos)
                channels.append({
                    "youtubeChannelId": current_channel_id, "handle": channel.get("handle"), "title": channel.get("title"),
                    "description": channel.get("description"), "thumbnailUrl": channel.get("thumbnail_url"),
                    "subscriberCount": (channel.get("statistics") or {}).get("subscriberCount"),
                    "viewCount": (channel.get("statistics") or {}).get("viewCount"),
                    "youtubeVideoCount": youtube_video_count,
                    "hiddenSubscriberCount": (channel.get("statistics") or {}).get("hiddenSubscriberCount"),
                    "videoCount": collected_video_count,
                    "commentCount": collected_comment_count,
                    "youtubeCommentCount": youtube_comment_count,
                    "videoCollectionRate": min(100, round((collected_video_count / youtube_video_count) * 100)) if youtube_video_count else 0,
                    "commentCollectionRate": min(100, round((collected_comment_count / youtube_comment_count) * 100)) if youtube_comment_count else 0,
                    "lastFetchedAt": max((video.source_fetched_at for video in channel_videos), default=channel.get("source_fetched_at")),
                    "targetId": target.id if target else None, "pin": deepcopy(pin) if pin else None,
                })
            channels.sort(key=lambda item: item["lastFetchedAt"] or utcnow(), reverse=True)
            videos = [
                video
                for video in self._videos.values()
                if (visible_video_ids is None or video.youtube_video_id in visible_video_ids)
                and (channel_id is None or video.youtube_channel_id == channel_id)
            ]
            videos_by_channel: dict[str, list[VideoRecord]] = {}
            for video in videos:
                videos_by_channel.setdefault(video.youtube_channel_id, []).append(video)
            ordered_buckets = [sorted(bucket, key=lambda item: item.source_fetched_at, reverse=True) for bucket in videos_by_channel.values()]
            videos = []
            while ordered_buckets and len(videos) < limit:
                next_buckets: list[list[VideoRecord]] = []
                for bucket in ordered_buckets:
                    videos.append(bucket.pop(0))
                    if len(videos) >= limit:
                        break
                    if bucket:
                        next_buckets.append(bucket)
                ordered_buckets = next_buckets
            return {"channels": channels, "videos": videos}

    def list_channel_subscriber_history(
        self, *, youtube_channel_id: str, limit: int = 180, owner_id: str | None = None
    ) -> list[dict[str, Any]]:
        with self._lock:
            visible_video_ids = self._visible_video_ids_locked(owner_id)
            if visible_video_ids is not None and not any(
                video.youtube_channel_id == youtube_channel_id and video.youtube_video_id in visible_video_ids
                for video in self._videos.values()
            ):
                raise NotFoundError(f"Channel '{youtube_channel_id}' was not found")
            channel = self._channels.get(youtube_channel_id)
            if not channel or not channel.get("statistics"):
                return []
            return [{
                "fetchedAt": channel.get("source_fetched_at") or utcnow(),
                "subscriberCount": channel["statistics"].get("subscriberCount"),
                "hiddenSubscriberCount": channel["statistics"].get("hiddenSubscriberCount"),
            }]

    def search_collected(self, *, query: str, limit: int = 20, owner_id: str | None = None, scope: str = "all") -> dict[str, Any]:
        with self._lock:
            visible_video_ids = self._visible_video_ids_locked(owner_id)
            video_results: list[dict[str, Any]] = []
            comment_results: list[dict[str, Any]] = []
            if scope in {"all", "videos"}:
                for video in self._videos.values():
                    if visible_video_ids is not None and video.youtube_video_id not in visible_video_ids:
                        continue
                    channel = self._channels.get(video.youtube_channel_id or "", {})
                    score, matched_fields = rank_text_fields(query, {
                        "title": video.title,
                        "description": video.description,
                        "channel": channel.get("title"),
                        "handle": channel.get("handle"),
                    })
                    if matched_fields:
                        video_results.append({"video": video, "score": score, "matched_fields": matched_fields})

            if scope in {"all", "comments"}:
                for comment in self._comments.values():
                    video = self._videos.get(comment.youtube_video_id)
                    if not video:
                        continue
                    if visible_video_ids is not None and video.youtube_video_id not in visible_video_ids:
                        continue
                    channel = self._channels.get(video.youtube_channel_id or "", {})
                    score, matched_fields = rank_text_fields(query, {"comment": comment.text_display})
                    if matched_fields:
                        comment_results.append({
                            "comment": comment, "video": video, "channel_title": channel.get("title"),
                            "score": score, "matched_fields": matched_fields,
                        })

            video_results.sort(key=lambda item: (item["score"], item["video"].source_fetched_at), reverse=True)
            comment_results.sort(key=lambda item: (item["score"], item["comment"].source_fetched_at), reverse=True)
            return {"videos": video_results[:limit], "comments": comment_results[:limit]}

    def _source_video_records(self, source_id: str) -> list[VideoRecord]:
        subscription = self._subscriptions.get(source_id)
        source = self._sources.get(source_id)
        if subscription:
            ids = self._target_videos.get(subscription.target_id, set())
        elif source and source.target_id:
            ids = self._target_videos.get(source.target_id, set())
        else:
            ids = self._source_videos.get(source_id, set())
        return sorted((self._videos[item] for item in ids if item in self._videos), key=lambda item: item.source_fetched_at, reverse=True)

    def _target_video_ids_locked(self, target_ids: Iterable[str]) -> set[str]:
        return {
            youtube_video_id
            for target_id in target_ids
            for youtube_video_id in self._target_videos.get(target_id, set())
        }

    def _visible_video_ids_locked(self, owner_id: str | None) -> set[str] | None:
        if owner_id is None:
            return None
        # Pausing a subscription stops target refresh only; it must not hide
        # previously collected public data from that same user.
        return self._target_video_ids_locked(self.subscription_target_ids(owner_id=owner_id, enabled_only=False))

    def _comments_for_video_ids(self, video_ids: set[str]) -> list[CommentRecord]:
        return sorted(
            (comment for comment in self._comments.values() if comment.youtube_video_id in video_ids),
            key=lambda item: item.published_at or item.source_fetched_at,
            reverse=True,
        )

    def save_analysis_summary(self, source_id: str) -> dict[str, Any]:
        with self._lock:
            videos = self._source_video_records(source_id)
            comments = self._comments_for_video_ids({video.youtube_video_id for video in videos})
            summary = build_summary(videos, comments)
            self._analysis[source_id] = deepcopy(summary)
            return deepcopy(summary)

    def get_source_results(self, source_id: str, *, owner_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            source = self.get_source(source_id, owner_id=owner_id)
            videos = self._source_video_records(source_id)
            comments = self._comments_for_video_ids({video.youtube_video_id for video in videos})
            latest_job = next(
                iter(
                    sorted(
                        (
                            job
                            for job in self._jobs.values()
                            if (source.target_id and job.target_id == source.target_id)
                            or (not source.target_id and job.source_id == source_id)
                        ),
                        key=lambda item: item.created_at,
                        reverse=True,
                    )
                ),
                None,
            )
            summary = deepcopy(self._analysis.get(source_id) or build_summary(videos, comments))
            return {
                "source": source,
                "latest_job": self._clone_job(latest_job) if latest_job else None,
                "videos": list(videos),
                "comments": list(comments),
                "analysis": summary,
            }

    def get_video_comments(self, video_id: str, *, owner_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            video = self._videos.get(video_id)
            if not video:
                raise NotFoundError(f"Video '{video_id}' was not found")
            visible_video_ids = self._visible_video_ids_locked(owner_id)
            if visible_video_ids is not None and video_id not in visible_video_ids:
                raise NotFoundError(f"Video '{video_id}' was not found")
            comments = self._comments_for_video_ids({video_id})
            return {"video": video, "comments": comments, "summary": build_summary([video], comments)}
