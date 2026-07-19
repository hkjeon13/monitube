"""Source result, video, and comment read use cases."""

from typing import Literal

from ..contracts import (
    AnalysisSummary,
    AuthorCommentResult,
    CommentDetailResponse,
    CommentRepliesResponse,
    CommentThreadItem,
    SourceOverviewResponse,
    SourceOverviewSummary,
    SourceResultsResponse,
    SourceTopVideos,
    SourceVideosPageResponse,
    VideoCommentThreadsResponse,
    VideoCommentsResponse,
)
from .base import ApplicationService
from .presenters import (
    comment_contract,
    comment_summary,
    job_contract,
    source_contract,
    video_contract,
)


class ResultService(ApplicationService):
    def get_source_results(
        self,
        source_id: str,
        *,
        owner_id: str | None = None,
    ) -> SourceResultsResponse:
        result = self.repository.get_source_results(source_id, owner_id=owner_id)
        summary = result["analysis"]
        return SourceResultsResponse(
            source=source_contract(result["source"]),
            latestJob=(
                job_contract(result["latest_job"])
                if result.get("latest_job")
                else None
            ),
            videos=[video_contract(video) for video in result["videos"]],
            commentSummary=comment_summary(summary),
            analysis=AnalysisSummary.model_validate(summary),
        )

    @staticmethod
    def _top_videos_contract(
        top_videos: dict[str, object],
    ) -> SourceTopVideos:
        return SourceTopVideos(
            views=[
                video_contract(video) for video in top_videos.get("views", [])
            ],
            likes=[
                video_contract(video) for video in top_videos.get("likes", [])
            ],
            comments=[
                video_contract(video)
                for video in top_videos.get("comments", [])
            ],
        )

    def get_source_overview(
        self,
        source_id: str,
        *,
        owner_id: str | None = None,
    ) -> SourceOverviewResponse:
        if self.derived_cache and self.derived_cache.enabled:
            source_record = self.repository.get_source(
                source_id,
                owner_id=owner_id,
            )
            version_reader = getattr(
                self.repository,
                "get_scope_data_version",
                None,
            )
            if callable(version_reader):
                data_version = version_reader(
                    target_id=source_record.target_id,
                    source_id=source_record.id,
                )
                scope_id = source_record.target_id or f"source-{source_record.id}"
                cache_key = self.derived_cache.target_summary_key(
                    scope_id,
                    data_version,
                )

                def load_derived() -> dict[str, object]:
                    loaded = self.repository.get_source_overview(
                        source_id,
                        owner_id=owner_id,
                    )
                    return {
                        "summary": SourceOverviewSummary.model_validate(
                            loaded["summary"]
                        ).model_dump(mode="json"),
                        "topVideos": self._top_videos_contract(
                            loaded.get("top_videos", {})
                        ).model_dump(mode="json"),
                    }

                cached = self.derived_cache.get_or_load(
                    cache_key,
                    load_derived,
                    ttl_seconds=45,
                )
                return SourceOverviewResponse(
                    source=source_contract(source_record),
                    latestJob=(
                        job_contract(source_record.latest_job)
                        if source_record.latest_job
                        else None
                    ),
                    summary=SourceOverviewSummary.model_validate(
                        cached["summary"]
                    ),
                    topVideos=SourceTopVideos.model_validate(cached["topVideos"]),
                )

        result = self.repository.get_source_overview(
            source_id,
            owner_id=owner_id,
        )
        return SourceOverviewResponse(
            source=source_contract(result["source"]),
            latestJob=(
                job_contract(result["latest_job"])
                if result.get("latest_job")
                else None
            ),
            summary=SourceOverviewSummary.model_validate(result["summary"]),
            topVideos=self._top_videos_contract(result.get("top_videos", {})),
        )

    def get_source_videos_page(
        self,
        source_id: str,
        *,
        owner_id: str | None = None,
        cursor: str | None = None,
        limit: int = 60,
    ) -> SourceVideosPageResponse:
        result = self.repository.get_source_videos_page(
            source_id,
            owner_id=owner_id,
            cursor=cursor,
            limit=limit,
        )
        return SourceVideosPageResponse(
            videos=[video_contract(video) for video in result["videos"]],
            nextCursor=result.get("next_cursor"),
            snapshotAt=result["snapshot_at"],
            total=result["total"],
        )

    def get_video_comments(
        self,
        video_id: str,
        *,
        owner_id: str | None = None,
    ) -> VideoCommentsResponse:
        result = self.repository.get_video_comments(video_id, owner_id=owner_id)
        return VideoCommentsResponse(
            video=video_contract(result["video"]),
            comments=[
                comment_contract(comment) for comment in result["comments"]
            ],
            summary=comment_summary(result["summary"]),
        )

    def get_video_comment_threads(
        self,
        video_id: str,
        *,
        owner_id: str | None = None,
        cursor: str | None = None,
        limit: int = 20,
        sort: Literal["newest", "oldest", "recommended"] = "newest",
    ) -> VideoCommentThreadsResponse:
        result = self.repository.get_video_comment_threads(
            video_id,
            owner_id=owner_id,
            cursor=cursor,
            limit=limit,
            sort=sort,
        )
        return VideoCommentThreadsResponse(
            video=video_contract(result["video"]),
            sort=sort,
            items=[
                CommentThreadItem(
                    comment=comment_contract(item["comment"]),
                    repliesPreview=[
                        comment_contract(reply)
                        for reply in item["replies_preview"]
                    ],
                    storedReplyCount=item["stored_reply_count"],
                )
                for item in result["items"]
            ],
            nextCursor=result.get("next_cursor"),
        )

    def get_comment_replies(
        self,
        comment_id: str,
        *,
        owner_id: str | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> CommentRepliesResponse:
        result = self.repository.get_comment_replies(
            comment_id,
            owner_id=owner_id,
            cursor=cursor,
            limit=limit,
        )
        return CommentRepliesResponse(
            comments=[
                comment_contract(comment) for comment in result["comments"]
            ],
            nextCursor=result.get("next_cursor"),
        )

    def get_comment_detail(
        self,
        comment_id: str,
        *,
        owner_id: str | None = None,
    ) -> CommentDetailResponse:
        result = self.repository.get_comment_detail(
            comment_id,
            owner_id=owner_id,
        )
        return CommentDetailResponse(
            comment=comment_contract(result["comment"]),
            video=video_contract(result["video"]),
            parentComment=(
                comment_contract(result["parent_comment"])
                if result.get("parent_comment")
                else None
            ),
            storedReplyCount=result.get(
                "stored_reply_count",
                len(result.get("replies", [])),
            ),
            replies=[
                comment_contract(reply) for reply in result.get("replies", [])
            ],
            authorComments=[
                AuthorCommentResult(
                    comment=comment_contract(item["comment"]),
                    video=video_contract(item["video"]),
                    channelTitle=item.get("channel_title"),
                )
                for item in result["author_comments"]
            ],
        )
