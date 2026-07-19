from monitube_api.collection_policy import (
    coverage_satisfies,
    desired_coverage,
    merge_collection_config,
)
from monitube_api.domain import SourceType


def test_merge_collection_config_only_widens_coverage() -> None:
    current = {
        "input": "@stable-handle",
        "includeComments": False,
        "collectAllVideos": False,
        "maxVideos": 10,
        "maxCommentPagesPerVideo": 1,
    }
    incoming = {
        "input": "@different-display-value",
        "includeComments": True,
        "collectAllVideos": True,
        "maxVideos": 100,
        "maxCommentPagesPerVideo": 3,
    }

    merged = merge_collection_config(SourceType.CHANNEL, current, incoming)

    assert merged == {
        "input": "@stable-handle",
        "includeComments": True,
        "collectAllVideos": True,
        "collectAllComments": False,
        "maxVideos": 100,
        "maxCommentPagesPerVideo": 3,
    }
    assert current["maxVideos"] == 10


def test_completed_coverage_must_include_requested_comment_depth() -> None:
    desired = desired_coverage(
        SourceType.VIDEO,
        {
            "includeComments": True,
            "collectAllComments": False,
            "maxCommentPagesPerVideo": 3,
        },
    )

    assert not coverage_satisfies(
        {
            "complete": True,
            "includeComments": True,
            "maxCommentPagesPerVideo": 2,
        },
        desired,
    )
    assert coverage_satisfies(
        {
            "complete": True,
            "includeComments": True,
            "maxCommentPagesPerVideo": 3,
        },
        desired,
    )
