"""Thread-safe in-memory persistence adapter for tests and local use."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib
from threading import RLock
from typing import Any, Iterable

from ..analysis import build_summary
from ..collection_policy import (
    coverage_satisfies,
    desired_coverage,
    job_coverage,
    merge_collection_config,
)
from ..domain import (
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
from ..fuzzy_search import normalize_search_text, rank_text_fields
from ..ports import CollectionRepository
from ..repositories import (
    CommentThreadSort,
    InvalidStateTransitionError,
    NotFoundError,
    RepositoryError,
    _ALLOWED_TRANSITIONS,
    _comment_sort_key,
    _comment_thread_sort_key,
    _effective_video_timestamp,
    _explore_video_sort_key,
    _video_sort_key,
    decode_comment_cursor,
    decode_comment_thread_cursor,
    decode_explore_video_cursor,
    decode_source_video_cursor,
    encode_comment_cursor,
    encode_comment_thread_cursor,
    encode_explore_video_cursor,
    encode_source_video_cursor,
    explore_video_filter_hash,
    source_video_filter_hash,
)

from .memory_collection import MemoryCollectionMixin
from .memory_jobs import MemoryJobMixin
from .memory_results import MemoryReadMixin


class InMemoryRepository(
    MemoryJobMixin,
    MemoryCollectionMixin,
    MemoryReadMixin,
    CollectionRepository,
):
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
        self._target_video_first_seen: dict[tuple[str, str], datetime] = {}
        self._pins: dict[str, dict[str, Any]] = {}
        self._runtime_configs: dict[str, dict[str, Any]] = {}
        self._channels: dict[str, dict[str, Any]] = {}
        self._videos: dict[str, VideoRecord] = {}
        self._video_first_seen: dict[str, datetime] = {}
        self._comments: dict[str, CommentRecord] = {}
        self._source_videos: dict[str, set[str]] = {}
        self._source_video_first_seen: dict[tuple[str, str], datetime] = {}
        self._analysis: dict[str, dict[str, Any]] = {}
        self._analysis_metadata: dict[str, dict[str, Any]] = {}
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
            if job.parent_job_id is None
            and (
                (record.target_id and job.target_id == record.target_id)
                or (not record.target_id and job.source_id == record.id)
            )
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
            (
                job
                for job in self._jobs.values()
                if job.target_id == target.id and job.parent_job_id is None
            ),
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
                self._analysis.pop(f"source:{identifier}", None)
                self._analysis_metadata.pop(f"source:{identifier}", None)
                for key in [key for key in self._source_video_first_seen if key[0] == identifier]:
                    self._source_video_first_seen.pop(key, None)
            for job_id in [job.id for job in self._jobs.values() if job.source_id in source_ids]:
                del self._jobs[job_id]
            if source.target_id:
                target = self._targets.pop(source.target_id, None)
                self._target_videos.pop(source.target_id, None)
                self._analysis.pop(f"target:{source.target_id}", None)
                self._analysis_metadata.pop(f"target:{source.target_id}", None)
                for key in [key for key in self._target_video_first_seen if key[0] == source.target_id]:
                    self._target_video_first_seen.pop(key, None)
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
