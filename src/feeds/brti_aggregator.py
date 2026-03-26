import asyncio
import logging
import time
from feeds.brti_calc import calculate_brti
from feeds.brti_state import (
    get_brti_settlement_proxy,
    get_brti_state,
    get_brti_ticks,
    get_brti_ws_log,
    get_brti_ws_stats,
    get_exchange_books_ref,
    record_brti_tick,
    set_brti_state,
)
from feeds.exchanges import (
    stream_bitstamp,
    stream_coinbase,
    stream_gemini,
    stream_kraken,
    stream_paxos,
)

logger = logging.getLogger(__name__)


async def _recalculate_loop(recalc_interval: float = 1.0) -> None:
    """Recalculates BRTI from the consolidated live exchange books on a fixed cadence."""
    await asyncio.sleep(3)
    logger.info("BRTI recalculation loop started (%.2fs interval)", recalc_interval)

    while True:
        now = time.time()
        exchange_books = get_exchange_books_ref()
        brti, depth, num_exchanges = calculate_brti(exchange_books, now)

        if brti is not None:
            set_brti_state(brti=brti, depth=depth, exchanges=num_exchanges, timestamp=now)
            book_sizes = {
                name: len(book["bids"]) + len(book["asks"])
                for name, book in exchange_books.items()
            }
            record_brti_tick(brti, depth, num_exchanges, book_sizes, "ok")
        else:
            record_brti_tick(None, 0, 0, {}, "calc_failed")

        await asyncio.sleep(recalc_interval)


async def run_brti_aggregator(recalc_interval: float = 1.0) -> None:
    """Starts all exchange feeds and the BRTI calculation task in the current event loop."""
    tasks = [
        asyncio.create_task(stream_coinbase()),
        asyncio.create_task(stream_kraken()),
        asyncio.create_task(stream_gemini()),
        asyncio.create_task(stream_bitstamp()),
        asyncio.create_task(stream_paxos()),
        asyncio.create_task(_recalculate_loop(recalc_interval=recalc_interval)),
    ]

    logger.info("BRTI aggregator started (Coinbase, Kraken, Gemini, Bitstamp, Paxos)")

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


__all__ = [
    "get_brti_state",
    "get_brti_ticks",
    "get_brti_settlement_proxy",
    "get_brti_ws_log",
    "get_brti_ws_stats",
    "run_brti_aggregator",
]
