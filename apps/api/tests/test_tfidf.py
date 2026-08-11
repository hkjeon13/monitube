from math import isclose, log

from monitube_api.nlp.tfidf import keyword_scores


def test_keyword_scores_use_smoothed_tfidf_and_stable_order() -> None:
    result = keyword_scores(
        [
            {"term": "희소", "total_term_frequency": 4, "document_frequency": 2},
            {"term": "보편", "total_term_frequency": 4, "document_frequency": 9},
        ],
        document_count=10,
    )

    assert [item["term"] for item in result] == ["희소", "보편"]
    assert isclose(
        float(result[0]["score"]),
        (1 + log(4)) * (log(11 / 3) + 1),
        rel_tol=1e-6,
    )
    assert result[0]["documentRate"] == 20.0


def test_keyword_scores_drop_single_document_noise_in_a_real_corpus() -> None:
    result = keyword_scores(
        [{"term": "일회성", "total_term_frequency": 9, "document_frequency": 1}],
        document_count=20,
    )

    assert result == []
