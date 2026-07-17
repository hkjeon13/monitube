from fastapi import Request
from fastapi.testclient import TestClient

from monitube_api.auth import AuthUser
from monitube_api.domain import CommentRecord, JobState, VideoRecord
from monitube_api.main import create_app, get_current_user
from monitube_api.repositories import InMemoryRepository


def _multi_user_client(repository: InMemoryRepository) -> TestClient:
    """Build one API app whose authenticated user comes from a test header."""

    app = create_app(repository=repository)

    def current_user(request: Request) -> AuthUser:
        user_id = request.headers.get("X-Test-User", "user-a")
        return AuthUser(id=user_id, username=user_id)

    app.dependency_overrides[get_current_user] = current_user
    return TestClient(app)


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

    # ``POST /sources`` is now a compatibility alias for a subscription intent,
    # so the shared coordinator creates its initial target job immediately.
    source_detail = client.get(f"/v1/sources/{source['id']}")
    assert source_detail.status_code == 200
    job = source_detail.json()["latestJob"]
    assert job is not None
    assert job["state"] == "queued"
    assert job["currentStage"] == "queued"
    assert job["progress"] == {"completed": 0, "total": None, "unit": "sources"}
    assert job["resumeIsAutomatic"] is False
    assert job["partialErrors"] == []
    assert client.get(f"/v1/jobs/{job['id']}").json() == job
    listed = client.get("/v1/sources").json()
    assert listed[0]["latestJob"]["id"] == job["id"]
    assert listed[0]["latestJob"]["videoProgress"] is None
    assert listed[0]["latestJob"]["commentProgress"] is None

    updated = client.patch(f"/v1/sources/{source['id']}", json={"enabled": False})
    assert updated.status_code == 200
    assert updated.json()["enabled"] is False

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


def test_all_content_channel_request_widens_existing_target_without_preflight() -> None:
    repository = InMemoryRepository()
    client = TestClient(create_app(repository=repository))
    limited = client.post(
        "/v1/collection-requests",
        json={"type": "channel", "config": {"input": "@GoogleDevelopers", "maxVideos": 10, "includeComments": False}},
    ).json()

    all_content = client.post(
        "/v1/collection-requests",
        json={
            "type": "channel",
            "config": {
                "input": "@GoogleDevelopers",
                "includeComments": True,
                "collectAllVideos": True,
                "collectAllComments": True,
            },
        },
    )

    assert all_content.status_code == 201
    assert all_content.json()["targetId"] == limited["targetId"]
    source = all_content.json()["source"]
    assert source["config"]["collectAllVideos"] is True
    assert source["config"]["collectAllComments"] is True


def test_channel_collection_is_pinned_for_automatic_refresh_by_default() -> None:
    repository = InMemoryRepository()
    client = TestClient(create_app(repository=repository))

    collected = client.post(
        "/v1/collection-requests",
        json={"type": "channel", "config": {"input": "@GoogleDevelopers", "includeComments": True}},
    ).json()
    pin = client.get(f"/v1/collection-targets/{collected['targetId']}/pin")

    assert pin.status_code == 200
    assert pin.json()["enabled"] is True
    assert pin.json()["intervalMinutes"] == 360

    repository.claim_next_job(worker_id="test-worker")
    repository.transition_job(collected["job"]["id"], JobState.COMPLETED)
    assert repository.dispatch_due_pins() == 1


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
    # Legacy target-pin writes map to the caller's subscription.  Cadence is
    # shared service policy, not a per-user option.
    assert pin.json()["intervalMinutes"] == 360

    assert repository.dispatch_due_pins() == 1
    assert any(job.target_id == collected["targetId"] and job.state is JobState.QUEUED for job in repository._jobs.values())
    assert client.get("/v1/explore").status_code == 200


