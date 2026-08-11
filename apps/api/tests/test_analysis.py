from monitube_api import analysis
from monitube_api.analysis import top_words_from_texts


def test_top_words_keeps_common_and_proper_nouns_only() -> None:
    result = top_words_from_texts(
        [
            "진짜 그냥 지금 영화를 보고 웃었다",
            "영화를 많이 보고 또 웃었다",
            "배우가 연기했다",
        ],
        limit=10,
    )

    counts = {item["word"]: item["count"] for item in result}
    assert counts["영화"] == 2
    assert counts["배우"] == 1
    assert counts["연기"] == 1
    assert {
        "보다",
        "웃다",
        "진짜",
        "그냥",
        "지금",
        "많이",
        "또",
    }.isdisjoint(counts)


def test_top_words_excludes_adverbs_and_other_grammatical_tokens() -> None:
    assert top_words_from_texts(
        ["진짜 그냥 지금 근데 많이 이런 아니라"],
        limit=10,
    ) == []


def test_top_words_uses_the_required_noun_analyzer(monkeypatch) -> None:
    class RequiredAnalyzer:
        @staticmethod
        def extract(_text: str) -> list[str]:
            return ["정상명사"]

    monkeypatch.setattr(analysis, "get_noun_analyzer", RequiredAnalyzer)

    assert top_words_from_texts(["공개 댓글"]) == [
        {"word": "정상명사", "count": 1}
    ]
