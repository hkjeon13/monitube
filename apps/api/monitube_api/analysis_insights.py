"""Deterministic, source-backed metrics for the workspace Analysis dashboard."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from statistics import median
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .domain import utcnow


SEOUL = ZoneInfo("Asia/Seoul")


def _ratio(numerator: float, denominator: float, *, scale: float = 1.0) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator * scale


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * percentile)))
    return ordered[index]


def _round_metric(value: float) -> float:
    return round(value, 2)


def build_video_insights(
    rows: Iterable[dict[str, Any]],
    *,
    limit: int = 20,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Build comparable video metrics, action cards, and publishing heatmap."""

    generated_at = generated_at or utcnow()
    metrics: list[dict[str, Any]] = []
    by_channel: dict[str, list[dict[str, Any]]] = defaultdict(list)
    publishing_groups: dict[tuple[int, int], list[float]] = defaultdict(list)

    for row in rows:
        published_at = row.get("published_at")
        statistics_fetched_at = row.get("statistics_fetched_at")
        view_count = int(row.get("view_count") or 0)
        like_count = int(row.get("like_count") or 0)
        youtube_comment_count = int(row.get("youtube_comment_count") or 0)
        collected_comment_count = int(row.get("collected_comment_count") or 0)
        age_days = 1.0
        if published_at and statistics_fetched_at:
            age_days = max(
                1.0,
                (statistics_fetched_at - published_at).total_seconds() / 86400,
            )
        views_per_day = view_count / age_days
        like_rate = _ratio(like_count, view_count, scale=100)
        comment_rate = _ratio(youtube_comment_count, view_count, scale=1000)
        engagement_rate = _ratio(
            like_count + youtube_comment_count,
            view_count,
            scale=100,
        )
        collection_coverage_rate = (
            _ratio(
                collected_comment_count,
                youtube_comment_count,
                scale=100,
            )
            if youtube_comment_count > 0
            else None
        )

        baseline_at = row.get("baseline_fetched_at")
        growth_window_days: float | None = None
        view_growth_7d: int | None = None
        like_growth_7d: int | None = None
        comment_growth_7d: int | None = None
        if baseline_at and statistics_fetched_at:
            growth_window_days = (
                statistics_fetched_at - baseline_at
            ).total_seconds() / 86400
            if 6 <= growth_window_days <= 14:
                view_growth_7d = max(
                    0,
                    view_count - int(row.get("baseline_view_count") or 0),
                )
                like_growth_7d = max(
                    0,
                    like_count - int(row.get("baseline_like_count") or 0),
                )
                comment_growth_7d = max(
                    0,
                    youtube_comment_count
                    - int(row.get("baseline_comment_count") or 0),
                )
            else:
                growth_window_days = None

        metric = {
            "id": str(row["id"]),
            "channelId": row.get("channel_id"),
            "channelTitle": row.get("channel_title"),
            "title": row.get("title"),
            "publishedAt": published_at,
            "statisticsFetchedAt": statistics_fetched_at,
            "viewCount": view_count,
            "likeCount": like_count,
            "youtubeCommentCount": youtube_comment_count,
            "collectedCommentCount": collected_comment_count,
            "ageDays": _round_metric(age_days),
            "viewsPerDay": _round_metric(views_per_day),
            "likeRate": _round_metric(like_rate),
            "commentRatePerThousand": _round_metric(comment_rate),
            "engagementRate": _round_metric(engagement_rate),
            "collectionCoverageRate": (
                _round_metric(collection_coverage_rate)
                if collection_coverage_rate is not None
                else None
            ),
            "viewGrowth7d": view_growth_7d,
            "likeGrowth7d": like_growth_7d,
            "commentGrowth7d": comment_growth_7d,
            "growthWindowDays": (
                _round_metric(growth_window_days)
                if growth_window_days is not None
                else None
            ),
            "channelMedianViewsPerDay": None,
            "channelMedianEngagementRate": None,
            "channelMedianMultiple": None,
        }
        metrics.append(metric)
        channel_key = str(row.get("channel_id") or "unknown")
        by_channel[channel_key].append(metric)

        if published_at:
            local_published = published_at.astimezone(SEOUL)
            hour_bucket = (local_published.hour // 6) * 6
            publishing_groups[
                (local_published.isoweekday(), hour_bucket)
            ].append(views_per_day)

    comparable_video_count = 0
    for channel_metrics in by_channel.values():
        if len(channel_metrics) < 5:
            continue
        median_views = median(
            item["viewsPerDay"] for item in channel_metrics
        )
        median_engagement = median(
            item["engagementRate"] for item in channel_metrics
        )
        for item in channel_metrics:
            item["channelMedianViewsPerDay"] = _round_metric(median_views)
            item["channelMedianEngagementRate"] = _round_metric(
                median_engagement
            )
            item["channelMedianMultiple"] = (
                _round_metric(item["viewsPerDay"] / median_views)
                if median_views > 0
                else None
            )
            comparable_video_count += 1

    views_per_day_values = [item["viewsPerDay"] for item in metrics]
    like_rate_values = [item["likeRate"] for item in metrics]
    comment_rate_values = [
        item["commentRatePerThousand"] for item in metrics
    ]
    growth_metrics = [
        item for item in metrics if item["viewGrowth7d"] is not None
    ]
    view_p75 = _percentile(views_per_day_values, 0.75)

    cards: list[dict[str, Any]] = []
    if growth_metrics:
        fastest = max(growth_metrics, key=lambda item: item["viewGrowth7d"])
        cards.append(
            {
                "id": "fastest-growth",
                "kind": "growth",
                "tone": "positive",
                "title": "최근 7일 조회 성장이 가장 큽니다",
                "description": (
                    f"{fastest.get('title') or fastest['id']} 영상이 "
                    f"{fastest['viewGrowth7d']:,}회 증가했습니다."
                ),
                "videoId": fastest["id"],
                "value": float(fastest["viewGrowth7d"]),
                "unit": "views",
            }
        )

    breakout_candidates = [
        item
        for item in metrics
        if item["viewCount"] >= 100
        and (item["channelMedianMultiple"] or 0) >= 2
    ]
    if breakout_candidates:
        breakout = max(
            breakout_candidates,
            key=lambda item: item["channelMedianMultiple"],
        )
        cards.append(
            {
                "id": "channel-breakout",
                "kind": "breakout",
                "tone": "positive",
                "title": "채널 평소보다 빠르게 조회되고 있습니다",
                "description": (
                    f"{breakout.get('title') or breakout['id']} 영상의 조회 "
                    f"효율이 채널 중앙값의 "
                    f"{breakout['channelMedianMultiple']:.1f}배입니다."
                ),
                "videoId": breakout["id"],
                "value": float(breakout["channelMedianMultiple"]),
                "unit": "multiple",
            }
        )

    opportunity_candidates = [
        item
        for item in metrics
        if item["viewCount"] >= 100
        and item["viewsPerDay"] >= view_p75
        and item["channelMedianEngagementRate"] is not None
        and item["engagementRate"] < item["channelMedianEngagementRate"]
    ]
    if opportunity_candidates:
        opportunity = max(
            opportunity_candidates,
            key=lambda item: item["viewsPerDay"],
        )
        cards.append(
            {
                "id": "engagement-opportunity",
                "kind": "opportunity",
                "tone": "attention",
                "title": "조회 대비 참여를 높일 기회가 있습니다",
                "description": (
                    f"{opportunity.get('title') or opportunity['id']} 영상은 "
                    "조회 효율 상위권이지만 참여율은 채널 중앙값보다 낮습니다."
                ),
                "videoId": opportunity["id"],
                "value": float(opportunity["engagementRate"]),
                "unit": "percent",
            }
        )

    conversation_candidates = [
        item for item in metrics if item["viewCount"] >= 100
    ]
    if conversation_candidates:
        conversation = max(
            conversation_candidates,
            key=lambda item: item["commentRatePerThousand"],
        )
        cards.append(
            {
                "id": "conversation-leader",
                "kind": "conversation",
                "tone": "neutral",
                "title": "대화를 가장 많이 만든 영상입니다",
                "description": (
                    f"{conversation.get('title') or conversation['id']} 영상은 "
                    "조회 1천 회당 "
                    f"{conversation['commentRatePerThousand']:.1f}개의 댓글이 "
                    "발생했습니다."
                ),
                "videoId": conversation["id"],
                "value": float(
                    conversation["commentRatePerThousand"]
                ),
                "unit": "comments_per_thousand",
            }
        )

    total_youtube_comments = sum(
        item["youtubeCommentCount"] for item in metrics
    )
    total_collected_comments = sum(
        item["collectedCommentCount"] for item in metrics
    )
    aggregate_coverage = (
        _ratio(
            total_collected_comments,
            total_youtube_comments,
            scale=100,
        )
        if total_youtube_comments > 0
        else None
    )
    if aggregate_coverage is not None and aggregate_coverage < 80:
        cards.append(
            {
                "id": "collection-coverage",
                "kind": "quality",
                "tone": "attention",
                "title": "댓글 해석에 수집 범위를 함께 확인하세요",
                "description": (
                    "YouTube 표시 댓글 대비 현재 저장된 댓글 비율은 "
                    f"{aggregate_coverage:.1f}%입니다."
                ),
                "videoId": None,
                "value": float(aggregate_coverage),
                "unit": "percent",
            }
        )

    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    ranking_sets = (
        sorted(
            growth_metrics,
            key=lambda item: item["viewGrowth7d"],
            reverse=True,
        ),
        sorted(metrics, key=lambda item: item["viewsPerDay"], reverse=True),
        sorted(metrics, key=lambda item: item["engagementRate"], reverse=True),
        sorted(
            metrics,
            key=lambda item: item["commentRatePerThousand"],
            reverse=True,
        ),
    )
    per_ranking = max(3, limit // len(ranking_sets))
    for ranking in ranking_sets:
        added = 0
        for item in ranking:
            if item["id"] in seen:
                continue
            selected.append(item)
            seen.add(item["id"])
            added += 1
            if len(selected) >= limit:
                break
            if added >= per_ranking:
                break
        if len(selected) >= limit:
            break
    if len(selected) < limit:
        for item in sorted(
            metrics,
            key=lambda row: row["viewsPerDay"],
            reverse=True,
        ):
            if item["id"] in seen:
                continue
            selected.append(item)
            seen.add(item["id"])
            if len(selected) >= limit:
                break

    heatmap = [
        {
            "weekday": weekday,
            "hourBucket": hour_bucket,
            "videoCount": len(values),
            "medianViewsPerDay": _round_metric(median(values)),
        }
        for (weekday, hour_bucket), values in sorted(
            publishing_groups.items()
        )
    ]

    return {
        "performanceSummary": {
            "videoCount": len(metrics),
            "comparableVideoCount": comparable_video_count,
            "snapshotEligible7d": len(growth_metrics),
            "medianViewsPerDay": _round_metric(
                median(views_per_day_values)
                if views_per_day_values
                else 0
            ),
            "medianLikeRate": _round_metric(
                median(like_rate_values) if like_rate_values else 0
            ),
            "medianCommentRatePerThousand": _round_metric(
                median(comment_rate_values) if comment_rate_values else 0
            ),
            "totalViewGrowth7d": sum(
                item["viewGrowth7d"] for item in growth_metrics
            ),
            "collectionCoverageRate": (
                _round_metric(aggregate_coverage)
                if aggregate_coverage is not None
                else None
            ),
        },
        "insights": cards[:5],
        "performanceVideos": selected,
        "publishingHeatmap": heatmap,
        "coverage": {
            "generatedAt": generated_at,
            "videoCount": len(metrics),
            "comparableVideoCount": comparable_video_count,
            "snapshotEligible7d": len(growth_metrics),
        },
    }
