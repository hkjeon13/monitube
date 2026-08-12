"""Deterministic keyword frequency ranking over persisted term aggregates."""

from __future__ import annotations

from typing import Any, Iterable, Mapping


def keyword_frequencies(
    rows: Iterable[Mapping[str, Any]],
    *,
    document_count: int,
    limit: int = 15,
) -> list[dict[str, int | float | str]]:
    """Rank terms by raw corpus frequency without TF-IDF weighting."""

    ranked: list[dict[str, int | float | str]] = []
    for row in rows:
        term = str(row.get("term") or "").strip()
        term_count = max(0, int(row.get("total_term_frequency") or 0))
        term_documents = max(0, int(row.get("document_frequency") or 0))
        if not term or term_count <= 0:
            continue
        ranked.append(
            {
                "term": term,
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

    ranked.sort(
        key=lambda item: (
            -int(item["termCount"]),
            -int(item["documentCount"]),
            str(item["term"]),
        )
    )
    return ranked[: max(0, limit)]
