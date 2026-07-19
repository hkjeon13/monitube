"""Explore, pin, channel history, and search use cases."""

from ..contracts import (
    ChannelSubscriberSnapshot,
    ExploreChannelsResponse,
    ExploreResponse,
    ExploreVideosPageResponse,
    SearchCommentResult,
    SearchVideoResult,
    TargetPin,
    TargetPinUpdate,
    UnifiedSearchResponse,
)
from ..fuzzy_search import normalize_search_text
from .base import ApplicationService
from .presenters import comment_contract, pin_contract, video_contract


class InvalidSearchQueryError(ValueError):
    """A search query that becomes too short after normalization."""


class ExploreService(ApplicationService):
    def _explore_cache_generation(self, owner_id: str) -> int | str:
        reader = getattr(self.repository, "get_owner_explore_generation", None)
        if callable(reader):
            return reader(owner_id=owner_id)
        if self.derived_cache:
            return self.derived_cache.owner_generation(owner_id)
        return 0

    def set_target_pin(
        self,
        target_id: str,
        request: TargetPinUpdate,
    ) -> TargetPin:
        return pin_contract(
            self.repository.set_target_pin(
                target_id=target_id,
                enabled=request.enabled,
                interval_minutes=request.intervalMinutes,
            )
        )

    def get_target_pin(self, target_id: str) -> TargetPin | None:
        pin = self.repository.get_target_pin(target_id=target_id)
        return pin_contract(pin) if pin else None

    def explore(
        self,
        *,
        owner_id: str | None = None,
        channel_id: str | None = None,
        offset: int = 0,
        limit: int = 60,
    ) -> ExploreResponse:
        result = self.repository.list_explore(
            channel_id=channel_id,
            owner_id=owner_id,
            offset=offset,
            limit=limit,
        )
        channels = []
        for item in result["channels"]:
            channel = dict(item)
            pin = channel.pop("pin", None)
            channels.append(
                {**channel, "pin": pin_contract(pin) if pin else None}
            )
        return ExploreResponse(
            channels=channels,
            videos=[video_contract(video) for video in result["videos"]],
            nextOffset=result.get("next_offset"),
        )

    def explore_channels(
        self,
        *,
        owner_id: str | None = None,
    ) -> ExploreChannelsResponse:
        def load() -> dict[str, object]:
            channels = []
            for item in self.repository.list_explore_channels(owner_id=owner_id):
                channel = dict(item)
                pin = channel.pop("pin", None)
                channels.append(
                    {**channel, "pin": pin_contract(pin) if pin else None}
                )
            return ExploreChannelsResponse(channels=channels).model_dump(
                mode="json"
            )

        if (
            self.derived_cache
            and self.derived_cache.enabled
            and owner_id is not None
        ):
            filter_hash = self.derived_cache.filter_hash({"kind": "channels"})
            key = self.derived_cache.owner_explore_key(
                owner_id,
                filter_hash,
                self._explore_cache_generation(owner_id),
            )
            return ExploreChannelsResponse.model_validate(
                self.derived_cache.get_or_load(key, load, ttl_seconds=45)
            )
        return ExploreChannelsResponse.model_validate(load())

    def explore_videos_page(
        self,
        *,
        owner_id: str | None = None,
        channel_id: str | None = None,
        cursor: str | None = None,
        limit: int = 60,
    ) -> ExploreVideosPageResponse:
        def load() -> dict[str, object]:
            result = self.repository.list_explore_videos_page(
                owner_id=owner_id,
                channel_id=channel_id,
                cursor=cursor,
                limit=limit,
            )
            return ExploreVideosPageResponse(
                videos=[video_contract(video) for video in result["videos"]],
                nextCursor=result.get("next_cursor"),
                snapshotAt=result["snapshot_at"],
                total=result["total"],
            ).model_dump(mode="json")

        if (
            self.derived_cache
            and self.derived_cache.enabled
            and owner_id is not None
        ):
            filter_hash = self.derived_cache.filter_hash(
                {
                    "kind": "videos",
                    "channelId": channel_id,
                    "cursor": cursor,
                    "limit": limit,
                }
            )
            key = self.derived_cache.owner_explore_key(
                owner_id,
                filter_hash,
                self._explore_cache_generation(owner_id),
            )
            return ExploreVideosPageResponse.model_validate(
                self.derived_cache.get_or_load(key, load, ttl_seconds=45)
            )
        return ExploreVideosPageResponse.model_validate(load())

    def channel_subscriber_history(
        self,
        youtube_channel_id: str,
        *,
        owner_id: str | None = None,
    ) -> list[ChannelSubscriberSnapshot]:
        return [
            ChannelSubscriberSnapshot.model_validate(item)
            for item in self.repository.list_channel_subscriber_history(
                youtube_channel_id=youtube_channel_id,
                owner_id=owner_id,
            )
        ]

    def search_collected(
        self,
        query: str,
        *,
        owner_id: str | None = None,
        limit: int = 20,
        scope: str = "all",
    ) -> UnifiedSearchResponse:
        normalized_query = normalize_search_text(query)
        if len(normalized_query) <= 1:
            raise InvalidSearchQueryError(
                "Search query must contain at least two normalized characters"
            )
        result = self.repository.search_collected(
            query=query,
            limit=limit,
            owner_id=owner_id,
            scope=scope,
        )
        if len(normalized_query) == 2:
            result = {**result, "comments": []}
        return UnifiedSearchResponse(
            query=query,
            videos=[
                SearchVideoResult(
                    video=video_contract(item["video"]),
                    score=item["score"],
                    matchedFields=item["matched_fields"],
                )
                for item in result["videos"]
            ],
            comments=[
                SearchCommentResult(
                    comment=comment_contract(item["comment"]),
                    video=video_contract(item["video"]),
                    channelTitle=item.get("channel_title"),
                    score=item["score"],
                    matchedFields=item["matched_fields"],
                )
                for item in result["comments"]
            ],
        )
