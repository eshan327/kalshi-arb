import asyncio
import logging

from core.asset_context import get_active_market_profile
from feeds.context import FeedsRuntimeContext

logger = logging.getLogger(__name__)


async def _cancel_tasks(tasks: list[asyncio.Task]) -> None:
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def run_brti_aggregator(recalc_interval: float = 1.0) -> None:
    """Runs exchange feeds and the index calculator for the process asset."""
    runtime_context = FeedsRuntimeContext(get_active_market_profile())
    tasks = runtime_context.spawn_tasks(recalc_interval)

    logger.info(
        "Index proxy aggregator started for %s (%s).",
        runtime_context.profile.display_name,
        runtime_context.profile.asset,
    )

    try:
        await asyncio.gather(*tasks)
    finally:
        await _cancel_tasks(tasks)
