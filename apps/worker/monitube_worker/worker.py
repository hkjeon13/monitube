"""Polling worker for server-managed YouTube collection jobs."""

from __future__ import annotations

import logging
import os
import signal
from threading import Event

from monitube_api.settings import Settings, create_repository

from .collector import YouTubeCollector
from .runner import JobRunner
from .youtube_data import RotatingYouTubeDataClient


shutdown_requested = Event()


def _request_shutdown(_: int, __: object) -> None:
    shutdown_requested.set()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logger = logging.getLogger(__name__)
    signal.signal(signal.SIGINT, _request_shutdown)
    signal.signal(signal.SIGTERM, _request_shutdown)
    settings = Settings.from_environment()
    repository, runtime_config_id = create_repository(settings)
    if not settings.youtube_api_key:
        logger.warning("YOUTUBE_API_KEY is not configured; worker will not claim collection jobs.")
        while not shutdown_requested.wait(timeout=settings.worker_poll_seconds):
            logger.debug("Worker is awaiting the server-managed YouTube credential.")
        logger.info("Monitube worker stopped.")
        return

    worker_id = os.getenv("WORKER_ID", f"worker-{os.getpid()}")
    collector = YouTubeCollector(
        repository,
        RotatingYouTubeDataClient(
            settings.youtube_api_keys,
            base_url=settings.youtube_api_base_url,
            timeout_seconds=settings.youtube_api_timeout_seconds,
        ),
        lease_seconds=settings.worker_lease_seconds,
    )
    runner = JobRunner(repository, collector)
    logger.info("Monitube worker is polling queued collection jobs.")
    while not shutdown_requested.is_set():
        # Pins are canonical-target subscriptions.  Dispatching here keeps the
        # scheduler inside the existing single worker/lease model and never creates
        # a second active job for the same channel.
        dispatched = repository.dispatch_due_pins(runtime_config_id=runtime_config_id)
        if dispatched:
            logger.info("Dispatched %s due pinned collection target(s)", dispatched)
        job = repository.claim_next_job(worker_id=worker_id, lease_seconds=settings.worker_lease_seconds)
        if not job:
            shutdown_requested.wait(timeout=settings.worker_poll_seconds)
            continue
        if settings.youtube_api_key_encryption_key and hasattr(repository, "load_runtime_keys"):
            registered_keys = repository.load_runtime_keys(
                runtime_config_id=runtime_config_id,
                encryption_key=settings.youtube_api_key_encryption_key,
            )
            if registered_keys:
                collector.client.replace_keys(registered_keys)
        completed = runner.run(job.id)
        logger.info("Collection job %s entered %s", completed.id, completed.state.value)
    logger.info("Monitube worker stopped.")


if __name__ == "__main__":
    main()
