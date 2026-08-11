"""Polling worker for server-managed YouTube collection jobs."""

from __future__ import annotations

import logging
import os
import signal
import hashlib
from threading import Event

from monitube_api.settings import Settings, create_repository

from .collector import YouTubeCollector
from .runner import JobRunner
from .nlp_worker import NlpIndexWorker
from .searchapi import SearchApiClient
from .youtube_data import RotatingYouTubeDataClient


shutdown_requested = Event()


def _request_shutdown(_: int, __: object) -> None:
    shutdown_requested.set()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    logger = logging.getLogger(__name__)
    signal.signal(signal.SIGINT, _request_shutdown)
    signal.signal(signal.SIGTERM, _request_shutdown)
    settings = Settings.from_environment()
    repository, runtime_config_id = create_repository(settings)
    worker_id = os.getenv(
        "WORKER_ID", f"worker-{os.getenv('HOSTNAME', 'local')}-{os.getpid()}"
    )
    nlp_worker = None
    if settings.enable_nlp_indexing and hasattr(repository, "claim_next_nlp_document"):
        # Construction is deliberately fail-fast. Production must never switch
        # silently to PeCab, regex, Kiwi, or another analyzer.
        nlp_worker = NlpIndexWorker(
            repository,
            worker_id=f"{worker_id}-nlp",
            lease_seconds=settings.nlp_index_lease_seconds,
        )
        stale = repository.enqueue_stale_nlp_documents(
            analyzer_version=nlp_worker.analyzer.version
        )
        if stale:
            logger.info("Queued %s stale NLP document(s) for reindexing", stale)

    def process_nlp_batch() -> int:
        if nlp_worker is None:
            return 0
        processed = 0
        for _ in range(settings.nlp_index_batch_size):
            if not nlp_worker.run_one():
                break
            processed += 1
        if processed:
            logger.info("Indexed %s NLP document(s)", processed)
        return processed

    if not settings.youtube_api_key:
        logger.warning("YOUTUBE_API_KEY is not configured; only NLP indexing will run.")
        while not shutdown_requested.wait(timeout=settings.worker_poll_seconds):
            process_nlp_batch()
        close = getattr(repository, "close", None)
        if callable(close):
            close()
        logger.info("Monitube worker stopped.")
        return

    # Compose replicas commonly all run as PID 1. Include the container hostname
    # so their leases and initial key-pool positions stay distinct.
    client = RotatingYouTubeDataClient(
        settings.youtube_api_keys,
        base_url=settings.youtube_api_base_url,
        timeout_seconds=settings.youtube_api_timeout_seconds,
    )
    searchapi_client = (
        SearchApiClient(
            settings.searchapi_api_key,
            base_url=settings.searchapi_base_url,
            timeout_seconds=settings.searchapi_timeout_seconds,
            gl=settings.searchapi_gl,
            hl=settings.searchapi_hl,
            zero_retention=settings.searchapi_zero_retention,
            channel_token_post_threshold_bytes=settings.searchapi_channel_token_post_threshold_bytes,
        )
        if settings.searchapi_api_key
        else None
    )
    if settings.discovery_provider == "searchapi" and searchapi_client is None:
        logger.warning(
            "DISCOVERY_PROVIDER=searchapi but SEARCH_API_KEY is not configured; discovery jobs will fail closed."
        )
    # Spread independently started workers across the configured pool. Failover
    # still rotates normally, and replace_keys preserves this selected key when
    # runtime-registered keys are loaded before a claimed job.
    for _ in range(
        int(hashlib.sha256(worker_id.encode("utf-8")).hexdigest(), 16)
        % client.key_count
    ):
        client.rotate()
    collector = YouTubeCollector(
        repository,
        client,
        discovery_provider=settings.discovery_provider,
        discovery_client=searchapi_client,
        transcript_client=searchapi_client,
        transcript_collection_enabled=settings.transcript_collection_enabled,
        transcript_primary_language=settings.transcript_primary_language,
        transcript_fallback_language=settings.transcript_fallback_language,
        transcript_type_preference=settings.transcript_type_preference,
        transcript_max_segments=settings.transcript_max_segments,
        lease_seconds=settings.worker_lease_seconds,
    )
    runner = JobRunner(repository, collector)
    logger.info("Monitube worker is polling queued collection jobs.")
    while not shutdown_requested.is_set():
        indexed = process_nlp_batch()
        # Pins are canonical-target subscriptions.  Dispatching here keeps the
        # scheduler inside the existing single worker/lease model and never creates
        # a second active job for the same channel.
        dispatched = repository.dispatch_due_pins(runtime_config_id=runtime_config_id)
        if dispatched:
            logger.info("Dispatched %s due pinned collection target(s)", dispatched)
        job = repository.claim_next_job(
            worker_id=worker_id, lease_seconds=settings.worker_lease_seconds
        )
        if not job:
            if not indexed:
                shutdown_requested.wait(timeout=settings.worker_poll_seconds)
            continue
        if settings.youtube_api_key_encryption_key and hasattr(
            repository, "load_runtime_keys"
        ):
            registered_keys = repository.load_runtime_keys(
                runtime_config_id=runtime_config_id,
                encryption_key=settings.youtube_api_key_encryption_key,
            )
            if registered_keys:
                collector.client.replace_keys(registered_keys)
        completed = runner.run(job.id)
        logger.info("Collection job %s entered %s", completed.id, completed.state.value)
    close = getattr(repository, "close", None)
    if callable(close):
        close()
    logger.info("Monitube worker stopped.")


if __name__ == "__main__":
    main()
