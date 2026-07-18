"""Lease-based worker for bounded, deterministic derived summaries."""

from __future__ import annotations

import logging
import os
import signal
from threading import Event

from monitube_api.settings import Settings, create_repository


shutdown_requested = Event()


def _request_shutdown(_: int, __: object) -> None:
    shutdown_requested.set()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger = logging.getLogger(__name__)
    signal.signal(signal.SIGINT, _request_shutdown)
    signal.signal(signal.SIGTERM, _request_shutdown)
    settings = Settings.from_environment()
    repository, _ = create_repository(settings)
    worker_id = os.getenv(
        "ANALYSIS_WORKER_ID",
        f"analysis-{os.getenv('HOSTNAME', 'local')}-{os.getpid()}",
    )
    try:
        if not settings.enable_analysis_worker:
            logger.info("Analysis worker is disabled; remaining healthy and idle.")
            while not shutdown_requested.wait(timeout=max(5.0, settings.worker_poll_seconds)):
                pass
            return
        required = (
            "enqueue_missing_analysis_runs",
            "claim_next_analysis_run",
            "complete_analysis_run",
            "fail_analysis_run",
        )
        if any(not hasattr(repository, method) for method in required):
            logger.warning("Configured repository does not support analysis leases; worker is idle.")
            while not shutdown_requested.wait(timeout=max(5.0, settings.worker_poll_seconds)):
                pass
            return

        logger.info("Analysis worker is polling deterministic summary runs.")
        seed_counter = 0
        while not shutdown_requested.is_set():
            # Seed a bounded number at startup and periodically. Parent-terminal
            # transactions normally enqueue new versions immediately.
            if seed_counter % 20 == 0:
                seeded = repository.enqueue_missing_analysis_runs(limit=100)
                if seeded:
                    logger.info("Queued %s missing analysis run(s)", seeded)
            seed_counter += 1
            run = repository.claim_next_analysis_run(
                worker_id=worker_id,
                lease_seconds=max(300, settings.worker_lease_seconds * 5),
            )
            if not run:
                shutdown_requested.wait(timeout=settings.worker_poll_seconds)
                continue
            run_id = str(run["id"])
            try:
                summary = repository.complete_analysis_run(
                    run_id,
                    worker_id=worker_id,
                    max_comments=50_000,
                    max_per_video=1_000,
                )
                logger.info(
                    "Analysis run %s completed (%s videos, %s comments)",
                    run_id,
                    summary.get("videoCount", 0),
                    summary.get("commentCount", 0),
                )
            except Exception as exc:  # retry state is durable; logs omit comment text
                state = repository.fail_analysis_run(
                    run_id,
                    worker_id=worker_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
                logger.exception("Analysis run %s entered %s", run_id, state)
    finally:
        close = getattr(repository, "close", None)
        if callable(close):
            close()
        logger.info("Monitube analysis worker stopped.")


if __name__ == "__main__":
    main()
