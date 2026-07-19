"""In-memory result, explore, search, and comment read models."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable

from ..analysis import build_summary
from ..domain import CommentRecord, JobRecord, JobState, SourceRecord, SourceType, VideoRecord, utcnow
from ..fuzzy_search import normalize_search_text, rank_text_fields
from ..repositories import (
    CommentThreadSort,
    NotFoundError,
    RepositoryError,
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


class MemoryReadMixin:
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
        self, *, limit: int = 60, offset: int = 0, channel_id: str | None = None, owner_id: str | None = None
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
            videos.sort(
                key=lambda item: (item.published_at or item.source_fetched_at, item.source_fetched_at),
                reverse=True,
            )
            page = videos[offset:offset + limit]
            return {
                "channels": channels,
                "videos": page,
                "next_offset": offset + len(page) if offset + len(page) < len(videos) else None,
            }

    def list_explore_channels(self, *, owner_id: str | None = None) -> list[dict[str, Any]]:
        """Return channel summaries independently from any video page."""

        return self.list_explore(limit=1, owner_id=owner_id)["channels"]

    def list_explore_videos_page(
        self,
        *,
        owner_id: str | None = None,
        channel_id: str | None = None,
        cursor: str | None = None,
        limit: int = 60,
    ) -> dict[str, Any]:
        with self._lock:
            scope = f"owner:{owner_id}" if owner_id is not None else "public"
            filter_hash = explore_video_filter_hash(channel_id)
            decoded = decode_explore_video_cursor(
                cursor,
                scope=scope,
                filter_hash=filter_hash,
            )
            snapshot_at = decoded.snapshot_at if decoded else utcnow()
            membership = self._visible_video_membership_locked(owner_id)
            videos = sorted(
                (
                    self._videos[youtube_video_id]
                    for youtube_video_id, first_seen_at in membership.items()
                    if first_seen_at <= snapshot_at
                    and (
                        channel_id is None
                        or self._videos[youtube_video_id].youtube_channel_id == channel_id
                    )
                ),
                key=_explore_video_sort_key,
                reverse=True,
            )
            total = len(videos)
            if decoded:
                cursor_key = (
                    decoded.effective_at,
                    decoded.fetched_at,
                    decoded.youtube_video_id,
                )
                videos = [video for video in videos if _explore_video_sort_key(video) < cursor_key]
            candidates = videos[: limit + 1]
            has_more = len(candidates) > limit
            page = candidates[:limit]
            next_cursor = (
                encode_explore_video_cursor(
                    page[-1],
                    snapshot_at=snapshot_at,
                    scope=scope,
                    filter_hash=filter_hash,
                )
                if has_more and page
                else None
            )
            return {
                "videos": list(page),
                "next_cursor": next_cursor,
                "snapshot_at": snapshot_at,
                "total": total,
            }

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
            normalized_query = normalize_search_text(query)
            short_query = len(normalized_query) == 2
            video_results: list[dict[str, Any]] = []
            comment_results: list[dict[str, Any]] = []
            if scope in {"all", "videos"}:
                for video in self._videos.values():
                    if visible_video_ids is not None and video.youtube_video_id not in visible_video_ids:
                        continue
                    channel = self._channels.get(video.youtube_channel_id or "", {})
                    fields = {
                        "id": video.youtube_video_id,
                        "title": video.title,
                        "handle": channel.get("handle"),
                    }
                    if short_query:
                        matched_fields = [
                            name
                            for name, value in fields.items()
                            if value and normalize_search_text(str(value)).startswith(normalized_query)
                        ]
                        score = 1.0 if matched_fields else 0.0
                    else:
                        score, matched_fields = rank_text_fields(query, {
                            **fields,
                            "description": video.description,
                            "channel": channel.get("title"),
                        })
                    if matched_fields:
                        video_results.append({"video": video, "score": score, "matched_fields": matched_fields})

            if not short_query and scope in {"all", "comments"}:
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

    def _source_video_membership_locked(self, source_id: str) -> dict[str, datetime]:
        subscription = self._subscriptions.get(source_id)
        source = self._sources.get(source_id)
        if subscription:
            ids = self._target_videos.get(subscription.target_id, set())
            first_seen = self._target_video_first_seen
            scope = subscription.target_id
        elif source and source.target_id:
            ids = self._target_videos.get(source.target_id, set())
            first_seen = self._target_video_first_seen
            scope = source.target_id
        else:
            ids = self._source_videos.get(source_id, set())
            first_seen = self._source_video_first_seen
            scope = source_id
        epoch = datetime.min.replace(tzinfo=UTC)
        return {
            youtube_video_id: first_seen.get((scope, youtube_video_id), epoch)
            for youtube_video_id in ids
            if youtube_video_id in self._videos
        }

    def _source_video_records(self, source_id: str) -> list[VideoRecord]:
        membership = self._source_video_membership_locked(source_id)
        return sorted(
            (self._videos[item] for item in membership),
            key=_video_sort_key,
            reverse=True,
        )

    def _source_parent_jobs_locked(self, source_id: str, source: SourceRecord) -> list[JobRecord]:
        return [
            job
            for job in self._jobs.values()
            if job.parent_job_id is None
            and (
                (source.target_id is not None and job.target_id == source.target_id)
                or (source.target_id is None and job.source_id == source_id)
            )
        ]

    @staticmethod
    def _top_video_records(videos: list[VideoRecord], *, limit: int = 6) -> dict[str, list[VideoRecord]]:
        metric_fields = {
            "views": "viewCount",
            "likes": "likeCount",
            "comments": "commentCount",
        }
        return {
            name: sorted(
                videos,
                key=lambda video: (
                    int(video.statistics.get(field, 0)),
                    _effective_video_timestamp(video),
                    video.youtube_video_id,
                ),
                reverse=True,
            )[:limit]
            for name, field in metric_fields.items()
        }

    @staticmethod
    def _summary_identity(source_id: str, source: SourceRecord) -> str:
        return f"target:{source.target_id}" if source.target_id else f"source:{source_id}"

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

    def _visible_video_membership_locked(self, owner_id: str | None) -> dict[str, datetime]:
        """Return distinct visible videos and the time they entered this scope."""

        if owner_id is None:
            return {
                youtube_video_id: self._video_first_seen.get(youtube_video_id, datetime.min.replace(tzinfo=UTC))
                for youtube_video_id in self._videos
            }
        visible: dict[str, datetime] = {}
        for subscription in self._subscriptions.values():
            if subscription.user_id != owner_id:
                continue
            for youtube_video_id in self._target_videos.get(subscription.target_id, set()):
                membership_seen = self._target_video_first_seen.get(
                    (subscription.target_id, youtube_video_id),
                    datetime.min.replace(tzinfo=UTC),
                )
                first_visible_at = max(subscription.created_at, membership_seen)
                current = visible.get(youtube_video_id)
                if current is None or first_visible_at < current:
                    visible[youtube_video_id] = first_visible_at
        return visible

    def _comments_for_video_ids(self, video_ids: set[str]) -> list[CommentRecord]:
        return sorted(
            (comment for comment in self._comments.values() if comment.youtube_video_id in video_ids),
            key=lambda item: item.published_at or item.source_fetched_at,
            reverse=True,
        )

    def save_analysis_summary(self, source_id: str) -> dict[str, Any]:
        with self._lock:
            source = self.get_source(source_id)
            videos = self._source_video_records(source_id)
            comments = self._comments_for_video_ids({video.youtube_video_id for video in videos})
            summary = build_summary(videos, comments)
            identity = self._summary_identity(source_id, source)
            parent_jobs = self._source_parent_jobs_locked(source_id, source)
            terminal_jobs = [job for job in parent_jobs if job.state.is_terminal]
            latest_terminal = max(terminal_jobs, key=lambda job: job.created_at, default=None)
            self._analysis[identity] = deepcopy(summary)
            self._analysis_metadata[identity] = {
                "data_version": len(terminal_jobs),
                "as_of_job_id": latest_terminal.id if latest_terminal else None,
                "coverage": {
                    "strategy": "full",
                    "sampledComments": len(comments),
                    "totalComments": len(comments),
                },
            }
            return deepcopy(summary)

    def get_source_results(self, source_id: str, *, owner_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            source = self.get_source(source_id, owner_id=owner_id)
            videos = self._source_video_records(source_id)
            comments = self._comments_for_video_ids({video.youtube_video_id for video in videos})
            parent_jobs = self._source_parent_jobs_locked(source_id, source)
            latest_job = next(
                iter(
                    sorted(
                        parent_jobs,
                        key=lambda item: item.created_at,
                        reverse=True,
                    )
                ),
                None,
            )
            identity = self._summary_identity(source_id, source)
            summary = deepcopy(self._analysis.get(identity) or self._analysis.get(source_id) or build_summary(videos, comments))
            return {
                "source": source,
                "latest_job": self._clone_job(latest_job) if latest_job else None,
                "videos": list(videos),
                "comments": list(comments),
                "analysis": summary,
            }

    def get_source_overview(self, source_id: str, *, owner_id: str | None = None) -> dict[str, Any]:
        """Build a bounded overview without returning any comment records."""

        with self._lock:
            source = self.get_source(source_id, owner_id=owner_id)
            videos = self._source_video_records(source_id)
            comments = self._comments_for_video_ids({video.youtube_video_id for video in videos})
            live_summary = build_summary(videos, comments)
            parent_jobs = self._source_parent_jobs_locked(source_id, source)
            latest_job = max(parent_jobs, key=lambda job: job.created_at, default=None)
            terminal_jobs = [job for job in parent_jobs if job.state.is_terminal]
            latest_terminal = max(terminal_jobs, key=lambda job: job.created_at, default=None)
            data_version = len(terminal_jobs)
            identity = self._summary_identity(source_id, source)
            cached_summary = self._analysis.get(identity) or self._analysis.get(source_id)
            metadata = self._analysis_metadata.get(identity, {})
            summary_status = "fresh"

            if cached_summary:
                # Exact fields are current; only the bounded language analysis may
                # lag the latest committed collection version.
                live_summary["topWords"] = deepcopy(cached_summary.get("topWords", []))
                live_summary["generatedAt"] = cached_summary.get("generatedAt", live_summary["generatedAt"])
                cached_version = int(metadata.get("data_version", 0))
                top_words_status = "fresh" if cached_version == data_version else "stale"
                as_of_job_id = metadata.get("as_of_job_id")
            elif latest_job and not latest_job.state.is_terminal:
                top_words_status = "building"
                as_of_job_id = latest_terminal.id if latest_terminal else None
            else:
                top_words_status = "building" if latest_terminal else "fresh"
                as_of_job_id = latest_terminal.id if latest_terminal else None

            partial_data = bool(
                latest_terminal
                and latest_terminal.state
                in {JobState.COMPLETED_WITH_WARNINGS, JobState.FAILED, JobState.CANCELLED}
            )
            if latest_terminal and latest_terminal.state in {JobState.FAILED, JobState.CANCELLED}:
                summary_status = "stale" if cached_summary else "failed"
                top_words_status = "stale" if cached_summary else "failed"

            return {
                "source": source,
                "latest_job": self._clone_job(latest_job) if latest_job else None,
                "summary": {
                    **deepcopy(live_summary),
                    "asOfJobId": as_of_job_id,
                    "dataVersion": data_version,
                    "status": summary_status,
                    "topWordsStatus": top_words_status,
                    "partialData": partial_data,
                    "coverage": deepcopy(metadata.get("coverage") or {"partial": partial_data}),
                },
                "top_videos": self._top_video_records(videos),
            }

    def get_source_videos_page(
        self, source_id: str, *, owner_id: str | None = None, cursor: str | None = None, limit: int = 60
    ) -> dict[str, Any]:
        with self._lock:
            self.get_source(source_id, owner_id=owner_id)
            filter_hash = source_video_filter_hash()
            decoded = decode_source_video_cursor(
                cursor,
                scope=source_id,
                filter_hash=filter_hash,
            )
            snapshot_at = decoded.snapshot_at if decoded else utcnow()
            membership = self._source_video_membership_locked(source_id)
            videos = sorted(
                (
                    self._videos[youtube_video_id]
                    for youtube_video_id, first_seen_at in membership.items()
                    if first_seen_at <= snapshot_at
                ),
                key=_video_sort_key,
                reverse=True,
            )
            total = len(videos)
            if decoded:
                cursor_key = (decoded.effective_at, decoded.youtube_video_id)
                videos = [video for video in videos if _video_sort_key(video) < cursor_key]
            candidates = videos[: limit + 1]
            has_more = len(candidates) > limit
            page = candidates[:limit]
            next_cursor = (
                encode_source_video_cursor(
                    page[-1],
                    snapshot_at=snapshot_at,
                    scope=source_id,
                    filter_hash=filter_hash,
                )
                if has_more and page
                else None
            )
            return {
                "videos": list(page),
                "next_cursor": next_cursor,
                "snapshot_at": snapshot_at,
                "total": total,
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

    def get_video_comment_threads(
        self, video_id: str, *, owner_id: str | None = None, cursor: str | None = None,
        limit: int = 20, sort: CommentThreadSort = "newest"
    ) -> dict[str, Any]:
        with self._lock:
            video = self._videos.get(video_id)
            if not video:
                raise NotFoundError(f"Video '{video_id}' was not found")
            visible_video_ids = self._visible_video_ids_locked(owner_id)
            if visible_video_ids is not None and video_id not in visible_video_ids:
                raise NotFoundError(f"Video '{video_id}' was not found")

            cursor_key = decode_comment_thread_cursor(cursor, sort)
            descending = sort != "oldest"
            top_level = sorted(
                (
                    comment
                    for comment in self._comments.values()
                    if comment.youtube_video_id == video_id and not comment.youtube_parent_comment_id
                ),
                key=lambda comment: _comment_thread_sort_key(comment, sort),
                reverse=descending,
            )
            if cursor_key:
                if descending:
                    top_level = [
                        comment for comment in top_level
                        if _comment_thread_sort_key(comment, sort) < cursor_key
                    ]
                else:
                    top_level = [
                        comment for comment in top_level
                        if _comment_thread_sort_key(comment, sort) > cursor_key
                    ]
            page = top_level[: limit + 1]
            has_more = len(page) > limit
            page = page[:limit]

            items: list[dict[str, Any]] = []
            for comment in page:
                replies = sorted(
                    (
                        reply
                        for reply in self._comments.values()
                        if reply.youtube_parent_comment_id == comment.youtube_comment_id
                    ),
                    key=_comment_sort_key,
                )
                items.append({
                    "comment": comment,
                    "replies_preview": replies[:2],
                    "stored_reply_count": len(replies),
                })
            return {
                "video": video,
                "items": items,
                "next_cursor": encode_comment_thread_cursor(page[-1], sort) if has_more and page else None,
            }

    def get_comment_replies(
        self, comment_id: str, *, owner_id: str | None = None, cursor: str | None = None, limit: int = 20
    ) -> dict[str, Any]:
        with self._lock:
            comment = self._comments.get(comment_id)
            if not comment:
                raise NotFoundError(f"Comment '{comment_id}' was not found")
            visible_video_ids = self._visible_video_ids_locked(owner_id)
            if visible_video_ids is not None and comment.youtube_video_id not in visible_video_ids:
                raise NotFoundError(f"Comment '{comment_id}' was not found")
            root_comment_id = comment.youtube_parent_comment_id or comment.youtube_comment_id
            cursor_key = decode_comment_cursor(cursor)
            replies = sorted(
                (
                    reply
                    for reply in self._comments.values()
                    if reply.youtube_parent_comment_id == root_comment_id
                ),
                key=_comment_sort_key,
            )
            if cursor_key:
                replies = [reply for reply in replies if _comment_sort_key(reply) > cursor_key]
            page = replies[: limit + 1]
            has_more = len(page) > limit
            page = page[:limit]
            return {
                "comments": page,
                "next_cursor": encode_comment_cursor(page[-1]) if has_more and page else None,
            }
