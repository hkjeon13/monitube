"""Channel, keyword, and video discovery/detail collection phases."""

from typing import Any, Iterable, Mapping

from monitube_api.channel_resolution import resolve_channel_input
from monitube_api.domain import JobRecord, VideoRecord, new_id, utcnow

from .parsing import as_int, parse_duration_seconds, parse_rfc3339


class DiscoveryCollectionMixin:
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
