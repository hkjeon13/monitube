"""Workspace-wide video and public-comment analysis use cases."""

from datetime import datetime

from ..contracts import (
    AnalysisExcludedTermsResponse,
    AnalysisInsightsResponse,
    AnalysisOverviewResponse,
)
from .base import ApplicationService


class AnalysisService(ApplicationService):
    def list_analysis_excluded_terms(
        self,
        *,
        owner_id: str,
    ) -> AnalysisExcludedTermsResponse:
        return AnalysisExcludedTermsResponse.model_validate(
            self.repository.list_analysis_excluded_terms(owner_id=owner_id)
        )

    def replace_analysis_excluded_terms(
        self,
        *,
        owner_id: str,
        corpus_kind: str,
        terms: list[str],
    ) -> AnalysisExcludedTermsResponse:
        return AnalysisExcludedTermsResponse.model_validate(
            self.repository.replace_analysis_excluded_terms(
                owner_id=owner_id,
                corpus_kind=corpus_kind,
                terms=terms,
            )
        )

    def get_analysis_insights(
        self,
        *,
        owner_id: str,
        scope: str = "all",
        target_ids: list[str] | None = None,
        channel_ids: list[str] | None = None,
        from_at: datetime | None = None,
        to_at: datetime | None = None,
        limit: int = 20,
    ) -> AnalysisInsightsResponse:
        return AnalysisInsightsResponse.model_validate(
            self.repository.get_analysis_insights(
                owner_id=owner_id,
                scope=scope,
                target_ids=target_ids or [],
                channel_ids=channel_ids or [],
                from_at=from_at,
                to_at=to_at,
                limit=limit,
            )
        )

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
        section: str = "all",
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
                section=section,
                limit=limit,
            )
        )
