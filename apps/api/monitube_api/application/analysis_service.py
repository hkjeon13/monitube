"""Workspace-wide video and public-comment analysis use cases."""

from datetime import datetime

from ..contracts import AnalysisOverviewResponse
from .base import ApplicationService


class AnalysisService(ApplicationService):
    def get_analysis_overview(
        self,
        *,
        owner_id: str,
        scope: str = "all",
        target_ids: list[str] | None = None,
        channel_ids: list[str] | None = None,
        from_at: datetime | None = None,
        to_at: datetime | None = None,
        comment_type: str = "all",
        limit: int = 10,
    ) -> AnalysisOverviewResponse:
        return AnalysisOverviewResponse.model_validate(
            self.repository.get_analysis_overview(
                owner_id=owner_id,
                scope=scope,
                target_ids=target_ids or [],
                channel_ids=channel_ids or [],
                from_at=from_at,
                to_at=to_at,
                comment_type=comment_type,
                limit=limit,
            )
        )
