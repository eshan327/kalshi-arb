import asyncio
import logging
import math
import time

from core.asset_context import get_active_market_profile
from core.market_profiles import MarketProfile
from feeds.brti_calc import calculate_brti, reset_brti_calc_state
from feeds.exchanges.bitstamp import BitstampAdapter
from feeds.exchanges.coinbase import CoinbaseAdapter
from feeds.exchanges.gemini import GeminiAdapter
from feeds.exchanges.kraken import KrakenAdapter
from feeds.state.book_store import get_exchange_books_ref, reset_exchange_books
from feeds.state.tick_store import record_brti_tick, reset_tick_state, set_brti_state

logger = logging.getLogger(__name__)


async def _recalculate_loop(profile: MarketProfile) -> None:
    await asyncio.sleep(3)
    logger.info("Index recalculation loop started (1.00s interval)")

    while True:
        now = time.time()
        next_slot = math.floor(now) + 1
        await asyncio.sleep(max(0.0, next_slot - now))
        now = time.time()
        exchange_books = get_exchange_books_ref()
        brti, depth, num_exchanges = calculate_brti(
            exchange_books,
            now,
            deviation_threshold=profile.index_deviation_threshold,
            potentially_erroneous_param=profile.index_erroneous_threshold,
            stale_threshold=profile.index_stale_threshold_sec,
            price_decimals=profile.index_tick_decimals,
        )

        if brti is None:
            record_brti_tick(None, 0, 0, {}, "calc_failed", timestamp=next_slot)
            continue

        set_brti_state(brti=brti, depth=depth, exchanges=num_exchanges, timestamp=now)
        book_sizes = {
            name: len(book["bids"]) + len(book["asks"])
            for name, book in exchange_books.items()
        }
        record_brti_tick(
            brti, depth, num_exchanges, book_sizes, "ok", timestamp=next_slot
        )


async def run_brti_aggregator() -> None:
    """Run exchange feeds and the synthetic index for the process asset."""
    profile = get_active_market_profile()
    reset_brti_calc_state()
    reset_exchange_books()
    reset_tick_state(profile.asset)
    adapters = [
        adapter(profile)
        for source, adapter in (
            ("coinbase", CoinbaseAdapter),
            ("kraken", KrakenAdapter),
            ("gemini", GeminiAdapter),
            ("bitstamp", BitstampAdapter),
        )
        if source in profile.exchange_sources
    ]

    logger.info(
        "Index proxy aggregator started for %s (%s).",
        profile.display_name,
        profile.asset,
    )
    async with asyncio.TaskGroup() as tasks:
        for adapter in adapters:
            tasks.create_task(adapter.stream())
        tasks.create_task(_recalculate_loop(profile))
