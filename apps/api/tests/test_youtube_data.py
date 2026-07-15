from urllib.parse import parse_qs, urlsplit

import pytest

from monitube_api.domain import QuotaBucket
from monitube_worker.youtube_data import YouTubeApiError, YouTubeDataClient


def test_client_uses_configured_base_url_and_keeps_key_out_of_error_message() -> None:
    requests: list[str] = []

    def transport(url: str, _timeout: float):
        requests.append(url)
        return 403, {"error": {"errors": [{"reason": "quotaExceeded"}]}}

    client = YouTubeDataClient("secret-api-key", base_url="http://youtube.test/v3", transport=transport)
    with pytest.raises(YouTubeApiError) as raised:
        client.search(part="snippet", q="FastAPI")

    query = parse_qs(urlsplit(requests[0]).query)
    assert query["key"] == ["secret-api-key"]
    assert raised.value.bucket is QuotaBucket.SEARCH_QUERIES
    assert "secret-api-key" not in str(raised.value)
