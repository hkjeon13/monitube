"""Small leased worker for transcript/comment noun indexing."""

from __future__ import annotations

from collections import Counter
import logging
from typing import Any

from monitube_api.nlp import ANALYZER_VERSION, get_noun_analyzer


class NlpIndexWorker:
    def __init__(self, repository: Any, *, worker_id: str, lease_seconds: int) -> None:
        self.repository = repository
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.analyzer = get_noun_analyzer()
        self.logger = logging.getLogger(__name__)

    def run_one(self) -> bool:
        document = self.repository.claim_next_nlp_document(
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
        )
        if not document:
            return False
        source_kind = document["source_kind"]
        source_id = document["source_id"]
        try:
            if document["action"] == "delete":
                self.repository.complete_nlp_document(
                    source_kind=source_kind,
                    source_id=source_id,
                    source_hash=document["source_hash"],
                    analyzer_version=ANALYZER_VERSION,
                    terms={},
                    delete=True,
                )
                return True

            terms = Counter(self.analyzer.extract(document["text"]))
            segment_terms = {
                int(segment["sequence"]): self.analyzer.extract(segment["text"])
                for segment in document.get("segments", [])
            }
            self.repository.complete_nlp_document(
                source_kind=source_kind,
                source_id=source_id,
                source_hash=document["source_hash"],
                analyzer_version=ANALYZER_VERSION,
                terms=terms,
                segment_terms=segment_terms,
            )
            return True
        except Exception as exc:
            self.logger.exception(
                "NLP indexing failed for %s/%s", source_kind, source_id
            )
            self.repository.fail_nlp_document(
                source_kind=source_kind,
                source_id=source_id,
                error_code=f"{type(exc).__name__}: {exc}",
            )
            return True