def test_deleting_a_subscription_keeps_shared_target_data_and_disables_the_last_pin() -> None:
    repository = InMemoryRepository()
    client = TestClient(create_app(repository=repository))
    created = client.post(
        "/v1/collection-requests",
        json={"type": "channel", "config": {"input": "@GoogleDevelopers", "includeComments": True}},
    ).json()
    client.put(
        f"/v1/collection-targets/{created['targetId']}/pin",
        json={"enabled": True, "intervalMinutes": 60},
    )

    deleted = client.delete(f"/v1/sources/{created['source']['id']}")

    assert deleted.status_code == 204
    assert client.get("/v1/sources").json() == []
    # A source row returned by the API is a subscription.  Removing the last
    # subscription must not delete shared target/content, only its refresh pin.
    assert created["targetId"] in repository._targets
    assert repository._pins[created["targetId"]]["enabled"] is False


def test_same_target_creates_one_subscription_per_user_without_cross_user_idempotency_replay() -> None:
    repository = InMemoryRepository()
    client = _multi_user_client(repository)
    first_headers = {"X-Test-User": "user-a", "Idempotency-Key": "browser-click-1"}
    second_headers = {"X-Test-User": "user-b", "Idempotency-Key": "browser-click-1"}
    payload = {
        "type": "channel",
        "config": {"input": "@GoogleDevelopers", "includeComments": True, "maxVideos": 25},
    }

    first = client.post("/v1/collection-requests", headers=first_headers, json=payload)
    second = client.post("/v1/collection-requests", headers=second_headers, json=payload)

    assert first.status_code == 201
    assert second.status_code == 201
    first_body = first.json()
    second_body = second.json()
    assert first_body["targetId"] == second_body["targetId"]
    assert first_body["id"] != second_body["id"]
    assert first_body["source"]["id"] != second_body["source"]["id"]
    assert first_body["job"]["id"] == second_body["job"]["id"]

    first_sources = client.get("/v1/sources", headers={"X-Test-User": "user-a"})
    second_sources = client.get("/v1/sources", headers={"X-Test-User": "user-b"})
    assert [source["id"] for source in first_sources.json()] == [first_body["source"]["id"]]
    assert [source["id"] for source in second_sources.json()] == [second_body["source"]["id"]]

    # Public IDs are subscription IDs: another subscriber cannot read, mutate,
    # or detach a different user's Sources entry.
    first_source_url = f"/v1/sources/{first_body['source']['id']}"
    assert client.get(first_source_url, headers={"X-Test-User": "user-b"}).status_code == 404
    assert client.patch(first_source_url, headers={"X-Test-User": "user-b"}, json={"enabled": False}).status_code == 404
    assert client.delete(first_source_url, headers={"X-Test-User": "user-b"}).status_code == 404

    assert client.delete(first_source_url, headers={"X-Test-User": "user-a"}).status_code == 204
    assert client.get("/v1/sources", headers={"X-Test-User": "user-a"}).json() == []
    assert [source["id"] for source in client.get("/v1/sources", headers={"X-Test-User": "user-b"}).json()] == [
        second_body["source"]["id"]
    ]
    assert first_body["targetId"] in repository._targets

    # Deleting a subscription consumes its old idempotency key.  An explicit
    # re-add with a retried browser request must create a new subscription,
    # never replay the detached worker source from the audit request.
    readded = client.post("/v1/collection-requests", headers=first_headers, json=payload)
    assert readded.status_code == 201
    readded_body = readded.json()
    assert readded_body["targetId"] == first_body["targetId"]
    assert readded_body["source"]["id"] != first_body["source"]["id"]
    assert [source["id"] for source in client.get("/v1/sources", headers={"X-Test-User": "user-a"}).json()] == [
        readded_body["source"]["id"]
    ]


