from fastapi.testclient import TestClient

from monitube_api.domain import JobState
from monitube_api.main import create_app
from monitube_api.repositories import InMemoryRepository


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


def test_collection_requests_share_one_target_job_and_honor_idempotency() -> None:
    repository = InMemoryRepository()
    client = TestClient(create_app(repository=repository))
    first = client.post(
        "/v1/collection-requests",
        headers={"Idempotency-Key": "collect-google-developers-1"},
        json={
            "type": "channel",
            "config": {"input": "https://www.youtube.com/@GoogleDevelopers", "maxVideos": 10, "includeComments": False},
        },
    )
    assert first.status_code == 201
    first_body = first.json()
    assert first_body["disposition"] == "queued"
    assert first_body["source"]["targetId"] == first_body["targetId"]
    assert first_body["job"] is not None

    retry = client.post(
        "/v1/collection-requests",
        headers={"Idempotency-Key": "collect-google-developers-1"},
        json={
            "type": "channel",
            "config": {"input": "@GoogleDevelopers", "maxVideos": 10, "includeComments": False},
        },
    )
    assert retry.status_code == 201
    assert retry.json()["id"] == first_body["id"]
    assert retry.json()["job"]["id"] == first_body["job"]["id"]

    wider = client.post(
        "/v1/collection-requests",
        json={
            "type": "channel",
            "config": {"input": "@googledevelopers", "maxVideos": 50, "includeComments": True, "maxCommentPagesPerVideo": 3},
        },
    )
    assert wider.status_code == 201
    assert wider.json()["targetId"] == first_body["targetId"]
    assert wider.json()["source"]["id"] == first_body["source"]["id"]
    assert wider.json()["job"]["id"] == first_body["job"]["id"]
    assert len(client.get("/v1/sources").json()) == 1


def test_running_target_queues_one_successor_then_serves_cached_results() -> None:
    repository = InMemoryRepository()
    client = TestClient(create_app(repository=repository))
    first = client.post(
        "/v1/collection-requests",
        json={"type": "channel", "config": {"input": "@GoogleDevelopers", "maxVideos": 10}},
    ).json()
    job_id = first["job"]["id"]
    repository.claim_next_job(worker_id="test-worker")

    successor = client.post(
        "/v1/collection-requests",
        json={
            "type": "channel",
            "config": {"input": "@GoogleDevelopers", "maxVideos": 50, "includeComments": True, "maxCommentPagesPerVideo": 2},
        },
    )
    assert successor.status_code == 201
    assert successor.json()["disposition"] == "successor_queued"
    assert successor.json()["job"] is None

    repository.transition_job(job_id, JobState.COMPLETED)
    pending = next(request for request in repository._requests.values() if request.id == successor.json()["id"])
    assert pending.job_id is not None
    assert pending.job_id != job_id

    # The completed job covered only the original 10 videos, so a 10-video request
    # is cached even while a wider successor is queued.
    cached = client.post(
        "/v1/collection-requests",
        json={"type": "channel", "config": {"input": "@GoogleDevelopers", "maxVideos": 10}},
    )
    assert cached.status_code == 201
    assert cached.json()["disposition"] == "cached"


def test_keyword_requests_share_a_target_when_only_collection_breadth_changes() -> None:
    repository = InMemoryRepository()
    client = TestClient(create_app(repository=repository))

    first = client.post(
        "/v1/collection-requests",
        json={
            "type": "keyword",
            "config": {
                "query": "  FastAPI   튜토리얼 ",
                "order": "date",
                "maxPagesPerRun": 1,
                "includeComments": False,
            },
        },
    )
    wider = client.post(
        "/v1/collection-requests",
        json={
            "type": "keyword",
            "config": {
                "query": "fastapi 튜토리얼",
                "order": "date",
                "maxPagesPerRun": 4,
                "includeComments": True,
                "maxCommentPagesPerVideo": 2,
            },
        },
    )

    assert first.status_code == 201
    assert wider.status_code == 201
    assert wider.json()["targetId"] == first.json()["targetId"]
    assert wider.json()["source"]["id"] == first.json()["source"]["id"]
    assert len(client.get("/v1/sources").json()) == 1


def test_pinned_target_dispatches_a_follow_up_collection_and_explore_is_public() -> None:
    repository = InMemoryRepository()
    client = TestClient(create_app(repository=repository))
    collected = client.post(
        "/v1/collection-requests",
        json={"type": "channel", "config": {"input": "@GoogleDevelopers", "maxVideos": 10, "includeComments": True}},
    ).json()
    repository.claim_next_job(worker_id="test-worker")
    repository.transition_job(collected["job"]["id"], JobState.COMPLETED)

    pin = client.put(
        f"/v1/collection-targets/{collected['targetId']}/pin",
        json={"enabled": True, "intervalMinutes": 60},
    )
    assert pin.status_code == 200
    assert pin.json()["enabled"] is True
    assert pin.json()["intervalMinutes"] == 60

    assert repository.dispatch_due_pins() == 1
    assert any(job.target_id == collected["targetId"] and job.state is JobState.QUEUED for job in repository._jobs.values())
    assert client.get("/v1/explore").status_code == 200
