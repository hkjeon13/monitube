from fastapi.testclient import TestClient

from monitube_tokenizer.main import create_app


class FakeAnalyzer:
    version = "mecab-nltk-v1"

    def extract(self, text: str | None) -> list[str]:
        return (text or "").casefold().split()


def test_tokenize_returns_tokens_only_without_bow_or_frequency() -> None:
    with TestClient(create_app(FakeAnalyzer())) as client:
        response = client.post(
            "/internal/v1/tokenize",
            json={
                "analyzerVersion": "mecab-nltk-v1",
                "documents": [
                    {
                        "id": "doc-1",
                        "text": "Alpha beta alpha",
                        "segments": [{"sequence": 0, "text": "Alpha beta"}],
                    }
                ],
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload == {
        "analyzerVersion": "mecab-nltk-v1",
        "documents": [
            {
                "id": "doc-1",
                "tokens": ["alpha", "beta", "alpha"],
                "segments": [
                    {"sequence": 0, "tokens": ["alpha", "beta"]}
                ],
            }
        ],
    }
    assert "termFrequency" not in response.text
    assert "documentFrequency" not in response.text


def test_contract_rejects_unknown_fields_and_wrong_version() -> None:
    with TestClient(create_app(FakeAnalyzer())) as client:
        unknown = client.post(
            "/internal/v1/tokenize",
            json={
                "analyzerVersion": "mecab-nltk-v1",
                "documents": [],
                "unexpected": True,
            },
        )
        wrong_version = client.post(
            "/internal/v1/tokenize",
            json={"analyzerVersion": "different", "documents": [{"id": "x", "text": "x"}]},
        )

    assert unknown.status_code == 422
    assert wrong_version.status_code == 422
