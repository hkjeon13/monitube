"""SearchAPI.io transcript collection with deterministic Korean/English fallback."""

from __future__ import annotations

import hashlib
from typing import Any, Mapping

from monitube_api.domain import (
    JobRecord,
    TranscriptSegmentRecord,
    VideoRecord,
    VideoTranscriptRecord,
    new_id,
    utcnow,
)

from ..searchapi import SearchApiError


def _language_options(payload: Mapping[str, Any]) -> list[dict[str, str]]:
    options: list[dict[str, str]] = []
    for item in payload.get("available_languages") or []:
        if not isinstance(item, Mapping):
            continue
        language = str(item.get("lang") or "").strip()
        if language:
            options.append({"lang": language, "name": str(item.get("name") or "").strip()})
    return options


class TranscriptCollectionMixin:
    """Collect a transcript only for a newly seen channel/keyword video."""

    def _persist_transcript_state(
        self,
        video_id: str,
        *,
        requested_language: str,
        state: str,
        error_code: str | None = None,
    ) -> VideoTranscriptRecord:
        now = utcnow()
        return self.repository.upsert_video_transcript(
            VideoTranscriptRecord(
                id=new_id(),
                youtube_video_id=video_id,
                provider="searchapi",
                requested_language=requested_language,
                resolved_language=None,
                language_name=None,
                selection_reason=None,
                transcript_type=self.transcript_type_preference,
                is_auto_generated=None,
                is_translated=None,
                state=state,
                full_text=None,
                content_hash=None,
                fetched_at=None,
                last_attempted_at=now,
                error_code=error_code,
            )
        )

    def _collect_transcript(self, job: JobRecord, video: VideoRecord) -> None:
        primary = self.transcript_primary_language
        fallback = self.transcript_fallback_language
        try:
            payload = self._searchapi_call(
                job,
                "youtube_transcripts",
                self.transcript_client.transcripts,
                video_id=video.youtube_video_id,
                language=primary,
                transcript_type=self.transcript_type_preference,
            )
            options = _language_options(payload)
            requested = primary
            selection_reason = "primary_language"
            if not payload.get("transcripts"):
                fallback_option = next(
                    (
                        item
                        for item in options
                        if item["lang"].casefold() == fallback.casefold()
                        or item["lang"].casefold().startswith(f"{fallback.casefold()}-")
                    ),
                    None,
                )
                if not fallback_option:
                    self._persist_transcript_state(
                        video.youtube_video_id,
                        requested_language=primary,
                        state="unavailable",
                        error_code="preferred_language_unavailable",
                    )
                    return
                requested = fallback_option["lang"]
                selection_reason = "fallback_language"
                payload = self._searchapi_call(
                    job,
                    "youtube_transcripts",
                    self.transcript_client.transcripts,
                    video_id=video.youtube_video_id,
                    language=requested,
                    transcript_type=self.transcript_type_preference,
                )
                options = _language_options(payload) or options

            raw_segments = payload.get("transcripts") or []
            segments: list[TranscriptSegmentRecord] = []
            for item in raw_segments[: self.transcript_max_segments]:
                if not isinstance(item, Mapping):
                    continue
                text = str(item.get("text") or "").strip()
                if not text:
                    continue
                try:
                    start_ms = max(0, round(float(item.get("start") or 0) * 1000))
                    duration_ms = max(0, round(float(item.get("duration") or 0) * 1000))
                except (TypeError, ValueError):
                    continue
                segments.append(
                    TranscriptSegmentRecord(
                        sequence=len(segments),
                        start_ms=start_ms,
                        duration_ms=duration_ms,
                        text=text,
                    )
                )
            if not segments:
                self._persist_transcript_state(
                    video.youtube_video_id,
                    requested_language=requested,
                    state="unavailable",
                    error_code="transcript_empty",
                )
                return

            parameters = payload.get("search_parameters") or {}
            resolved = str(parameters.get("lang") or requested)
            language_option = next(
                (item for item in options if item["lang"].casefold() == resolved.casefold()),
                None,
            )
            language_name = language_option["name"] if language_option else None
            full_text = "\n".join(segment.text for segment in segments)
            lowered_name = (language_name or "").casefold()
            now = utcnow()
            self.repository.upsert_video_transcript(
                VideoTranscriptRecord(
                    id=new_id(),
                    youtube_video_id=video.youtube_video_id,
                    provider="searchapi",
                    requested_language=requested,
                    resolved_language=resolved,
                    language_name=language_name,
                    selection_reason=selection_reason,
                    transcript_type=self.transcript_type_preference,
                    is_auto_generated="auto-generated" in lowered_name,
                    is_translated=resolved.casefold() != requested.casefold(),
                    state="available",
                    full_text=full_text,
                    content_hash=hashlib.sha256(full_text.encode("utf-8")).hexdigest(),
                    fetched_at=now,
                    last_attempted_at=now,
                    segments=tuple(segments),
                )
            )
        except SearchApiError as exc:
            self._persist_transcript_state(
                video.youtube_video_id,
                requested_language=primary,
                state="failed",
                error_code=exc.error_code,
            )

    def _maybe_collect_transcript(
        self,
        job: JobRecord,
        video: VideoRecord,
        *,
        newly_discovered: bool,
    ) -> None:
        if not (
            (newly_discovered or self._active_checkpoint.get("transcriptVideoId") == video.youtube_video_id)
            and self.transcript_collection_enabled
            and self.transcript_client is not None
        ):
            return
        self._active_checkpoint["transcriptVideoId"] = video.youtube_video_id
        self.repository.checkpoint_job(job.id, self._active_checkpoint)
        self._collect_transcript(job, video)
        self._active_checkpoint.pop("transcriptVideoId", None)
        self.repository.checkpoint_job(job.id, self._active_checkpoint)
