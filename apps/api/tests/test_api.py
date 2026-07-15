from fastapi.testclient import TestClient

from monitube_api.main import create_app


def test_health_and_channel_resolution_endpoints() -> None:
    client = TestClient(create_app())

    assert client.get("/health").json() == {"status": "ok", "service": "monitube-api"}
    response = client.post("/v1/channel-resolutions", json={"input": "youtube.com/@GoogleDevelopers"})

    assert response.status_code == 200
    assert response.json()["lookup"] == {"parameter": "forHandle", "value": "@GoogleDevelopers"}

    video = client.post("/v1/video-resolutions", json={"input": "https://youtu.be/dQw4w9WgXcQ?t=42"})
    assert video.status_code == 200
    assert video.json() == {"kind": "short_url", "normalized": "dQw4w9WgXcQ"}


def test_source_and_job_contract_is_project_free() -> None:
    client = TestClient(create_app())
    source_response = client.post(
        "/v1/sources",
        json={
            "type": "channel",
            "config": {
                "input": "@GoogleDevelopers",
                "includeComments": True,
                "maxVideos": 25,
                "maxCommentPagesPerVideo": 1,
            },
        },
    )

    assert source_response.status_code == 201
    source = source_response.json()
    assert source["type"] == "channel"
    assert "projectId" not in source
    assert client.get("/v1/sources").json() == [source]

    updated = client.patch(f"/v1/sources/{source['id']}", json={"enabled": False})
    assert updated.status_code == 200
    assert updated.json()["enabled"] is False

    job_response = client.post(
        f"/v1/sources/{source['id']}/jobs",
        json={"include_comments": True, "max_comments_per_video": 3},
    )

    assert job_response.status_code == 201
    job = job_response.json()
    assert job["state"] == "queued"
    assert job["currentStage"] == "queued"
    assert job["progress"] == {"completed": 0, "total": None, "unit": "sources"}
    assert job["resumeIsAutomatic"] is False
    assert job["partialErrors"] == []
    assert client.get(f"/v1/jobs/{job['id']}").json() == job

    assert client.delete(f"/v1/sources/{source['id']}").status_code == 204
    assert client.get(f"/v1/sources/{source['id']}").status_code == 404
    assert client.get("/v1/projects").status_code == 404


def test_keyword_and_direct_video_sources_use_the_same_project_free_contract() -> None:
    client = TestClient(create_app())
    keyword = client.post(
        "/v1/sources",
        json={
            "type": "keyword",
            "config": {"query": "FastAPI", "order": "date", "maxPagesPerRun": 2, "includeComments": False, "maxCommentPagesPerVideo": 1},
        },
    )
    video = client.post(
        "/v1/sources",
        json={
            "type": "video",
            "config": {"input": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "includeComments": True, "maxCommentPagesPerVideo": 2},
        },
    )

    assert keyword.status_code == 201
    assert keyword.json()["type"] == "keyword"
    assert video.status_code == 201
    assert video.json()["type"] == "video"
    assert video.json()["config"]["input"] == "dQw4w9WgXcQ"
