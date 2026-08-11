from urllib.parse import parse_qs, urlparse

from monitube_worker.searchapi import SearchApiClient, SearchApiError


def test_searchapi_client_uses_bearer_header_and_never_places_key_in_url() -> None:
    captured: dict[str, object] = {}

    def transport(url, method, headers, body, timeout):
        captured.update(url=url, method=method, headers=headers, body=body, timeout=timeout)
        return 200, {"videos": []}

    client = SearchApiClient("top-secret", transport=transport)
    client.youtube(query="한국 뉴스")

    assert "top-secret" not in str(captured["url"])
    assert captured["headers"]["Authorization"] == "Bearer top-secret"
    query = parse_qs(urlparse(str(captured["url"])).query)
    assert query["engine"] == ["youtube"]
    assert query["q"] == ["한국 뉴스"]


def test_searchapi_client_surfaces_safe_provider_error() -> None:
    def transport(_url, _method, _headers, _body, _timeout):
        return 429, {"error": {"code": "rate_limit"}}

    client = SearchApiClient("top-secret", transport=transport)
    try:
        client.channel(channel_id="UCexample")
    except SearchApiError as exc:
        assert exc.status_code == 429
        assert exc.error_code == "rate_limit"
        assert "top-secret" not in str(exc)
    else:
        raise AssertionError("SearchApiError was not raised")


def test_transcript_language_miss_returns_safe_language_options() -> None:
    def transport(_url, _method, _headers, _body, _timeout):
        return 400, {
            "error": "Selected language has not been transcribed",
            "available_languages": [{"lang": "en", "name": "English"}],
            "search_metadata": {"request_url": "https://example.invalid/opaque-token"},
        }

    client = SearchApiClient("top-secret", transport=transport)
    payload = client.transcripts(
        video_id="dQw4w9WgXcQ", language="ko", transcript_type="manual"
    )

    assert payload == {"available_languages": [{"lang": "en", "name": "English"}]}
