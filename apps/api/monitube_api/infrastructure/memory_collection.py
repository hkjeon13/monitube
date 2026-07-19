"""In-memory collected-content writes and direct comment detail reads."""

from copy import deepcopy
from dataclasses import replace
from typing import Any, Iterable

from ..domain import CommentRecord, QuotaBucket, VideoRecord, utcnow
from ..repositories import (
    NotFoundError,
    RepositoryError,
    _comment_sort_key,
)


class MemoryCollectionMixin:
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
            self._video_first_seen.setdefault(video.youtube_video_id, utcnow())
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
            first_seen_at = utcnow()
            self._source_videos.setdefault(source_id, set()).add(youtube_video_id)
            self._source_video_first_seen.setdefault((source_id, youtube_video_id), first_seen_at)
            if source.target_id:
                self._target_videos.setdefault(source.target_id, set()).add(youtube_video_id)
                self._target_video_first_seen.setdefault((source.target_id, youtube_video_id), first_seen_at)

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

    def persist_comment_page(
        self,
        comments: Iterable[CommentRecord],
        *,
        job_id: str | None = None,
        checkpoint: dict[str, Any] | None = None,
    ) -> list[CommentRecord]:
        """Atomically mirror one upstream comment page in the in-memory store."""

        with self._lock:
            if checkpoint is not None and job_id is None:
                raise RepositoryError("job_id is required when persisting a checkpoint")
            stored = [self.upsert_comment(comment) for comment in comments]
            if checkpoint is not None and job_id is not None:
                self.checkpoint_job(job_id, checkpoint)
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
            parent_comment = (
                self._comments.get(comment.youtube_parent_comment_id)
                if comment.youtube_parent_comment_id
                else None
            )
            root_comment_id = parent_comment.youtube_comment_id if parent_comment else comment.youtube_comment_id
            replies = [
                item
                for item in self._comments.values()
                if item.youtube_parent_comment_id == root_comment_id
                and (visible_video_ids is None or item.youtube_video_id in visible_video_ids)
            ]
            # Render a thread in conversation order.  ``source_fetched_at`` is
            # a deterministic fallback for older records without a publish date.
            replies.sort(key=lambda item: (item.published_at or item.source_fetched_at, item.youtube_comment_id))
            reply_ids = {item.youtube_comment_id for item in replies}
            stored_reply_count = len(replies)
            display_replies = replies[:2]
            if comment.youtube_parent_comment_id and comment.youtube_comment_id not in {
                item.youtube_comment_id for item in display_replies
            }:
                display_replies.append(comment)
                display_replies.sort(key=_comment_sort_key)
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
                "parent_comment": parent_comment,
                "replies": display_replies,
                "stored_reply_count": stored_reply_count,
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
