"""Channel, keyword, and video discovery/detail collection phases."""

from datetime import UTC, datetime, timedelta
from typing import Any, Iterable, Mapping

from monitube_api.channel_resolution import resolve_channel_input
from monitube_api.domain import JobRecord, VideoRecord, new_id, utcnow

from .parsing import as_int, parse_duration_seconds, parse_rfc3339


_YOUTUBE_PUBLICATION_EPOCH = datetime(2005, 4, 23, tzinfo=UTC)
_KEYWORD_BACKFILL_VERSION = 1


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class DiscoveryCollectionMixin:
    def _resolve_channel(self, job: JobRecord, input_value: str) -> Mapping[str, Any]:
        if getattr(self, "discovery_provider", "youtube") == "searchapi":
            return self._resolve_channel_searchapi(job, input_value)
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

    def _resolve_channel_searchapi(
        self, job: JobRecord, input_value: str
    ) -> Mapping[str, Any]:
        client = getattr(self, "discovery_client", None)
        if client is None:
            raise RuntimeError(
                "SearchAPI.io discovery is enabled but SEARCH_API_KEY is not configured"
            )
        resolution = resolve_channel_input(input_value)
        channel_input = resolution.normalized
        if resolution.requires_search:
            search_payload = self._searchapi_call(
                job,
                "youtube",
                client.youtube,
                query=resolution.normalized,
            )
            candidates = search_payload.get("channels") or []
            if not candidates:
                for section in search_payload.get("sections") or []:
                    for item in section.get("items") or []:
                        if item.get("id") and not item.get("length"):
                            candidates.append(item)
            if not candidates or not candidates[0].get("id"):
                raise RuntimeError("No YouTube channel matched this source input")
            channel_input = str(candidates[0]["id"])
        payload = self._searchapi_call(
            job,
            "youtube_channel",
            client.channel,
            channel_id=channel_input,
        )
        channel = payload.get("channel") or {}
        about = payload.get("about") or {}
        channel_id = channel.get("id")
        if not channel_id:
            raise RuntimeError("SearchAPI.io channel response did not contain an ID")
        fetched_at = utcnow()
        video_count = as_int(channel.get("videos") or about.get("videos"))
        self.repository.upsert_channel(
            {
                "youtube_channel_id": str(channel_id),
                "handle": channel.get("handle"),
                "title": channel.get("title"),
                "description": channel.get("description") or about.get("description"),
                "thumbnail_url": channel.get("avatar"),
                "uploads_playlist_id": None,
                "statistics": {
                    "subscriberCount": as_int(channel.get("subscribers") or about.get("subscribers")),
                    "viewCount": as_int(channel.get("views") or about.get("views")),
                    "videoCount": video_count,
                    "hiddenSubscriberCount": False,
                },
                "source_attribution": "searchapi_youtube_channel",
                "provider_profile": {
                    "provider": "searchapi_youtube_channel",
                    "keywords": channel.get("keywords"),
                    "tags": channel.get("tags") or [],
                    "available_countries": channel.get("available_countries") or [],
                    "badges": channel.get("badges") or [],
                    "is_verified": channel.get("is_verified"),
                    "is_family_safe": channel.get("is_family_safe"),
                    "banner_url": channel.get("banner"),
                    "avatar_url": channel.get("avatar"),
                    "external_links": about.get("links") or [],
                    "joined_date": about.get("joined_date"),
                },
                "source_fetched_at": fetched_at,
            }
        )
        self.repository.promote_channel_target(
            source_id=job.source_id,
            youtube_channel_id=str(channel_id),
            handle=channel.get("handle"),
        )
        return {
            "id": str(channel_id),
            "snippet": {
                "customUrl": channel.get("handle"),
                "title": channel.get("title"),
                "description": channel.get("description") or about.get("description"),
            },
            "statistics": {"videoCount": video_count},
        }

    def _channel_video_ids(
        self, job: JobRecord, source_config: Mapping[str, Any], *, incremental_refresh: bool
    ) -> tuple[list[str], dict[str, VideoRecord], bool]:
        if getattr(self, "discovery_provider", "youtube") == "searchapi":
            return self._channel_video_ids_searchapi(
                job, source_config, incremental_refresh=incremental_refresh
            )
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

    def _channel_video_ids_searchapi(
        self,
        job: JobRecord,
        source_config: Mapping[str, Any],
        *,
        incremental_refresh: bool,
    ) -> tuple[list[str], dict[str, VideoRecord], bool]:
        client = getattr(self, "discovery_client", None)
        if client is None:
            raise RuntimeError(
                "SearchAPI.io discovery is enabled but SEARCH_API_KEY is not configured"
            )
        channel = self._resolve_channel(job, str(source_config["input"]))
        channel_id = str(channel["id"])
        collect_all = bool(source_config.get("collectAllVideos"))
        limit = None if collect_all else job.max_videos or as_int(source_config.get("maxVideos")) or 50
        expected_video_count = as_int((channel.get("statistics") or {}).get("videoCount"))
        stored_video_count = self.repository.count_videos_by_channel(channel_id)
        backfill_required = bool(collect_all and expected_video_count > stored_video_count)
        ids: list[str] = []
        known_videos: dict[str, VideoRecord] = {}
        page_token: str | None = None
        page_count = 0
        while limit is None or len(ids) < limit:
            payload = self._searchapi_call(
                job,
                "youtube_channel_videos",
                client.channel_videos,
                channel_id=channel_id,
                next_page_token=page_token,
            )
            page_count += 1
            page_ids = [
                str(item["id"])
                for item in payload.get("videos") or []
                if item.get("id")
            ]
            page_ids = list(dict.fromkeys(page_ids))
            for video_id in page_ids:
                if video_id not in ids:
                    ids.append(video_id)
                    if limit is not None and len(ids) >= limit:
                        break
            existing = self.repository.get_videos_by_youtube_ids(page_ids)
            known_videos.update(existing)
            page_token = (payload.get("pagination") or {}).get("next_page_token")
            self._checkpoint(
                job,
                stage="searchapi_channel_videos",
                scope_key=channel_id,
                page_token=page_token,
                batch_cursor=page_count,
            )
            if (
                incremental_refresh
                and not backfill_required
                and collect_all
                and page_ids
                and len(existing) == len(page_ids)
            ):
                break
            if not page_ids or not page_token:
                break
        if backfill_required:
            ids.reverse()
        return ids, known_videos, backfill_required

    @staticmethod
    def _searchapi_keyword_page_ids(payload: Mapping[str, Any]) -> list[str]:
        ids = [
            str(item["id"])
            for item in payload.get("videos") or []
            if item.get("id")
        ]
        for section in payload.get("sections") or []:
            section_name = str(
                section.get("section_name") or section.get("section_title") or ""
            ).lower()
            if "short" not in section_name:
                continue
            ids.extend(
                str(item["id"])
                for item in section.get("items") or []
                if item.get("id")
            )
        return list(dict.fromkeys(ids))

    def _keyword_video_ids_searchapi(
        self, job: JobRecord, source_config: Mapping[str, Any]
    ) -> list[str]:
        client = getattr(self, "discovery_client", None)
        if client is None:
            raise RuntimeError(
                "SearchAPI.io discovery is enabled but SEARCH_API_KEY is not configured"
            )
        max_pages = max(1, as_int(source_config.get("maxPagesPerRun")) or 1)
        ids: list[str] = []
        page_token: str | None = None
        for page in range(1, max_pages + 1):
            payload = self._searchapi_call(
                job,
                "youtube",
                client.youtube,
                query=str(source_config["query"]),
                page_token=page_token,
            )
            page_ids = self._searchapi_keyword_page_ids(payload)
            ids.extend(video_id for video_id in page_ids if video_id not in ids)
            page_token = (payload.get("pagination") or {}).get("next_page_token")
            self._checkpoint(
                job,
                stage="searchapi_keyword",
                scope_key=str(source_config["query"]),
                page_token=page_token,
                batch_cursor=page,
            )
            if not page_ids or not page_token:
                break
        self._active_checkpoint["keywordHistoricalBackfillComplete"] = True
        if page_token:
            self._active_checkpoint["keywordCoverage"] = "limited"
        return ids

    def _keyword_video_ids_incremental(self, job: JobRecord, source_config: Mapping[str, Any]) -> list[str]:
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

    def _keyword_backfill_state(
        self, job: JobRecord, source_config: Mapping[str, Any]
    ) -> dict[str, Any]:
        existing = job.checkpoint.get("keywordBackfill")
        if isinstance(existing, dict) and existing.get("version") == _KEYWORD_BACKFILL_VERSION:
            return dict(existing)

        lower_bound = parse_rfc3339(source_config.get("publishedAfter")) or _YOUTUBE_PUBLICATION_EPOCH
        upper_bound = parse_rfc3339(source_config.get("publishedBefore")) or utcnow()
        return {
            "version": _KEYWORD_BACKFILL_VERSION,
            "after": _rfc3339(lower_bound),
            # Freeze the upper edge for this multi-day job. New uploads are picked
            # up by the normal incremental refresh after the historical run ends.
            "before": _rfc3339(upper_bound),
            "pageToken": None,
            "page": 0,
            "expectedTotal": 0,
            "batchIds": [],
            "oldestPublishedAt": None,
            "lastBoundary": None,
            "discoveredIds": [],
            "complete": upper_bound <= lower_bound,
        }

    def _checkpoint_keyword_backfill(
        self, job: JobRecord, source_config: Mapping[str, Any], state: Mapping[str, Any]
    ) -> None:
        self._active_checkpoint["keywordBackfill"] = dict(state)
        self._checkpoint(
            job,
            stage="keyword_history",
            scope_key=str(source_config["query"]),
            page_token=str(state.get("pageToken") or "") or None,
            batch_cursor=as_int(state.get("page")),
        )

    def _keyword_video_ids_historical(
        self, job: JobRecord, source_config: Mapping[str, Any]
    ) -> list[str]:
        """Walk a newest-first keyword result set backwards until its lower bound.

        YouTube can stop returning ``nextPageToken`` while ``totalResults`` still
        reports many more matches. At that boundary, start a new search whose
        exclusive upper bound is the oldest timestamp just observed. Page results
        are fanned out before the cursor is committed, so a quota pause can resume
        days later without replaying every newer search page.
        """

        state = self._keyword_backfill_state(job, source_config)
        lower_bound = parse_rfc3339(state.get("after")) or _YOUTUBE_PUBLICATION_EPOCH
        if bool(state.get("complete")):
            self._active_checkpoint["keywordHistoricalBackfillComplete"] = True
            return [str(value) for value in state.get("discoveredIds", [])]
        # Persist the frozen upper bound before the first upstream call. A quota
        # failure on that call must not move the boundary to a later retry time.
        self._checkpoint_keyword_backfill(job, source_config, state)

        while True:
            upper_bound = parse_rfc3339(state.get("before")) or utcnow()
            if upper_bound <= lower_bound:
                state["complete"] = True
                self._active_checkpoint["keywordHistoricalBackfillComplete"] = True
                self._checkpoint_keyword_backfill(job, source_config, state)
                return [str(value) for value in state.get("discoveredIds", [])]

            payload = self._call(
                job,
                "search",
                part="snippet",
                type="video",
                q=source_config["query"],
                # Historical completeness requires deterministic newest-first
                # traversal even when the source's presentation order is relevance.
                order="date",
                publishedAfter=_rfc3339(lower_bound),
                publishedBefore=_rfc3339(upper_bound),
                regionCode=source_config.get("regionCode"),
                relevanceLanguage=source_config.get("relevanceLanguage"),
                maxResults=50,
                pageToken=state.get("pageToken"),
            )

            response_total = as_int((payload.get("pageInfo") or {}).get("totalResults"))
            state["expectedTotal"] = max(as_int(state.get("expectedTotal")), response_total)
            if response_total:
                self._active_checkpoint["keywordExpectedTotal"] = max(
                    as_int(self._active_checkpoint.get("keywordExpectedTotal")),
                    response_total,
                )

            page_ids: list[str] = []
            oldest = parse_rfc3339(state.get("oldestPublishedAt"))
            for item in payload.get("items", []):
                video_id = (item.get("id") or {}).get("videoId")
                if video_id and video_id not in page_ids:
                    page_ids.append(video_id)
                published_at = parse_rfc3339((item.get("snippet") or {}).get("publishedAt"))
                if published_at and (oldest is None or published_at < oldest):
                    oldest = published_at

            batch_ids = list(dict.fromkeys([
                *(str(value) for value in state.get("batchIds", [])),
                *page_ids,
            ]))
            state["batchIds"] = batch_ids
            state["oldestPublishedAt"] = _rfc3339(oldest) if oldest else None
            state["page"] = as_int(state.get("page")) + 1
            state["pageToken"] = payload.get("nextPageToken")

            known_on_page = self.repository.source_video_ids(job.source_id, page_ids)
            new_page_ids = [video_id for video_id in page_ids if video_id not in known_on_page]
            if job.target_id is not None:
                self.repository.enqueue_video_jobs(parent_job=job, youtube_video_ids=new_page_ids)
            else:
                state["discoveredIds"] = list(dict.fromkeys([
                    *(str(value) for value in state.get("discoveredIds", [])),
                    *new_page_ids,
                ]))

            self._checkpoint_keyword_backfill(job, source_config, state)
            if job.target_id is not None:
                total, terminal, _failed = self.repository.child_job_summary(parent_job_id=job.id)
                self._set_phase_progress(
                    job,
                    phase="videos",
                    completed=terminal,
                    total=total,
                    current_stage="backfilling_keyword_history",
                )

            if state.get("pageToken"):
                continue

            # ``totalResults`` is approximate, but a value vastly larger than the
            # returned unique IDs is the only durable signal that pagination ended
            # before the historical range did. Continue below the oldest result.
            truncated = as_int(state.get("expectedTotal")) > len(batch_ids)
            if truncated and oldest and oldest > lower_bound:
                boundary = _rfc3339(oldest)
                # Include the boundary second once so videos sharing that timestamp
                # are not lost. If the provider returns the exact same boundary
                # again, switch to the exclusive timestamp to guarantee progress.
                next_upper = oldest + timedelta(seconds=1)
                if state.get("lastBoundary") == boundary or next_upper >= upper_bound:
                    next_upper = oldest
                if next_upper > lower_bound:
                    state.update({
                        "before": _rfc3339(next_upper),
                        "pageToken": None,
                        "page": 0,
                        "expectedTotal": 0,
                        "batchIds": [],
                        "oldestPublishedAt": None,
                        "lastBoundary": boundary,
                    })
                    self._checkpoint_keyword_backfill(job, source_config, state)
                    continue

            state["complete"] = True
            self._active_checkpoint["keywordHistoricalBackfillComplete"] = True
            self._checkpoint_keyword_backfill(job, source_config, state)
            return [str(value) for value in state.get("discoveredIds", [])]

    def _keyword_video_ids(
        self,
        job: JobRecord,
        source_config: Mapping[str, Any],
        *,
        historical_backfill: bool,
    ) -> list[str]:
        if getattr(self, "discovery_provider", "youtube") == "searchapi":
            return self._keyword_video_ids_searchapi(job, source_config)
        if historical_backfill:
            return self._keyword_video_ids_historical(job, source_config)
        self._active_checkpoint["keywordHistoricalBackfillComplete"] = True
        return self._keyword_video_ids_incremental(job, source_config)

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
                completed=len(records),
                total=len(distinct_ids),
                current_stage="fetching_videos",
            )
        return records
