"""Public comment thread, reply, and coverage collection phases."""

from datetime import UTC, datetime
from typing import Any, Iterable, Mapping

from monitube_api.domain import CommentRecord, JobRecord, VideoRecord, new_id, utcnow
from monitube_api.quota import YoutubeErrorCategory, classify_youtube_error

from ..youtube_data import YouTubeApiError
from .parsing import as_int, parse_rfc3339


class CommentCollectionMixin:
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
