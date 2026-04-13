import asyncio
import logging
from core.asset_context import get_active_asset_context
from feeds.context import FeedsRuntimeContext
from feeds.state.diagnostics_store import get_brti_ws_log, get_brti_ws_stats
from feeds.state.tick_store import get_brti_settlement_proxy, get_brti_state, get_brti_ticks

logger = logging.getLogger(__name__)


async def _cancel_tasks(tasks: list[asyncio.Task]) -> None:
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def _raise_if_task_failed(tasks: list[asyncio.Task]) -> None:
    for task in tasks:
        if not task.done():
            continue
        if task.cancelled():
            continue

        exc = task.exception()
        if exc is not None:
            raise exc

        raise RuntimeError("Exchange feed task stopped unexpectedly.")


async def run_brti_aggregator(recalc_interval: float = 1.0) -> None:
    """Runs exchange feeds + index calculator for the active asset, rotating on selector changes."""
    asset_context = get_active_asset_context()
    active_asset = asset_context.profile.asset
    runtime_context = FeedsRuntimeContext.create(active_asset)
    tasks = runtime_context.spawn_tasks(recalc_interval)

    logger.info(
        "Index proxy aggregator started for %s (%s).",
        runtime_context.profile.display_name,
        runtime_context.profile.asset,
    )

    try:
        while True:
            await asyncio.sleep(1)
            _raise_if_task_failed(tasks)

            latest_asset = get_active_asset_context().profile.asset
            if latest_asset == active_asset:
                continue

            logger.info("Switching index proxy feeds from %s to %s...", active_asset, latest_asset)
            await _cancel_tasks(tasks)

            active_asset = latest_asset
            runtime_context = FeedsRuntimeContext.create(active_asset)
            tasks = runtime_context.spawn_tasks(recalc_interval)
            logger.info(
                "Index proxy aggregator resumed for %s (%s).",
                runtime_context.profile.display_name,
                runtime_context.profile.asset,
            )
    except asyncio.CancelledError:
        await _cancel_tasks(tasks)
        raise
    except Exception:
        await _cancel_tasks(tasks)
        raise


__all__ = [
    "get_brti_state",
    "get_brti_ticks",
    "get_brti_settlement_proxy",
    "get_brti_ws_log",
    "get_brti_ws_stats",
    "run_brti_aggregator",
]
