from monitube_api.domain import JobState, QuotaBucket
from monitube_api.quota import YoutubeErrorCategory, classify_youtube_error, extract_youtube_error_reasons


def test_quota_exhaustion_waits_on_the_same_bucket() -> None:
    classification = classify_youtube_error(403, ["quotaExceeded"], quota_bucket=QuotaBucket.SEARCH_QUERIES)

    assert classification.category is YoutubeErrorCategory.QUOTA_EXHAUSTED
    assert classification.suggested_state is JobState.WAITING_QUOTA
    assert classification.quota_bucket is QuotaBucket.SEARCH_QUERIES
    assert classification.retryable is True


def test_rate_limit_and_server_failures_are_retryable() -> None:
    assert classify_youtube_error(429).suggested_state is JobState.WAITING_RETRY
    assert classify_youtube_error(503).suggested_state is JobState.WAITING_RETRY


def test_extracts_google_error_reasons() -> None:
    payload = {"error": {"errors": [{"reason": "dailyLimitExceeded"}, {"reason": "quotaExceeded"}]}}

    assert extract_youtube_error_reasons(payload) == ("dailyLimitExceeded", "quotaExceeded")
