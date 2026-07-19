"""Collected result and explore read ports."""

from typing import Any, Literal, Protocol, TypeAlias


CommentThreadSort: TypeAlias = Literal["newest", "oldest", "recommended"]


class ResultReadRepository(Protocol):
    def save_analysis_summary(self, source_id: str) -> dict[str, Any]: ...

    def get_source_results(
        self,
        source_id: str,
        *,
        owner_id: str | None = None,
    ) -> dict[str, Any]: ...

    def get_source_overview(
        self,
        source_id: str,
        *,
        owner_id: str | None = None,
    ) -> dict[str, Any]: ...

    def get_source_videos_page(
        self,
        source_id: str,
        *,
        owner_id: str | None = None,
        cursor: str | None = None,
        limit: int = 60,
    ) -> dict[str, Any]: ...

    def get_video_comments(
        self,
        video_id: str,
        *,
        owner_id: str | None = None,
    ) -> dict[str, Any]: ...

    def get_video_comment_threads(
        self,
        video_id: str,
        *,
        owner_id: str | None = None,
        cursor: str | None = None,
        limit: int = 20,
        sort: CommentThreadSort = "newest",
    ) -> dict[str, Any]: ...

    def get_comment_replies(
        self,
        comment_id: str,
        *,
        owner_id: str | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]: ...

    def get_comment_detail(
        self,
        comment_id: str,
        *,
        owner_id: str | None = None,
    ) -> dict[str, Any]: ...


class ExploreReadRepository(Protocol):
    def list_explore(
        self,
        *,
        limit: int = 60,
        offset: int = 0,
        channel_id: str | None = None,
        owner_id: str | None = None,
    ) -> dict[str, Any]: ...

    def list_explore_channels(
        self,
        *,
        owner_id: str | None = None,
    ) -> list[dict[str, Any]]: ...

    def list_explore_videos_page(
        self,
        *,
        owner_id: str | None = None,
        channel_id: str | None = None,
        cursor: str | None = None,
        limit: int = 60,
    ) -> dict[str, Any]: ...

    def list_channel_subscriber_history(
        self,
        *,
        youtube_channel_id: str,
        limit: int = 180,
        owner_id: str | None = None,
    ) -> list[dict[str, Any]]: ...

    def search_collected(
        self,
        *,
        query: str,
        limit: int = 20,
        owner_id: str | None = None,
        scope: str = "all",
    ) -> dict[str, Any]: ...
