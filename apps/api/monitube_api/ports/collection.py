"""Collected-content writes and upstream request accounting ports."""

from typing import Any, Iterable, Protocol

from ..domain import CommentRecord, QuotaBucket, VideoRecord, VideoTranscriptRecord


class CollectionWriteRepository(Protocol):
    def upsert_channel(self, channel: dict[str, Any]) -> dict[str, Any]: ...

    def upsert_video(self, video: VideoRecord) -> VideoRecord: ...

    def get_videos_by_youtube_ids(
        self,
        youtube_video_ids: Iterable[str],
    ) -> dict[str, VideoRecord]: ...

    def count_videos_by_channel(self, youtube_channel_id: str) -> int: ...

    def link_source_video(self, source_id: str, youtube_video_id: str) -> None: ...

    def source_video_ids(
        self,
        source_id: str,
        youtube_video_ids: Iterable[str],
    ) -> set[str]: ...

    def count_source_videos(self, source_id: str) -> int: ...

    def upsert_comment(self, comment: CommentRecord) -> CommentRecord: ...

    def persist_comment_page(
        self,
        comments: Iterable[CommentRecord],
        *,
        job_id: str | None = None,
        checkpoint: dict[str, Any] | None = None,
    ) -> list[CommentRecord]: ...

    def existing_comment_ids(
        self,
        youtube_comment_ids: Iterable[str],
    ) -> set[str]: ...

    def comment_counts_by_video(
        self,
        youtube_video_ids: Iterable[str],
    ) -> dict[str, int]: ...

    def has_video_transcript(self, youtube_video_id: str) -> bool: ...

    def upsert_video_transcript(
        self, transcript: VideoTranscriptRecord
    ) -> VideoTranscriptRecord: ...

    def get_video_transcript(
        self, youtube_video_id: str, *, owner_id: str | None = None
    ) -> VideoTranscriptRecord: ...

    def record_provider_request(
        self,
        *,
        job_id: str,
        provider: str,
        operation: str,
        status_code: int,
        error_code: str | None = None,
        item_count: int | None = None,
        requested_language: str | None = None,
        resolved_language: str | None = None,
    ) -> None: ...


class QuotaAuditRepository(Protocol):
    def bootstrap_runtime_config(
        self,
        *,
        environment: str,
        google_project_number: str,
        secret_ref: str,
        key_fingerprint: str | None,
    ) -> str: ...

    def record_api_request(
        self,
        *,
        job_id: str,
        bucket: QuotaBucket,
        endpoint: str,
        status_code: int,
        error_reason: str | None = None,
    ) -> None: ...
