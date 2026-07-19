"""Characterization tests for the public HTTP surface."""

import json

from monitube_api.main import create_app


EXPECTED_OPERATIONS = {
    ("GET", "/health"),
    ("GET", "/ready"),
    ("POST", "/register/key"),
    ("POST", "/v1/auth/login"),
    ("POST", "/v1/auth/logout"),
    ("GET", "/v1/auth/me"),
    ("POST", "/v1/auth/register"),
    ("POST", "/v1/channel-resolutions"),
    ("GET", "/v1/channels/{youtube_channel_id}/subscriber-history"),
    ("POST", "/v1/collection-requests"),
    ("GET", "/v1/collection-targets/{target_id}/pin"),
    ("PUT", "/v1/collection-targets/{target_id}/pin"),
    ("GET", "/v1/comments/{comment_id}"),
    ("GET", "/v1/comments/{comment_id}/replies"),
    ("GET", "/v1/explore"),
    ("GET", "/v1/explore/channels"),
    ("GET", "/v1/explore/videos"),
    ("GET", "/v1/jobs/active"),
    ("GET", "/v1/jobs/recent-failures"),
    ("GET", "/v1/jobs/{job_id}"),
    ("GET", "/v1/search"),
    ("GET", "/v1/sources"),
    ("POST", "/v1/sources"),
    ("DELETE", "/v1/sources/{source_id}"),
    ("GET", "/v1/sources/{source_id}"),
    ("PATCH", "/v1/sources/{source_id}"),
    ("GET", "/v1/sources/{source_id}/jobs"),
    ("POST", "/v1/sources/{source_id}/jobs"),
    ("GET", "/v1/sources/{source_id}/overview"),
    ("GET", "/v1/sources/{source_id}/results"),
    ("GET", "/v1/sources/{source_id}/videos"),
    ("POST", "/v1/video-resolutions"),
    ("GET", "/v1/videos/{video_id}/comment-threads"),
    ("GET", "/v1/videos/{video_id}/comments"),
}


def test_openapi_operations_are_stable() -> None:
    schema = create_app().openapi()
    actual = {
        (method.upper(), path)
        for path, path_item in schema["paths"].items()
        for method in path_item
    }

    assert actual == EXPECTED_OPERATIONS


def test_public_v1_contract_does_not_expose_server_credentials() -> None:
    schema = create_app().openapi()
    public_paths = {
        path: path_item
        for path, path_item in schema["paths"].items()
        if path.startswith("/v1/")
    }
    serialized = json.dumps(public_paths)

    for forbidden_name in (
        "apiKeys",
        "apiKey",
        "credentialId",
        "projectId",
        "secretRef",
    ):
        assert forbidden_name not in serialized
