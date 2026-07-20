"""In-memory target coordination, jobs, leases, and checkpoints."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable

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
    JobRecord,
    JobState,
    SourceRecord,
    SourceType,
    new_id,
    utcnow,
)
from ..repositories import (
    InvalidStateTransitionError,
    NotFoundError,
    RepositoryError,
    _ALLOWED_TRANSITIONS,
)


class MemoryJobMixin:
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

    def enqueue_video_jobs(self, *, parent_job: JobRecord, youtube_video_ids: Iterable[str]) -> int:
        """Fan out one discovery job into idempotent, independently retryable video jobs."""
        with self._lock:
            created = 0
            known = {
                str(job.checkpoint.get("youtubeVideoId"))
                for job in self._jobs.values()
                if job.parent_job_id == parent_job.id
            }
            now = utcnow()
            for youtube_video_id in dict.fromkeys(str(value) for value in youtube_video_ids):
                if not youtube_video_id or youtube_video_id in known:
                    continue
                record = JobRecord(
                    id=new_id(), source_id=parent_job.source_id, state=JobState.QUEUED,
                    current_stage="queued_video", progress_completed=0, progress_total=1,
                    progress_unit="videos", include_comments=parent_job.include_comments,
                    max_videos=1, max_comments_per_video=parent_job.max_comments_per_video,
                    checkpoint={"jobKind": "video", "youtubeVideoId": youtube_video_id},
                    runtime_config_id=parent_job.runtime_config_id, created_at=now, updated_at=now,
                    parent_job_id=parent_job.id,
                )
                self._jobs[record.id] = record
                known.add(youtube_video_id)
                created += 1
            return created

    def child_job_summary(self, *, parent_job_id: str) -> tuple[int, int, int]:
        with self._lock:
            children = [job for job in self._jobs.values() if job.parent_job_id == parent_job_id]
            completed = sum(job.state.is_terminal for job in children)
            failed = sum(job.state in {JobState.FAILED, JobState.CANCELLED} for job in children)
            return len(children), completed, failed

    _desired_coverage = staticmethod(desired_coverage)
    _merge_config = staticmethod(merge_collection_config)
    _coverage_satisfies = staticmethod(coverage_satisfies)
    _job_coverage = staticmethod(job_coverage)

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
                merged_video_ids = self._target_videos.pop(current.id, set())
                self._target_videos.setdefault(target.id, set()).update(merged_video_ids)
                for youtube_video_id in merged_video_ids:
                    current_seen = self._target_video_first_seen.pop(
                        (current.id, youtube_video_id),
                        datetime.min.replace(tzinfo=UTC),
                    )
                    canonical_key = (target.id, youtube_video_id)
                    canonical_seen = self._target_video_first_seen.get(canonical_key)
                    self._target_video_first_seen[canonical_key] = (
                        min(canonical_seen, current_seen) if canonical_seen else current_seen
                    )
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

    def list_active_parent_jobs(self, *, owner_id: str) -> list[dict[str, Any]]:
        """Return only browser-facing parent jobs for the caller's sources."""

        with self._lock:
            visible_sources: list[tuple[str, str | None, str | None]] = [
                (subscription.id, subscription.target_id, None)
                for subscription in self._subscriptions.values()
                if subscription.user_id == owner_id
            ]
            visible_sources.extend(
                (source_id, None, source_id)
                for source_id, candidate_owner in self._source_owners.items()
                if candidate_owner == owner_id
                and source_id in self._sources
                and self._sources[source_id].target_id is None
            )
            active: list[dict[str, Any]] = []
            for public_source_id, target_id, legacy_source_id in visible_sources:
                candidate = max(
                    (
                        job
                        for job in self._jobs.values()
                        if job.parent_job_id is None
                        and not job.state.is_terminal
                        and (
                            (target_id is not None and job.target_id == target_id)
                            or (legacy_source_id is not None and job.source_id == legacy_source_id)
                        )
                    ),
                    key=lambda job: job.created_at,
                    default=None,
                )
                if candidate:
                    active.append(
                        {
                            "source_id": public_source_id,
                            "target_id": target_id,
                            "job": self._clone_job(candidate),
                        }
                    )
            active.sort(key=lambda item: item["job"].created_at)
            return active

    def list_recent_failed_parent_jobs(
        self, *, owner_id: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Return the caller's failed coordinator jobs, newest failure first."""

        with self._lock:
            failures: list[dict[str, Any]] = []
            for job in self._jobs.values():
                if job.parent_job_id is not None or job.state is not JobState.FAILED:
                    continue
                failed_children = [
                    child
                    for child in self._jobs.values()
                    if child.parent_job_id == job.id and child.state is JobState.FAILED
                ]
                failed_children.sort(
                    key=lambda child: (
                        bool(
                            (child.pause_reason and child.pause_reason.strip())
                            or child.partial_errors
                        ),
                        child.updated_at,
                        child.id,
                    ),
                    reverse=True,
                )
                representative_child = failed_children[0] if failed_children else None
                child_failure_fields = {
                    "failed_child_count": len(failed_children),
                    "representative_child_pause_reason": (
                        representative_child.pause_reason if representative_child else None
                    ),
                    "representative_child_partial_errors": deepcopy(
                        representative_child.partial_errors if representative_child else []
                    ),
                }
                if job.target_id is not None:
                    subscription_id = self._subscription_ids_by_user_target.get(
                        (owner_id, job.target_id)
                    )
                    if not subscription_id:
                        continue
                    subscription = self._subscriptions[subscription_id]
                    # A subscriber can join a shared job that was already queued.
                    # Keep failures that occurred after the subscription while
                    # still hiding terminal history from before it existed.
                    if job.updated_at < subscription.created_at:
                        continue
                    target = self._targets.get(job.target_id)
                    if not target:
                        continue
                    config = subscription.display_config or target.config
                    failures.append(
                        {
                            "source_id": subscription.id,
                            "target_id": target.id,
                            "source_type": target.type,
                            "source_config": deepcopy(config),
                            "canonical_key": target.canonical_key,
                            "failed_at": job.updated_at,
                            "job": self._clone_job(job),
                            **child_failure_fields,
                        }
                    )
                    continue

                source = self._sources.get(job.source_id)
                if (
                    not source
                    or source.target_id is not None
                    or self._source_owners.get(source.id) != owner_id
                ):
                    continue
                failures.append(
                    {
                        "source_id": source.id,
                        "target_id": None,
                        "source_type": source.type,
                        "source_config": deepcopy(source.config),
                        "canonical_key": source.canonical_key,
                        "failed_at": job.updated_at,
                        "job": self._clone_job(job),
                        **child_failure_fields,
                    }
                )

            failures.sort(
                key=lambda item: (item["failed_at"], item["job"].id),
                reverse=True,
            )
            return failures[:limit]

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
