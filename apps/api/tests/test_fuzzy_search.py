from monitube_api.fuzzy_search import rank_text_fields


def test_multi_term_search_requires_every_term() -> None:
    all_terms_score, all_terms_fields = rank_text_fields(
        "단어1 단어2", {"title": "단어1과 단어2를 모두 포함한 영상"},
    )
    one_term_score, one_term_fields = rank_text_fields(
        "단어1 단어2", {"title": "단어1만 포함한 영상"},
    )

    assert all_terms_fields == ["title"]
    assert one_term_fields == []
    assert one_term_score == 0.0
    assert all_terms_score > one_term_score
