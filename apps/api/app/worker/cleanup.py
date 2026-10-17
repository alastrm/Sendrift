"""Cleanup worker CLI entrypoint."""
import argparse
import asyncio
import logging
import signal

from app.core.config import settings
from app.core.logging import setup_logging
from app.services.cleanup_service import CleanupResult, CleanupService

logger = logging.getLogger(__name__)


async def run_cleanup_cycle(reconcile_orphans: bool = False) -> CleanupResult:
    """Execute one pass of expired object and row cleanup."""
    service = CleanupService()
    return await service.run_cleanup_cycle(reconcile_orphans=reconcile_orphans)


async def run_worker_loop(
    interval_seconds: int = settings.CLEANUP_INTERVAL_SECONDS,
    stop_event: asyncio.Event | None = None,
    reconcile_orphans: bool = False,
) -> None:
    """Run cleanup continuously in a loop until stop_event is signaled."""
    if stop_event is None:
        stop_event = asyncio.Event()

    logger.info(
        "Sendrift cleanup worker loop started (interval: %ds, reconcile_orphans: %s)",
        interval_seconds,
        reconcile_orphans,
    )

    cycle_count = 0
    while not stop_event.is_set():
        cycle_count += 1
        try:
            # Run orphan reconciliation periodically (every 10 cycles) if enabled
            do_reconcile = reconcile_orphans and (cycle_count % 10 == 0)
            await run_cleanup_cycle(reconcile_orphans=do_reconcile)
        except Exception as exc:
            logger.error("Error during cleanup cycle: %s", type(exc).__name__)

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except (asyncio.TimeoutError, TimeoutError):
            pass

    logger.info("Sendrift cleanup worker shutting down gracefully.")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Sendrift File Lifecycle & Cleanup Worker")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single cleanup cycle and exit",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=settings.CLEANUP_INTERVAL_SECONDS,
        help="Interval in seconds between cleanup cycles (default: 60)",
    )
    parser.add_argument(
        "--reconcile",
        action="store_true",
        help="Audit and delete orphan S3 objects missing database records",
    )
    args = parser.parse_args()

    setup_logging(settings.LOG_LEVEL)

    if args.once:
        logger.info("Executing single cleanup cycle...")
        res = await run_cleanup_cycle(reconcile_orphans=args.reconcile)
        logger.info("Single cleanup pass completed: %s", res)
        return

    stop_event = asyncio.Event()

    def _signal_handler(*_: object) -> None:
        logger.info("Received termination signal; stopping worker loop...")
        stop_event.set()

    try:
        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)
    except (ValueError, AttributeError):
        pass

    await run_worker_loop(
        interval_seconds=args.interval,
        stop_event=stop_event,
        reconcile_orphans=args.reconcile,
    )


if __name__ == "__main__":
    asyncio.run(main())
