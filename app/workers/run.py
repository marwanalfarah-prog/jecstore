"""Worker entry point.

Run the recurring jobs::

    python -m app.workers.run              # scheduler, runs until interrupted
    python -m app.workers.run --once       # run every job once and exit
    python -m app.workers.run --job drain_outbox

``--once`` is what a cron entry or a container health check would call; the
scheduler form is for a long-lived worker process.

Jobs never run inside the web process. A slow SMTP server or a long inventory
sweep must not sit in a request (Part II §5).
"""

from __future__ import annotations

import argparse
import signal
import sys
import time

from app.core.logging import configure_logging, get_logger
from app.workers.jobs import JOBS

log = get_logger(__name__)


def run_once(names: list[str] | None = None) -> dict[str, object]:
    """Run each named job exactly once. Failures are isolated per job."""
    selected = names or list(JOBS)
    results: dict[str, object] = {}

    for name in selected:
        entry = JOBS.get(name)
        if entry is None:
            log.error("unknown_job", extra={"job": name})
            continue
        func, _ = entry
        try:
            results[name] = func()
        except Exception:  # noqa: BLE001 - one bad job must not stop the rest
            log.exception("job_failed", extra={"job": name})
            results[name] = "failed"

    return results


def run_scheduler() -> None:
    """Run jobs on their intervals until interrupted.

    A plain loop rather than APScheduler: the schedule is four fixed intervals,
    and a dependency-free loop is easier to reason about and to containerise.
    Swap in APScheduler or Celery when the schedule needs cron expressions or
    the volume needs a broker (Part II §7.4).
    """
    running = True

    def stop(signum, frame):  # noqa: ARG001
        nonlocal running
        log.info("worker_stopping", extra={"signal": signum})
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    next_run = {name: 0.0 for name in JOBS}
    log.info("worker_started", extra={"jobs": ",".join(JOBS)})

    while running:
        now = time.monotonic()
        for name, (func, interval) in JOBS.items():
            if now < next_run[name]:
                continue
            try:
                func()
            except Exception:  # noqa: BLE001 - keep the worker alive
                log.exception("job_failed", extra={"job": name})
            next_run[name] = time.monotonic() + interval

        # Short sleep so shutdown stays responsive.
        for _ in range(10):
            if not running:
                break
            time.sleep(0.5)

    log.info("worker_stopped")


def main() -> int:
    configure_logging()

    parser = argparse.ArgumentParser(description="JEC Store background worker.")
    parser.add_argument("--once", action="store_true", help="Run each job once and exit.")
    parser.add_argument(
        "--job", action="append", choices=sorted(JOBS), help="Run only this job (repeatable)."
    )
    args = parser.parse_args()

    if args.once or args.job:
        results = run_once(args.job)
        for name, outcome in results.items():
            log.info("job_result", extra={"job": name, "outcome": outcome})
        return 0

    run_scheduler()
    return 0


if __name__ == "__main__":
    sys.exit(main())
