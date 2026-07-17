"""Small dependency-free fuzzy matching utilities for collected public data."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping


_WORD_RE = re.compile(r"[^\w]+", re.UNICODE)


def normalize_search_text(value: str) -> str:
    """Normalize case, compatibility characters and spacing for tolerant matching."""

    return _WORD_RE.sub("", unicodedata.normalize("NFKC", value).casefold())


def jaro_winkler_similarity(left: str, right: str) -> float:
    """Return the Jaro-Winkler similarity of two already-normalized strings."""

    if left == right:
        return 1.0
    if not left or not right:
        return 0.0

    match_distance = max(len(left), len(right)) // 2 - 1
    left_matches = [False] * len(left)
    right_matches = [False] * len(right)
    matches = 0

    for left_index, character in enumerate(left):
        start = max(0, left_index - match_distance)
        end = min(left_index + match_distance + 1, len(right))
        for right_index in range(start, end):
            if right_matches[right_index] or character != right[right_index]:
                continue
            left_matches[left_index] = True
            right_matches[right_index] = True
            matches += 1
            break

    if matches == 0:
        return 0.0

    transpositions = 0
    right_index = 0
    for left_index, matched in enumerate(left_matches):
        if not matched:
            continue
        while not right_matches[right_index]:
            right_index += 1
        if left[left_index] != right[right_index]:
            transpositions += 1
        right_index += 1

    jaro = (
        matches / len(left)
        + matches / len(right)
        + (matches - transpositions / 2) / matches
    ) / 3
    prefix = 0
    for left_character, right_character in zip(left, right):
        if left_character != right_character or prefix == 4:
            break
        prefix += 1
    return jaro + prefix * 0.1 * (1 - jaro)


def text_similarity(query: str, value: str | None) -> float:
    """Score a query against text, including a windowed match for long comments."""

    if not value:
        return 0.0
    normalized_query = normalize_search_text(query)
    normalized_value = normalize_search_text(value)
    if not normalized_query or not normalized_value:
        return 0.0
    if normalized_query in normalized_value:
        return 1.0

    # Compare words and short windows, so one typo in a long Korean comment can
    # still match without letting unrelated long text dominate the score.
    candidates = [normalize_search_text(word) for word in re.findall(r"\w+", value, flags=re.UNICODE)]
    window_min = max(1, len(normalized_query) - 2)
    window_max = min(len(normalized_value), len(normalized_query) + 2)
    for window_size in range(window_min, window_max + 1):
        candidates.extend(
            normalized_value[index:index + window_size]
            for index in range(0, max(0, len(normalized_value) - window_size + 1))
        )
    candidates.append(normalized_value)
    return max((jaro_winkler_similarity(normalized_query, candidate) for candidate in candidates if candidate), default=0.0)


def rank_text_fields(query: str, fields: Mapping[str, str | None], *, threshold: float = 0.79) -> tuple[float, list[str]]:
    """Return an AND search score, requiring every whitespace-separated term."""

    terms = re.findall(r"\S+", query) or [query]
    if len(terms) > 1:
        normalized_values = [normalize_search_text(value) for value in fields.values() if value]
        if not all(
            normalized_term and any(normalized_term in normalized_value for normalized_value in normalized_values)
            for normalized_term in (normalize_search_text(term) for term in terms)
        ):
            return (0.0, [])

    best_by_term = [0.0] * len(terms)
    matched: list[str] = []
    for field, value in fields.items():
        scores = [text_similarity(term, value) for term in terms]
        for index, score in enumerate(scores):
            best_by_term[index] = max(best_by_term[index], score)
        if any(score >= threshold for score in scores):
            matched.append(field)

    if any(score < threshold for score in best_by_term):
        return (0.0, [])

    score = sum(best_by_term) / len(terms)
    return (score, matched)
