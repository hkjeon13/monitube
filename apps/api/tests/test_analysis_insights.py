from datetime import UTC, datetime, timedelta

from monitube_api.analysis import question_signals_from_texts
from monitube_api.analysis_insights import build_video_insights


def test_video_insights_build_growth_benchmarks_and_heatmap() -> None:
    latest_at = datetime(2026, 7, 30, tzinfo=UTC)
    baseline_at = latest_at - timedelta(days=7)
    rows = []
    for index, view_count in enumerate((100, 200, 300, 400, 2000), start=1):
        rows.append(
            {
                "id": f"video-{index}",
                "channel_id": "UCmetrics",
                "channel_title": "Metrics",
                "title": f"Video {index}",
                "published_at": latest_at - timedelta(days=10),
                "statistics_fetched_at": latest_at,
                "view_count": view_count,
                "like_count": view_count // 10,
                "youtube_comment_count": view_count // 100,
                "collected_comment_count": view_count // 200,
                "baseline_fetched_at": baseline_at,
                "baseline_view_count": max(0, view_count - index * 10),
                "baseline_like_count": 0,
                "baseline_comment_count": 0,
            }
        )

    result = build_video_insights(rows, limit=10, generated_at=latest_at)

    assert result["performanceSummary"]["videoCount"] == 5
    assert result["performanceSummary"]["comparableVideoCount"] == 5
    assert result["performanceSummary"]["snapshotEligible7d"] == 5
    assert result["performanceSummary"]["totalViewGrowth7d"] == 150
    assert result["performanceVideos"][0]["viewGrowth7d"] == 50
    assert result["publishingHeatmap"][0]["videoCount"] == 5
    assert any(item["kind"] == "growth" for item in result["insights"])
    assert any(item["kind"] == "breakout" for item in result["insights"])


def test_video_insights_groups_publishing_heatmap_in_three_hour_kst_buckets() -> None:
    generated_at = datetime(2026, 8, 13, 12, tzinfo=UTC)
    published_times = (
        datetime(2026, 8, 9, 15, 0, tzinfo=UTC),   # Mon 00:00 KST
        datetime(2026, 8, 9, 17, 59, tzinfo=UTC),  # Mon 02:59 KST
        datetime(2026, 8, 9, 18, 0, tzinfo=UTC),   # Mon 03:00 KST
        datetime(2026, 8, 10, 12, 0, tzinfo=UTC),  # Mon 21:00 KST
        datetime(2026, 8, 10, 14, 59, tzinfo=UTC), # Mon 23:59 KST
        datetime(2026, 8, 10, 15, 0, tzinfo=UTC),  # Tue 00:00 KST
    )
    rows = [
        {
            "id": f"boundary-{index}",
            "channel_id": "UCboundary",
            "published_at": published_at,
            "statistics_fetched_at": generated_at,
            "view_count": 100 * index,
            "like_count": 0,
            "youtube_comment_count": 0,
            "collected_comment_count": 0,
        }
        for index, published_at in enumerate(published_times, start=1)
    ]

    result = build_video_insights(rows, limit=10, generated_at=generated_at)

    cells = {
        (cell["weekday"], cell["hourBucket"]): cell
        for cell in result["publishingHeatmap"]
    }
    assert cells[(1, 0)]["videoCount"] == 2
    assert cells[(1, 3)]["videoCount"] == 1
    assert cells[(1, 21)]["videoCount"] == 2
    assert cells[(2, 0)]["videoCount"] == 1


def test_question_signal_is_bounded_and_transparent() -> None:
    result = question_signals_from_texts(
        ["이 영상은 어떻게 만들었나요?", "좋아요", None, ""]
    )

    assert result == {
        "questionCount": 1,
        "questionSampleSize": 2,
        "questionRate": 50.0,
    }
