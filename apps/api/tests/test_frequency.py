from monitube_api.nlp.frequency import keyword_frequencies


def test_keyword_frequencies_rank_by_total_occurrences() -> None:
    result = keyword_frequencies(
        [
            {"term": "희소", "total_term_frequency": 4, "document_frequency": 2},
            {"term": "보편", "total_term_frequency": 12, "document_frequency": 9},
        ],
        document_count=10,
    )

    assert [item["term"] for item in result] == ["보편", "희소"]
    assert result[0]["termCount"] == 12
    assert result[0]["documentRate"] == 90.0


def test_keyword_frequencies_keep_single_document_terms() -> None:
    result = keyword_frequencies(
        [{"term": "일회성", "total_term_frequency": 9, "document_frequency": 1}],
        document_count=20,
    )

    assert result == [
        {
            "term": "일회성",
            "termCount": 9,
            "documentCount": 1,
            "documentRate": 5.0,
        }
    ]


def test_keyword_frequencies_ignore_document_count_when_frequency_ties() -> None:
    result = keyword_frequencies(
        [
            {"term": "다", "total_term_frequency": 3, "document_frequency": 3},
            {"term": "가", "total_term_frequency": 3, "document_frequency": 1},
            {"term": "나", "total_term_frequency": 3, "document_frequency": 2},
        ],
        document_count=3,
        limit=2,
    )

    assert [item["term"] for item in result] == ["가", "나"]
