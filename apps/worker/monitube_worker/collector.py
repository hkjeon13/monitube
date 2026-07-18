"""Source-specific YouTube collection and persistence for the polling worker."""

from __future__ import annotations

from datetime import UTC, datetime
import re
from typing import Any, Iterable, Mapping

from monitube_api.channel_resolution import resolve_channel_input
from monitube_api.domain import CommentRecord, JobRecord, JobState, SourceType, VideoRecord, new_id, utcnow
from monitube_api.quota import YoutubeErrorCategory, classify_youtube_error
from monitube_api.repositories import CollectionRepository

from .runner import LeaseLostError, QuotaExhaustedError, RetryableCollectionError
from .youtube_data import YouTubeApiError, YouTubeDataClient


_DURATION = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


def parse_rfc3339(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def parse_duration_seconds(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    matched = _DURATION.fullmatch(value)
    if not matched:
        return None
    parts = {name: int(raw or 0) for name, raw in matched.groupdict().items()}
    return parts["days"] * 86_400 + parts["hours"] * 3_600 + parts["minutes"] * 60 + parts["seconds"]


def as_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def quota_retry_delay_seconds(checkpoint: Mapping[str, Any]) -> int:
    """Back off quota-paused work at 1h, 2h, then 3h intervals.

    Quota credits can become available when another managed credential rotates or
    its window resets.  Retrying at a bounded cadence makes that recovery
    automatic without making users wait for a guessed daily-reset boundary.
    """

    prior_attempts = as_int(checkpoint.get("quotaRetryAttempt"))
    return min(10_800, 3_600 * (prior_attempts + 1))


class YouTubeCollector:
    """Collect one source with a single configured API key; it never rotates keys."""

    def __init__(self, repository: CollectionRepository, client: YouTubeDataClient, *, lease_seconds: int = 120) -> None:
        self.repository = repository
        self.client = client
        self.lease_seconds = lease_seconds
        self._active_checkpoint: dict[str, Any] = {}

    def _checkpoint(self, job: JobRecord, *, stage: str, scope_key: str, page_token: str | None, batch_cursor: int = 0) -> None:
        checkpoint = self._checkpoint_payload(
            stage=stage,
            scope_key=scope_key,
            page_token=page_token,
            batch_cursor=batch_cursor,
        )
        self.repository.checkpoint_job(job.id, checkpoint)
        self._active_checkpoint = checkpoint

    def _checkpoint_payload(
        self, *, stage: str, scope_key: str, page_token: str | None, batch_cursor: int = 0
    ) -> dict[str, Any]:
        """Build a candidate checkpoint without advancing the committed cursor."""

        # These fields identify the durable unit of work or the completed
        # discovery boundary. They must survive every detail/comment cursor
        # replacement: losing ``jobKind`` turns a retried child into a full
        # source collection, while losing ``youtubeVideoId`` also breaks shared
        # target version invalidation at parent completion.
        durable_keys = (
            "jobKind",
            "youtubeVideoId",
            "fanoutDiscovered",
            "fanoutVideoCount",
            "phaseProgress",
            "quotaRetryAttempt",
            "keywordExpectedTotal",
        )
        preserved = {
            key: self._active_checkpoint[key]
            for key in durable_keys
            if key in self._active_checkpoint
        }
        return {
            **preserved,
            "stage": stage,
            "scopeKey": scope_key,
            "pageToken": page_token,
            "batchCursor": batch_cursor,
        }

    def _set_phase_progress(
        self,
        job: JobRecord,
        *,
        phase: str,
        completed: int,
        total: int | None,
        current_stage: str,
    ) -> None:
        """Persist independently renderable video/comment progress with the job."""

        existing = self._active_checkpoint.get("phaseProgress")
        phases = dict(existing) if isinstance(existing, dict) else {}
        phases[phase] = {"completed": max(0, completed), "total": max(0, total) if total is not None else None}
        self._active_checkpoint["phaseProgress"] = phases
        self.repository.update_job_progress(
            job.id,
            completed=max(0, completed),
            total=max(0, total) if total is not None else None,
            unit="videos" if phase == "videos" else "comments",
            current_stage=current_stage,
        )
        # Keep the current cursor untouched.  In particular, replacing a
        # comment-page checkpoint here would prevent a quota-paused job from
        # resuming that video's page cursor.
        self.repository.checkpoint_job(job.id, self._active_checkpoint)

    @staticmethod
    def _resume_cursor(job: JobRecord, *, stage: str, scope_key: str) -> tuple[str | None, int]:
        checkpoint = job.checkpoint
        if checkpoint.get("stage") != stage or checkpoint.get("scopeKey") != scope_key:
            return None, 0
        page_token = checkpoint.get("pageToken")
        return (str(page_token) if page_token else None, as_int(checkpoint.get("batchCursor")))

    def _call(self, job: JobRecord, endpoint: str, **params: Any) -> Mapping[str, Any]:
        if job.lease_owner:
            # Renew immediately before every potentially slow upstream call. A failed
            # renewal means another worker reclaimed the job, so do not continue it.
            if not self.repository.renew_job_lease(job_id=job.id, worker_id=job.lease_owner, lease_seconds=self.lease_seconds):
                raise LeaseLostError("Collection job lease is no longer owned by this worker")
        attempts = max(1, int(getattr(self.client, "key_count", 1)))
        for attempt in range(attempts):
            fingerprint = getattr(self.client, "key_fingerprint", None)
            try:
                payload = self.client.request(endpoint, params)
                if fingerprint and hasattr(self.repository, "record_runtime_key_state"):
                    self.repository.record_runtime_key_state(runtime_config_id=job.runtime_config_id, key_fingerprint=fingerprint)
                break
            except YouTubeApiError as exc:
                if fingerprint and hasattr(self.repository, "record_runtime_key_state"):
                    self.repository.record_runtime_key_state(runtime_config_id=job.runtime_config_id, key_fingerprint=fingerprint, error_reason=exc.reasons[0] if exc.reasons else "upstream_error")
                if attempt + 1 < attempts and getattr(self.client, "should_failover", lambda _error: False)(exc):
                    self.client.rotate()
                    continue
                self.repository.record_api_request(
                    job_id=job.id, bucket=exc.bucket, endpoint=endpoint, status_code=exc.status_code,
                    error_reason=exc.reasons[0] if exc.reasons else None,
                )
                raise
        else:  # pragma: no cover - loop always breaks or raises
            raise RuntimeError("YouTube key pool exhausted")
        self.repository.record_api_request(
            job_id=job.id,
            bucket=self.client.bucket_for(endpoint),
            endpoint=endpoint,
            status_code=200,
        )
        return payload

    def _raise_classified(self, job: JobRecord, exc: YouTubeApiError) -> None:
        classification = classify_youtube_error(exc.status_code, exc.reasons, quota_bucket=exc.bucket)
        if self._active_checkpoint:
            self.repository.checkpoint_job(job.id, self._active_checkpoint)
        if classification.category is YoutubeErrorCategory.QUOTA_EXHAUSTED:
            checkpoint = dict(self._active_checkpoint or job.checkpoint)
            checkpoint["quotaRetryAttempt"] = as_int(checkpoint.get("quotaRetryAttempt")) + 1
            self._active_checkpoint = checkpoint
            self.repository.checkpoint_job(job.id, checkpoint)
            raise QuotaExhaustedError(
                str(exc),
                bucket=classification.quota_bucket or exc.bucket,
                resume_after_seconds=quota_retry_delay_seconds(job.checkpoint),
            ) from exc
        if classification.retryable:
            raise RetryableCollectionError(str(exc), retry_after_seconds=60) from exc
        raise exc

    def _add_partial_error(self, job: JobRecord, *, scope: str, code: str, message: str, retryable: bool) -> None:
        current = self.repository.get_job(job.id)
        errors = list(current.partial_errors)
        errors.append({"scope": scope, "sourceId": current.source_id, "code": code, "retryable": retryable, "message": message})
        self.repository.transition_job(job.id, current.state, partial_errors=errors)

    def _resolve_channel(self, job: JobRecord, input_value: str) -> Mapping[str, Any]:
        resolution = resolve_channel_input(input_value)
        if resolution.requires_search:
            search = self._call(job, "search", part="snippet", type="channel", q=resolution.normalized, maxResults=1)
            items = search.get("items", [])
            if not items:
                raise RuntimeError("No YouTube channel matched this source input")
            channel_id = (items[0].get("id") or {}).get("channelId")
            if not channel_id:
                raise RuntimeError("Channel search result did not contain a channel ID")
            params = {"id": channel_id}
        else:
            params = {resolution.lookup_parameter: resolution.normalized}
        payload = self._call(job, "channels", part="snippet,contentDetails,statistics", maxResults=1, **params)
        items = payload.get("items", [])
        if not items:
            raise RuntimeError("YouTube channel was not found")
        item = items[0]
        snippet = item.get("snippet") or {}
        content_details = item.get("contentDetails") or {}
        statistics = item.get("statistics") or {}
        uploads = ((content_details.get("relatedPlaylists") or {}).get("uploads"))
        self.repository.upsert_channel(
            {
                "youtube_channel_id": item["id"],
                "handle": snippet.get("customUrl"),
                "title": snippet.get("title"),
                "description": snippet.get("description"),
                "thumbnail_url": ((snippet.get("thumbnails") or {}).get("high") or (snippet.get("thumbnails") or {}).get("medium") or (snippet.get("thumbnails") or {}).get("default") or {}).get("url"),
                "uploads_playlist_id": uploads,
                "statistics": {
                    "subscriberCount": as_int(statistics.get("subscriberCount")),
                    "viewCount": as_int(statistics.get("viewCount")),
                    "videoCount": as_int(statistics.get("videoCount")),
                    "hiddenSubscriberCount": bool(statistics.get("hiddenSubscriberCount", False)),
                },
                "source_fetched_at": utcnow(),
            }
        )
        # A handle or URL is only a mutable alias. Once YouTube resolves it to a
        # UC identifier, atomically promote the worker source's provisional target
        # so later handle/URL/ID requests share one collection target.
        self.repository.promote_channel_target(
            source_id=job.source_id,
            youtube_channel_id=str(item["id"]),
            handle=snippet.get("customUrl"),
        )
        return item

    def _channel_video_ids(
        self, job: JobRecord, source_config: Mapping[str, Any], *, incremental_refresh: bool
    ) -> tuple[list[str], dict[str, VideoRecord], bool]:
        channel = self._resolve_channel(job, str(source_config["input"]))
        playlist_id = ((channel.get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads")
        if not playlist_id:
            return [], {}, False
        collect_all = bool(source_config.get("collectAllVideos"))
        limit = None if collect_all else job.max_videos or as_int(source_config.get("maxVideos")) or 50
        expected_video_count = as_int((channel.get("statistics") or {}).get("videoCount"))
        stored_video_count = self.repository.count_videos_by_channel(str(channel["id"]))
        # The uploads playlist is newest-first.  A target marked complete can still
        # be incomplete when an earlier quota pause meant we never reached its tail.
        # In that case do not stop at the first known page: traverse the playlist and
        # then process the returned IDs oldest-first to fill the historical gap.
        backfill_required = bool(collect_all and expected_video_count > stored_video_count)
        ids: list[str] = []
        known_videos: dict[str, VideoRecord] = {}
        # Discovery pages are idempotently replayed after a quota pause. The page
        # checkpoint alone cannot reconstruct IDs from earlier pages, so resuming its
        # cursor would silently omit them before they are linked to this source.
        page_token: str | None = None
        page_count = 0
        while limit is None or len(ids) < limit:
            payload = self._call(
                job,
                "playlistItems",
                part="snippet,contentDetails",
                playlistId=playlist_id,
                maxResults=50 if limit is None else min(50, limit - len(ids)),
                pageToken=page_token,
            )
            page_count += 1
            page_ids: list[str] = []
            for item in payload.get("items", []):
                video_id = (item.get("contentDetails") or {}).get("videoId") or (item.get("snippet") or {}).get("resourceId", {}).get("videoId")
                if video_id and video_id not in page_ids:
                    page_ids.append(video_id)
                if video_id and video_id not in ids:
                    ids.append(video_id)
                    if limit is not None and len(ids) >= limit:
                        break
            existing_on_page = self.repository.get_videos_by_youtube_ids(page_ids)
            known_videos.update(existing_on_page)
            page_token = payload.get("nextPageToken")
            self._checkpoint(job, stage="channel_playlist", scope_key=str(playlist_id), page_token=page_token, batch_cursor=page_count)
            # Upload playlists are newest-first. On a healthy incremental refresh,
            # an all-known page proves older pages cannot introduce an upload. A
            # count deficit disables this shortcut until historical coverage catches
            # up with the channel's public video count.
            if incremental_refresh and not backfill_required and collect_all and page_ids and len(existing_on_page) == len(page_ids):
                break
            if not page_token:
                break
        if backfill_required:
            ids.reverse()
        return ids, known_videos, backfill_required

    def _keyword_video_ids(self, job: JobRecord, source_config: Mapping[str, Any]) -> list[str]:
        ids: list[str] = []
        # A fully known page is an incremental boundary only for latest-first
        # results: every following page is older and has already been collected.
        # A bare page cursor cannot reproduce previous search result IDs safely.
        page_token: str | None = None
        page = 0
        expected_total = as_int(job.checkpoint.get("keywordExpectedTotal"))
        stored_total = self.repository.count_source_videos(job.source_id)
        while True:
            page += 1
            payload = self._call(
                job,
                "search",
                part="snippet",
                type="video",
                q=source_config["query"],
                order=source_config.get("order", "date"),
                publishedAfter=source_config.get("publishedAfter"),
                publishedBefore=source_config.get("publishedBefore"),
                regionCode=source_config.get("regionCode"),
                relevanceLanguage=source_config.get("relevanceLanguage"),
                maxResults=50,
                pageToken=page_token,
            )
            response_total = as_int((payload.get("pageInfo") or {}).get("totalResults"))
            if response_total:
                expected_total = max(expected_total, response_total)
                self._active_checkpoint["keywordExpectedTotal"] = expected_total
            page_ids: list[str] = []
            for item in payload.get("items", []):
                video_id = (item.get("id") or {}).get("videoId")
                if video_id and video_id not in page_ids:
                    page_ids.append(video_id)
                if video_id and video_id not in ids:
                    ids.append(video_id)
            page_token = payload.get("nextPageToken")
            self._checkpoint(job, stage="keyword_search", scope_key=str(source_config["query"]), page_token=page_token, batch_cursor=page)
            # A successful but empty page is the provider's natural end of the
            # result set. Errors take the exception/retry path instead.
            if not page_ids:
                break
            known_on_page = self.repository.source_video_ids(job.source_id, page_ids)
            if (
                source_config.get("order", "date") == "date"
                and page_ids
                and len(known_on_page) == len(page_ids)
                and stored_total >= expected_total
            ):
                break
            if not page_token:
                break
        return ids

    def _video_records(self, job: JobRecord, video_ids: Iterable[str]) -> list[VideoRecord]:
        records: list[VideoRecord] = []
        distinct_ids = list(dict.fromkeys(video_ids))
        # Source linkage happens after detail upsert, so replay all detail batches on
        # resume. Upserts make this safe and avoid missing an earlier batch.
        for offset in range(0, len(distinct_ids), 50):
            batch = distinct_ids[offset : offset + 50]
            payload = self._call(job, "videos", part="snippet,contentDetails,statistics,status", id=",".join(batch), maxResults=50)
            for item in payload.get("items", []):
                snippet = item.get("snippet") or {}
                content_details = item.get("contentDetails") or {}
                status = item.get("status") or {}
                statistics = item.get("statistics") or {}
                channel_id = snippet.get("channelId")
                if channel_id:
                    # Keyword/direct-video discovery often lacks a prior channel
                    # source. Store a minimal channel row so the video relation is
                    # still retained; a later channel collection enriches it.
                    self.repository.upsert_channel(
                        {
                            "youtube_channel_id": channel_id,
                            "handle": None,
                            "title": snippet.get("channelTitle"),
                            "description": None,
                            "uploads_playlist_id": None,
                            "source_fetched_at": utcnow(),
                        }
                    )
                record = VideoRecord(
                    id=new_id(),
                    youtube_video_id=item["id"],
                    youtube_channel_id=channel_id,
                    title=snippet.get("title"),
                    description=snippet.get("description"),
                    published_at=parse_rfc3339(snippet.get("publishedAt")),
                    duration_seconds=parse_duration_seconds(content_details.get("duration")),
                    privacy_status=status.get("privacyStatus"),
                    made_for_kids=status.get("madeForKids"),
                    statistics={
                        "viewCount": as_int(statistics.get("viewCount")),
                        "likeCount": as_int(statistics.get("likeCount")),
                        "commentCount": as_int(statistics.get("commentCount")),
                    },
                    source_fetched_at=utcnow(),
                )
                records.append(self.repository.upsert_video(record))
            self._checkpoint(job, stage="video_details", scope_key="videos", page_token=None, batch_cursor=offset + len(batch))
            self._set_phase_progress(
                job,
                phase="videos",
                completed=offset + len(batch),
                total=len(distinct_ids),
                current_stage="fetching_videos",
            )
        return records

    def _comment_from_item(self, *, video_id: str, thread_id: str, item: Mapping[str, Any], parent_id: str | None = None) -> CommentRecord:
        snippet = item.get("snippet") or {}
        author_channel = snippet.get("authorChannelId") or {}
        return CommentRecord(
            id=new_id(),
            youtube_comment_id=str(item["id"]),
            youtube_video_id=video_id,
            youtube_parent_comment_id=parent_id or snippet.get("parentId"),
            youtube_thread_id=thread_id,
            text_display=snippet.get("textDisplay") or snippet.get("textOriginal"),
            like_count=as_int(snippet.get("likeCount")),
            published_at=parse_rfc3339(snippet.get("publishedAt")),
            updated_at=parse_rfc3339(snippet.get("updatedAt")),
            source_fetched_at=utcnow(),
            author_channel_id=author_channel.get("value") if isinstance(author_channel, Mapping) else None,
            author_display_name=snippet.get("authorDisplayName"),
        )

    def _persist_comment_page(
        self,
        comments: list[CommentRecord],
        *,
        job_id: str | None = None,
        checkpoint: dict[str, Any] | None = None,
    ) -> list[CommentRecord]:
        """Use the batch writer when enabled, preserving a rollback switch."""

        if getattr(self.repository, "enable_comment_batch_write", True):
            stored = self.repository.persist_comment_page(
                comments, job_id=job_id, checkpoint=checkpoint
            )
        else:
            stored = [self.repository.upsert_comment(comment) for comment in comments]
            if checkpoint is not None and job_id is not None:
                self.repository.checkpoint_job(job_id, checkpoint)
        # The in-memory resume cursor advances only after the data and matching
        # checkpoint have committed. A reply-page quota/error before this point
        # must leave the parent commentThreads cursor at its prior boundary.
        if checkpoint is not None:
            self._active_checkpoint = dict(checkpoint)
        return stored

    def _collect_remaining_replies(
        self,
        job: JobRecord,
        *,
        video_id: str,
        thread_id: str,
        parent_comment_id: str,
        final_checkpoint: dict[str, Any] | None = None,
    ) -> int:
        """Fetch every reply page for a top-level comment.

        ``commentThreads.list`` only embeds a limited reply subset.  The
        separate ``comments.list(parentId=...)`` traversal is therefore needed
        for the advertised public-comment count and coverage rate to be honest.
        Upserts make replaying this traversal safe when a quota pause occurs.
        """

        count = 0
        page_token: str | None = None
        while True:
            payload = self._call(
                job,
                "comments",
                part="snippet",
                parentId=parent_comment_id,
                maxResults=100,
                textFormat="plainText",
                pageToken=page_token,
            )
            reply_page = [
                self._comment_from_item(
                    video_id=video_id,
                    thread_id=thread_id,
                    item=reply,
                    parent_id=parent_comment_id,
                )
                for reply in payload.get("items", [])
                if reply.get("id")
            ]
            next_page_token = payload.get("nextPageToken")
            checkpoint = final_checkpoint if not next_page_token else None
            if reply_page or checkpoint is not None:
                self._persist_comment_page(
                    reply_page,
                    job_id=job.id if checkpoint is not None else None,
                    checkpoint=checkpoint,
                )
                count += len(reply_page)
            page_token = next_page_token
            if not page_token:
                return count

    def _collect_comments(
        self, job: JobRecord, video: VideoRecord, max_pages: int | None, *, incremental_refresh: bool
    ) -> int:
        page_token, completed_pages = self._resume_cursor(job, stage="comments", scope_key=video.youtube_video_id)
        count = 0
        page = completed_pages
        while max_pages is None or page < max_pages:
            page += 1
            try:
                payload = self._call(
                    job,
                    "commentThreads",
                    part="snippet,replies",
                    videoId=video.youtube_video_id,
                    maxResults=100,
                    textFormat="plainText",
                    order="time",
                    pageToken=page_token,
                )
            except YouTubeApiError as exc:
                classification = classify_youtube_error(exc.status_code, exc.reasons, quota_bucket=exc.bucket)
                if classification.category is YoutubeErrorCategory.RESOURCE_UNAVAILABLE:
                    self._add_partial_error(job, scope="video", code=exc.reasons[0] if exc.reasons else "comments_unavailable", message=str(exc), retryable=False)
                    return count
                raise
            threads = payload.get("items", [])
            page_comment_ids: list[str] = []
            for thread in threads:
                top = (thread.get("snippet") or {}).get("topLevelComment")
                if top and top.get("id"):
                    page_comment_ids.append(str(top["id"]))
                for reply in ((thread.get("replies") or {}).get("comments") or []):
                    if reply.get("id"):
                        page_comment_ids.append(str(reply["id"]))
            page_comment_ids = list(dict.fromkeys(page_comment_ids))
            known_comment_ids = self.repository.existing_comment_ids(page_comment_ids)
            page_records: list[CommentRecord] = []
            reply_traversals: list[tuple[str, str]] = []
            for thread in threads:
                thread_id = str(thread.get("id") or "")
                top = (thread.get("snippet") or {}).get("topLevelComment")
                if top and top.get("id"):
                    top_record = self._comment_from_item(
                        video_id=video.youtube_video_id,
                        thread_id=thread_id,
                        item=top,
                    )
                    page_records.append(top_record)
                    inline_replies = ((thread.get("replies") or {}).get("comments") or [])
                    total_replies = as_int((thread.get("snippet") or {}).get("totalReplyCount"))
                    if total_replies > len(inline_replies):
                        reply_traversals.append((thread_id, top_record.youtube_comment_id))
                    else:
                        for reply in inline_replies:
                            if reply.get("id"):
                                page_records.append(
                                    self._comment_from_item(
                                        video_id=video.youtube_video_id,
                                        thread_id=thread_id,
                                        item=reply,
                                        parent_id=top_record.youtube_comment_id,
                                    )
                                )
            next_page_token = payload.get("nextPageToken")
            checkpoint = self._checkpoint_payload(
                stage="comments",
                scope_key=video.youtube_video_id,
                page_token=next_page_token,
                batch_cursor=page,
            )
            if page_records:
                self._persist_comment_page(
                    page_records,
                    job_id=job.id if not reply_traversals else None,
                    checkpoint=checkpoint if not reply_traversals else None,
                )
                count += len(page_records)
            for traversal_index, (thread_id, parent_comment_id) in enumerate(reply_traversals):
                count += self._collect_remaining_replies(
                    job,
                    video_id=video.youtube_video_id,
                    thread_id=thread_id,
                    parent_comment_id=parent_comment_id,
                    final_checkpoint=checkpoint if traversal_index == len(reply_traversals) - 1 else None,
                )
            if not page_records and not reply_traversals:
                self._persist_comment_page([], job_id=job.id, checkpoint=checkpoint)
            page_token = next_page_token
            # ``order=time`` makes the first all-known page the incremental
            # boundary. We still upsert that page above so likes/edits remain fresh.
            if incremental_refresh and page_comment_ids and len(known_comment_ids) == len(page_comment_ids):
                break
            if not page_token:
                break
        return count

    def _prioritize_comment_collection(
        self, videos: Iterable[VideoRecord], persisted_comment_counts: Mapping[str, int]
    ) -> list[VideoRecord]:
        """Order incomplete videos by comment coverage, then by oldest upload.

        YouTube's advertised ``commentCount`` is the target count.  A lower
        persisted/advertised ratio means the video has more of its public
        discussion left to collect.  Preserve the video holding an active
        page cursor first so a quota-paused job can resume without discarding
        its already-paid-for pagination position.
        """

        resume_scope = self._active_checkpoint.get("scopeKey") if self._active_checkpoint.get("stage") == "comments" else None

        def priority(video: VideoRecord) -> tuple[int, float, datetime, str]:
            advertised_count = as_int(video.statistics.get("commentCount"))
            persisted_count = persisted_comment_counts.get(video.youtube_video_id, 0)
            coverage = persisted_count / advertised_count if advertised_count else 1.0
            # Missing publication metadata is placed last within the same
            # coverage band because it cannot reliably be considered old.
            published_at = video.published_at or datetime.max.replace(tzinfo=UTC)
            return (
                0 if video.youtube_video_id == resume_scope else 1,
                coverage,
                published_at,
                video.youtube_video_id,
            )

        return sorted(videos, key=priority)

    def collect(self, job: JobRecord) -> None:
        """Collect and persist a single claimed job, raising runner-recognized errors."""

        source = self.repository.get_source(job.source_id)
        self._active_checkpoint = dict(job.checkpoint)
        try:
            if job.checkpoint.get("jobKind") == "video":
                self._collect_video_job(job, source)
                return
            if job.checkpoint.get("fanoutDiscovered"):
                self._finalize_fanout_job(job, source)
                return
            if source.type is SourceType.VIDEO:
                # A direct video request is already the smallest schedulable unit.
                self._collect_video_job(job, source, video_id=str(source.config["input"]))
                return
            if source.type is SourceType.CHANNEL:
                incremental_refresh = bool(source.coverage.get("complete") and source.coverage.get("collectAllVideos"))
                video_ids, known_videos, backfill_required = self._channel_video_ids(
                    job, source.config, incremental_refresh=incremental_refresh
                )
            elif source.type is SourceType.KEYWORD:
                video_ids = self._keyword_video_ids(job, source.config)
                known_videos = {}
                incremental_refresh = False
                backfill_required = False
            if job.target_id is None:
                self._collect_video_ids_inline(
                    job, source, video_ids, incremental_refresh=incremental_refresh, backfill_required=backfill_required
                )
                return
            # A discovery job performs only the cheap list/search phase, then fans
            # out independently retryable video jobs. This stops a large channel
            # from monopolising the worker ahead of other channels or keywords.
            self.repository.enqueue_video_jobs(parent_job=job, youtube_video_ids=video_ids)
            checkpoint = dict(self._active_checkpoint)
            checkpoint["fanoutDiscovered"] = True
            checkpoint["fanoutVideoCount"] = len(video_ids)
            self._active_checkpoint = checkpoint
            self.repository.checkpoint_job(job.id, checkpoint)
            self._set_phase_progress(
                job, phase="videos", completed=0, total=len(video_ids),
                current_stage="waiting_for_video_jobs",
            )
            raise RetryableCollectionError("Waiting for video collection jobs", retry_after_seconds=5)
        except YouTubeApiError as exc:
            self._raise_classified(job, exc)

    def _finalize_fanout_job(self, job: JobRecord, source: Any) -> None:
        total, terminal, failed = self.repository.child_job_summary(parent_job_id=job.id)
        self._set_phase_progress(job, phase="videos", completed=terminal, total=total, current_stage="waiting_for_video_jobs")
        if terminal < total:
            raise RetryableCollectionError("Waiting for video collection jobs", retry_after_seconds=5)
        if failed:
            raise RuntimeError(f"{failed} video collection job(s) failed")
        self._checkpoint(job, stage="completed", scope_key=source.id, page_token=None, batch_cursor=total)

    def _collect_video_ids_inline(
        self, job: JobRecord, source: Any, video_ids: list[str], *, incremental_refresh: bool, backfill_required: bool
    ) -> None:
        stage = "backfilling_oldest_videos" if backfill_required else "fetching_videos"
        self._set_phase_progress(job, phase="videos", completed=0, total=len(video_ids), current_stage=stage)
        videos = self._video_records(job, video_ids)
        for video in videos:
            self.repository.link_source_video(source.id, video.youtube_video_id)
        self._set_phase_progress(job, phase="videos", completed=len(video_ids), total=len(video_ids), current_stage="videos_persisted")
        if job.include_comments or source.config.get("includeComments"):
            max_pages = None if source.config.get("collectAllComments") else (
                job.max_comments_per_video or as_int(source.config.get("maxCommentPagesPerVideo")) or 1
            )
            persisted = self.repository.comment_counts_by_video(video.youtube_video_id for video in videos)
            pending = self._prioritize_comment_collection(
                [video for video in videos if persisted.get(video.youtube_video_id, 0) < video.statistics.get("commentCount", 0)], persisted
            )
            done = len(videos) - len(pending)
            self._set_phase_progress(job, phase="comments", completed=done, total=len(videos), current_stage="collecting_comments")
            for index, video in enumerate(pending, start=1):
                self._collect_comments(job, video, max_pages, incremental_refresh=incremental_refresh)
                self._set_phase_progress(job, phase="comments", completed=done + index, total=len(videos), current_stage="collecting_comments")
        self._checkpoint(job, stage="completed", scope_key=source.id, page_token=None, batch_cursor=len(videos))

    def _collect_video_job(self, job: JobRecord, source: Any, *, video_id: str | None = None) -> None:
        video_id = video_id or str(job.checkpoint.get("youtubeVideoId") or "")
        if not video_id:
            raise RuntimeError("Video job is missing youtubeVideoId")
        # Direct-video parent jobs do not start with the child ``jobKind``
        # checkpoint. Persist the video identity before the first detail cursor
        # so retry routing and cross-target terminal invalidation remain exact.
        self._active_checkpoint["youtubeVideoId"] = video_id
        videos = self._video_records(job, [video_id])
        for video in videos:
            self.repository.link_source_video(source.id, video.youtube_video_id)
        self._set_phase_progress(job, phase="videos", completed=len(videos), total=1, current_stage="video_persisted")
        if not videos:
            return
        include_comments = bool(job.include_comments or source.config.get("includeComments"))
        if include_comments:
            max_pages = None if source.config.get("collectAllComments") else (
                job.max_comments_per_video or as_int(source.config.get("maxCommentPagesPerVideo")) or 1
            )
            video = videos[0]
            persisted_count = self.repository.comment_counts_by_video([video.youtube_video_id]).get(video.youtube_video_id, 0)
            if persisted_count < video.statistics.get("commentCount", 0):
                self._set_phase_progress(job, phase="comments", completed=0, total=1, current_stage="collecting_comments")
                self._collect_comments(job, video, max_pages, incremental_refresh=False)
            self._set_phase_progress(job, phase="comments", completed=1, total=1, current_stage="comments_persisted")
        self._checkpoint(job, stage="completed", scope_key=video_id, page_token=None, batch_cursor=1)
