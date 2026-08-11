"""Deterministic TF-IDF scoring over persisted aggregate statistics."""

from __future__ import annotations

from math import log
from typing import Iterable, Mapping, Any


def keyword_scores(
    rows: Iterable[Mapping[str, Any]],
    *,
    document_count: int,
    limit: int = 15,
    minimum_document_count: int = 2,
) -> list[dict[str, int | float | str]]:
    """Score aggregate term statistics without loading source documents."""

    minimum = 1 if document_count < minimum_document_count else minimum_document_count
    scored: list[dict[str, int | float | str]] = []
    for row in rows:
        term = str(row.get("term") or "").strip()
        term_count = max(0, int(row.get("total_term_frequency") or 0))
        term_documents = max(0, int(row.get("document_frequency") or 0))
        if not term or term_count <= 0 or term_documents < minimum:
            continue
        tf = 1.0 + log(term_count)
        idf = log((max(0, document_count) + 1) / (term_documents + 1)) + 1.0
        scored.append(
            {
                "term": term,
                "score": round(tf * idf, 6),
                "termCount": term_count,
                "documentCount": term_documents,
                "documentRate": round(
                    term_documents / document_count * 100,
                    2,
                )
                if document_count
                else 0.0,
            }
        )
    scored.sort(
        key=lambda item: (
            -float(item["score"]),
            -int(item["documentCount"]),
            -int(item["termCount"]),
            str(item["term"]),
        )
    )
    return scored[: max(0, limit)]
