"""Cleanup worker CLI entrypoint."""
import asyncio
import logging

from app.core.config import settings
from app.core.logging import setup_logging

logger = logging.getLogger(__name__)


async def run_cleanup_cycle() -> None:
    """Execute one pass of expired object and row cleanup."""
    logger.info("Cleanup cycle executed (standing by for share lifecycle implementation).")


async def main() -> None:
    setup_logging(settings.LOG_LEVEL)
    logger.info("Starting Sendrift cleanup worker...")
    await run_cleanup_cycle()


if __name__ == "__main__":
    asyncio.run(main())