def test_legacy_target_pin_toggle_cannot_disable_another_users_subscription() -> None:
    repository = InMemoryRepository()
    client = _multi_user_client(repository)
    payload = {"type": "channel", "config": {"input": "@GoogleDevelopers", "includeComments": True}}
    first = client.post("/v1/collection-requests", headers={"X-Test-User": "user-a"}, json=payload).json()
    second = client.post("/v1/collection-requests", headers={"X-Test-User": "user-b"}, json=payload).json()

    paused = client.put(
        f"/v1/collection-targets/{first['targetId']}/pin",
        headers={"X-Test-User": "user-a"},
        json={"enabled": False, "intervalMinutes": 15},
    )

    assert paused.status_code == 200
    # User B is still subscribed, so the aggregate target refresh remains on.
    assert paused.json()["enabled"] is True
    assert client.get(f"/v1/sources/{first['source']['id']}", headers={"X-Test-User": "user-a"}).json()["enabled"] is False
    assert client.get(f"/v1/sources/{second['source']['id']}", headers={"X-Test-User": "user-b"}).json()["enabled"] is True


def test_explore_search_and_comment_reads_are_scoped_to_subscribed_targets() -> None:
    repository = InMemoryRepository()
    client = _multi_user_client(repository)

    def submit_video(owner_id: str, video_id: str) -> dict[str, object]:
        response = client.post(
            "/v1/collection-requests",
            headers={"X-Test-User": owner_id},
            json={"type": "video", "config": {"input": video_id, "includeComments": True}},
        )
        assert response.status_code == 201
        return response.json()

    first = submit_video("user-a", "dQw4w9WgXcQ")
    second = submit_video("user-b", "9bZkp7q19f0")

    def worker_source_id(target_id: str) -> str:
        return next(source.id for source in repository._sources.values() if source.target_id == target_id)

    alpha = repository.upsert_video(
        VideoRecord(
            id="video-alpha-row",
            youtube_video_id="dQw4w9WgXcQ",
            youtube_channel_id="UCalpha",
            title="alpha private video",
            description="Visible only through user A's target",
            published_at=None,
            duration_seconds=None,
            privacy_status="public",
            made_for_kids=False,
            statistics={},
            source_fetched_at=repository._sources[worker_source_id(str(first["targetId"]))].created_at,
        )
    )
    beta = repository.upsert_video(
        VideoRecord(
            id="video-beta-row",
            youtube_video_id="9bZkp7q19f0",
            youtube_channel_id="UCbeta",
            title="beta private video",
            description="Visible only through user B's target",
            published_at=None,
            duration_seconds=None,
            privacy_status="public",
            made_for_kids=False,
            statistics={},
            source_fetched_at=repository._sources[worker_source_id(str(second["targetId"]))].created_at,
        )
    )
    repository.link_source_video(worker_source_id(str(first["targetId"])), alpha.youtube_video_id)
    repository.link_source_video(worker_source_id(str(second["targetId"])), beta.youtube_video_id)
    repository.upsert_comment(
        CommentRecord(
            id="alpha-comment-row",
            youtube_comment_id="alpha-comment",
            youtube_video_id=alpha.youtube_video_id,
            youtube_parent_comment_id=None,
            youtube_thread_id="alpha-thread",
            text_display="alpha comment text",
            like_count=0,
            published_at=None,
            updated_at=None,
            source_fetched_at=alpha.source_fetched_at,
        )
    )

    user_a_headers = {"X-Test-User": "user-a"}
    user_b_headers = {"X-Test-User": "user-b"}
    a_search = client.get("/v1/search", headers=user_a_headers, params={"q": "alpha"})
    b_search = client.get("/v1/search", headers=user_b_headers, params={"q": "alpha"})

    assert [item["video"]["id"] for item in a_search.json()["videos"]] == [alpha.youtube_video_id]
    assert [item["comment"]["id"] for item in a_search.json()["comments"]] == ["alpha-comment"]
    assert b_search.json()["videos"] == []
    assert b_search.json()["comments"] == []
    assert client.get(f"/v1/videos/{alpha.youtube_video_id}/comments", headers=user_b_headers).status_code == 404
    assert client.get("/v1/comments/alpha-comment", headers=user_b_headers).status_code == 404
    assert client.get(f"/v1/sources/{first['source']['id']}/results", headers=user_b_headers).status_code == 404
