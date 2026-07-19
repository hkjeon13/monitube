"""Collection source and canonical target use cases."""

import hashlib

from ..channel_resolution import resolve_channel_input
from ..contracts import (
    ChannelLookup,
    ChannelResolutionResponse,
    CollectionRequestCreate,
    CollectionRequestResponse,
    CollectionSource,
    CollectionSourceCreate,
    CollectionSourceUpdate,
    SourceConfig,
    VideoSourceConfig,
    parse_source_config,
)
from ..domain import CollectionSubmission, SourceType
from ..video_resolution import resolve_video_input
from .base import ApplicationService
from .presenters import job_contract, source_contract


class SourceService(ApplicationService):
    def resolve_channel(self, input_value: str) -> ChannelResolutionResponse:
        resolution = resolve_channel_input(input_value)
        return ChannelResolutionResponse(
            kind=resolution.kind.value,
            normalized=resolution.normalized,
            lookup=ChannelLookup(
                parameter=resolution.lookup_parameter,
                value=resolution.normalized,
            ),
            requires_search=resolution.requires_search,
        )

    @staticmethod
    def _canonical_config(
        source_type: SourceType,
        raw_config: SourceConfig | dict[str, object],
    ) -> SourceConfig:
        config = parse_source_config(source_type, raw_config)
        if isinstance(config, VideoSourceConfig):
            return config.model_copy(
                update={"input": resolve_video_input(config.input).normalized}
            )
        if source_type is SourceType.CHANNEL:
            return config.model_copy(
                update={"input": resolve_channel_input(config.input).normalized}
            )
        return config

    @staticmethod
    def _canonical_target(
        source_type: SourceType,
        config: SourceConfig,
    ) -> tuple[str, list[tuple[str, str]]]:
        """Build a stable identity while excluding collection breadth."""

        serialized = config.model_dump(mode="json", exclude_none=True)
        if source_type is SourceType.CHANNEL:
            resolution = resolve_channel_input(str(serialized["input"]))
            normalized = resolution.normalized
            lowered = normalized.casefold()
            if resolution.kind.value == "channel_id":
                return (
                    f"channel:{normalized}",
                    [("channel_id", normalized), ("input", normalized)],
                )
            return (
                f"channel:{resolution.kind.value}:{lowered}",
                [(resolution.kind.value, lowered), ("input", lowered)],
            )
        if source_type is SourceType.VIDEO:
            video_id = str(serialized["input"])
            return (
                f"video:{video_id}",
                [("video_id", video_id), ("input", video_id)],
            )

        fingerprint_material = "\x1f".join(
            (
                " ".join(str(serialized.get("query") or "").split()).lower(),
                str(serialized.get("publishedAfter") or ""),
                str(serialized.get("publishedBefore") or ""),
                str(serialized.get("regionCode") or "").upper(),
                str(serialized.get("relevanceLanguage") or "").lower(),
                str(serialized.get("order") or "date"),
            )
        )
        fingerprint = hashlib.sha256(
            fingerprint_material.encode("utf-8")
        ).hexdigest()
        return f"keyword:{fingerprint}", [("keyword", fingerprint)]

    @staticmethod
    def _submission_contract(
        submission: CollectionSubmission,
    ) -> CollectionRequestResponse:
        return CollectionRequestResponse(
            id=submission.request.id,
            disposition=submission.disposition,
            targetId=submission.target.id,
            source=source_contract(submission.source),
            job=job_contract(submission.job) if submission.job else None,
        )

    def create_source(
        self,
        request: CollectionSourceCreate,
        *,
        owner_id: str | None = None,
    ) -> CollectionSource:
        """Create a subscription through the shared target coordinator."""

        config = self._canonical_config(request.type, request.config)
        canonical_key, aliases = self._canonical_target(request.type, config)
        submission = self.repository.submit_collection_request(
            source_type=request.type,
            config=config.model_dump(mode="json", exclude_none=True),
            canonical_key=canonical_key,
            aliases=aliases,
            force_refresh=False,
            idempotency_key=None,
            owner_id=owner_id,
            runtime_config_id=self.runtime_config_id,
        )
        return source_contract(submission.source)

    def submit_collection_request(
        self,
        request: CollectionRequestCreate,
        *,
        owner_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> CollectionRequestResponse:
        config = self._canonical_config(request.type, request.config)
        canonical_key, aliases = self._canonical_target(request.type, config)
        submission = self.repository.submit_collection_request(
            source_type=request.type,
            config=config.model_dump(mode="json", exclude_none=True),
            canonical_key=canonical_key,
            aliases=aliases,
            force_refresh=request.forceRefresh,
            idempotency_key=idempotency_key.strip() if idempotency_key else None,
            owner_id=owner_id,
            runtime_config_id=self.runtime_config_id,
        )
        return self._submission_contract(submission)

    def list_sources(
        self,
        *,
        owner_id: str | None = None,
    ) -> list[CollectionSource]:
        return [
            source_contract(record)
            for record in self.repository.list_sources(owner_id=owner_id)
        ]

    def get_source(
        self,
        source_id: str,
        *,
        owner_id: str | None = None,
    ) -> CollectionSource:
        return source_contract(
            self.repository.get_source(source_id, owner_id=owner_id)
        )

    def update_source(
        self,
        source_id: str,
        request: CollectionSourceUpdate,
        *,
        owner_id: str | None = None,
    ) -> CollectionSource:
        existing = self.repository.get_source(source_id, owner_id=owner_id)
        changes: dict[str, object] = {}
        if request.enabled is not None:
            changes["enabled"] = request.enabled
        if request.config is not None:
            config = self._canonical_config(existing.type, request.config)
            changes["config"] = config.model_dump(mode="json", exclude_none=True)
        if request.nextRunAt is not None:
            changes["next_run_at"] = request.nextRunAt
        if not changes:
            return source_contract(existing)
        return source_contract(
            self.repository.update_source(
                source_id,
                owner_id=owner_id,
                **changes,
            )
        )

    def delete_source(
        self,
        source_id: str,
        *,
        owner_id: str | None = None,
    ) -> None:
        self.repository.delete_source(source_id, owner_id=owner_id)
