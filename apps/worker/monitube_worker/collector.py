"""Source-specific YouTube collection and persistence for the polling worker."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import re
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

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


def seconds_until_pacific_reset(now: datetime | None = None) -> int:
    """Conservative delay until the next YouTube daily quota reset boundary."""

    local_now = (now or utcnow()).astimezone(ZoneInfo("America/Los_Angeles"))
    next_midnight = (local_now + timedelta(days=1)).replace(hour=0, minute=1, second=0, microsecond=0)
    return max(60, int((next_midnight - local_now).total_seconds()))


class YouTubeCollector:
    """Collect one source with a single configured API key; it never rotates keys."""

    def __init__(self, repository: CollectionRepository, client: YouTubeDataClient, *, lease_seconds: int = 120) -> None:
        self.repository = repository
        self.client = client
        self.lease_seconds = lease_seconds
        self._active_checkpoint: dict[str, Any] = {}

    def _checkpoint(self, job: JobRecord, *, stage: str, scope_key: str, page_token: str | None, batch_cursor: int = 0) -> None:
        self._active_checkpoint = {
            "stage": stage,
            "scopeKey": scope_key,
            "pageToken": page_token,
            "batchCursor": batch_cursor,
        }
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
        try:
            payload = self.client.request(endpoint, params)
        except YouTubeApiError as exc:
            self.repository.record_api_request(
                job_id=job.id,
                bucket=exc.bucket,
                endpoint=endpoint,
                status_code=exc.status_code,
                error_reason=exc.reasons[0] if exc.reasons else None,
            )
            raise
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
            raise QuotaExhaustedError(
                str(exc), bucket=classification.quota_bucket or exc.bucket, resume_after_seconds=seconds_until_pacific_reset()
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
        uploads = ((content_details.get("relatedPlaylists") or {}).get("uploads"))
        self.repository.upsert_channel(
            {
                "youtube_channel_id": item["id"],
                "handle": snippet.get("customUrl"),
                "title": snippet.get("title"),
                "description": snippet.get("description"),
                "uploads_playlist_id": uploads,
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

    def _channel_video_ids(self, job: JobRecord, source_config: Mapping[str, Any]) -> list[str]:
        channel = self._resolve_channel(job, str(source_config["input"]))
        playlist_id = ((channel.get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads")
        if not playlist_id:
            return []
        limit = job.max_videos or as_int(source_config.get("maxVideos")) or 50
        ids: list[str] = []
        # Discovery pages are idempotently replayed after a quota pause. The page
        # checkpoint alone cannot reconstruct IDs from earlier pages, so resuming its
        # cursor would silently omit them before they are linked to this source.
        page_token: str | None = None
        page_count = 0
        while len(ids) < limit:
            payload = self._call(
                job,
                "playlistItems",
                part="snippet,contentDetails",
                playlistId=playlist_id,
                maxResults=min(50, limit - len(ids)),
                pageToken=page_token,
            )
            page_count += 1
            for item in payload.get("items", []):
                video_id = (item.get("contentDetails") or {}).get("videoId") or (item.get("snippet") or {}).get("resourceId", {}).get("videoId")
                if video_id and video_id not in ids:
                    ids.append(video_id)
                    if len(ids) >= limit:
                        break
            page_token = payload.get("nextPageToken")
            self._checkpoint(job, stage="channel_playlist", scope_key=str(playlist_id), page_token=page_token, batch_cursor=page_count)
            if not page_token:
                break
        return ids

    def _keyword_video_ids(self, job: JobRecord, source_config: Mapping[str, Any]) -> list[str]:
        max_pages = as_int(source_config.get("maxPagesPerRun")) or 1
        ids: list[str] = []
        # Replay the frozen query window and dedupe through source/video upserts.
        # A bare page cursor cannot reproduce previous search result IDs safely.
        page_token: str | None = None
        for page in range(1, max_pages + 1):
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
            for item in payload.get("items", []):
                video_id = (item.get("id") or {}).get("videoId")
                if video_id and video_id not in ids:
                    ids.append(video_id)
            page_token = payload.get("nextPageToken")
            self._checkpoint(job, stage="keyword_search", scope_key=str(source_config["query"]), page_token=page_token, batch_cursor=page)
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
        return records

    def _comment_from_item(self, *, video_id: str, thread_id: str, item: Mapping[str, Any], parent_id: str | None = None) -> CommentRecord:
        snippet = item.get("snippet") or {}
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
        )

    def _collect_comments(self, job: JobRecord, video: VideoRecord, max_pages: int) -> int:
        page_token, completed_pages = self._resume_cursor(job, stage="comments", scope_key=video.youtube_video_id)
        count = 0
        for page in range(completed_pages + 1, max_pages + 1):
            try:
                payload = self._call(
                    job,
                    "commentThreads",
                    part="snippet,replies",
                    videoId=video.youtube_video_id,
                    maxResults=100,
                    textFormat="plainText",
                    pageToken=page_token,
                )
            except YouTubeApiError as exc:
                classification = classify_youtube_error(exc.status_code, exc.reasons, quota_bucket=exc.bucket)
                if classification.category is YoutubeErrorCategory.RESOURCE_UNAVAILABLE:
                    self._add_partial_error(job, scope="video", code=exc.reasons[0] if exc.reasons else "comments_unavailable", message=str(exc), retryable=False)
                    return count
                raise
            for thread in payload.get("items", []):
                thread_id = str(thread.get("id") or "")
                top = (thread.get("snippet") or {}).get("topLevelComment")
                if top and top.get("id"):
                    persisted = self.repository.upsert_comment(self._comment_from_item(video_id=video.youtube_video_id, thread_id=thread_id, item=top))
                    count += 1
                    for reply in ((thread.get("replies") or {}).get("comments") or []):
                        if reply.get("id"):
                            self.repository.upsert_comment(
                                self._comment_from_item(video_id=video.youtube_video_id, thread_id=thread_id, item=reply, parent_id=persisted.youtube_comment_id)
                            )
                            count += 1
            page_token = payload.get("nextPageToken")
            self._checkpoint(job, stage="comments", scope_key=video.youtube_video_id, page_token=page_token, batch_cursor=page)
            if not page_token:
                break
        return count

    def collect(self, job: JobRecord) -> None:
        """Collect and persist a single claimed job, raising runner-recognized errors."""

        source = self.repository.get_source(job.source_id)
        self._active_checkpoint = dict(job.checkpoint)
        try:
            if source.type is SourceType.CHANNEL:
                video_ids = self._channel_video_ids(job, source.config)
            elif source.type is SourceType.KEYWORD:
                video_ids = self._keyword_video_ids(job, source.config)
            else:
                video_ids = [str(source.config["input"])]

            self.repository.update_job_progress(job.id, completed=0, total=len(video_ids), unit="videos", current_stage="fetching_videos")
            videos = self._video_records(job, video_ids)
            for video in videos:
                self.repository.link_source_video(source.id, video.youtube_video_id)
            self.repository.update_job_progress(job.id, completed=len(videos), total=len(video_ids), unit="videos", current_stage="videos_persisted")

            include_comments = bool(job.include_comments or source.config.get("includeComments"))
            if include_comments:
                max_pages = job.max_comments_per_video or as_int(source.config.get("maxCommentPagesPerVideo")) or 1
                collected_comments = 0
                for index, video in enumerate(videos, start=1):
                    collected_comments += self._collect_comments(job, video, max_pages)
                    self.repository.update_job_progress(job.id, completed=index, total=len(videos), unit="comments", current_stage="collecting_comments")
                self._checkpoint(job, stage="analysis", scope_key=source.id, page_token=None, batch_cursor=collected_comments)

            self.repository.save_analysis_summary(source.id)
            self._checkpoint(job, stage="completed", scope_key=source.id, page_token=None, batch_cursor=len(videos))
        except YouTubeApiError as exc:
            self._raise_classified(job, exc)
