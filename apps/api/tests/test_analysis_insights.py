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


def test_question_signal_is_bounded_and_transparent() -> None:
    result = question_signals_from_texts(
        ["이 영상은 어떻게 만들었나요?", "좋아요", None, ""]
    )

    assert result == {
        "questionCount": 1,
        "questionSampleSize": 2,
        "questionRate": 50.0,
    }
