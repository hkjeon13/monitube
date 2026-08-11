from types import SimpleNamespace

from monitube_worker import nlp_worker


class RepositoryStub:
    def __init__(self, document):
        self.document = document
        self.completed = None
        self.failed = None

    def claim_next_nlp_document(self, **_kwargs):
        document, self.document = self.document, None
        return document

    def complete_nlp_document(self, **kwargs):
        self.completed = kwargs

    def fail_nlp_document(self, **kwargs):
        self.failed = kwargs


def test_nlp_worker_indexes_document_and_each_transcript_segment(monkeypatch) -> None:
    analyzer = SimpleNamespace(
        version="test-v1",
        extract=lambda text: ["분석", "분석"] if "전체" in text else ["구간"],
    )
    monkeypatch.setattr(nlp_worker, "get_noun_analyzer", lambda: analyzer)
    repository = RepositoryStub(
        {
            "source_kind": "transcript",
            "source_id": "source-id",
            "source_hash": "hash",
            "action": "index",
            "text": "전체 대본",
            "segments": [{"sequence": 0, "text": "첫 구간"}],
        }
    )

    worker = nlp_worker.NlpIndexWorker(repository, worker_id="worker", lease_seconds=60)

    assert worker.run_one() is True
    assert repository.completed["terms"] == {"분석": 2}
    assert repository.completed["segment_terms"] == {0: ["구간"]}
    assert repository.failed is None


def test_nlp_worker_deletes_without_analyzing(monkeypatch) -> None:
    monkeypatch.setattr(
        nlp_worker,
        "get_noun_analyzer",
        lambda: SimpleNamespace(version="test-v1", extract=lambda _text: []),
    )
    repository = RepositoryStub(
        {
            "source_kind": "comment",
            "source_id": "source-id",
            "source_hash": "hash",
            "action": "delete",
        }
    )

    worker = nlp_worker.NlpIndexWorker(repository, worker_id="worker", lease_seconds=60)

    assert worker.run_one() is True
    assert repository.completed["delete"] is True
