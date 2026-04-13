from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from core.market_profiles import MarketProfile, get_market_profile
from feeds.calc.rti_pipeline import RTIPipeline
from feeds.state.book_store import get_exchange_books_ref
from feeds.state.runtime_state import reset_brti_runtime_state
from feeds.state.tick_store import record_brti_tick, set_brti_state
from feeds.exchanges import (
    BitstampAdapter,
    CoinbaseAdapter,
    GeminiAdapter,
    KrakenAdapter,
    PaxosAdapter,
)

logger = logging.getLogger(__name__)


@dataclass
class FeedsRuntimeContext:
    profile: MarketProfile
    calculator: RTIPipeline

    @staticmethod
    def create(asset: str) -> "FeedsRuntimeContext":
        profile = get_market_profile(asset)
        return FeedsRuntimeContext(
            profile=profile,
            calculator=RTIPipeline(profile=profile),
        )

    def reset_state(self) -> None:
        self.calculator.reset()
        reset_brti_runtime_state(self.profile.asset)

    async def recalculate_loop(self, recalc_interval: float = 1.0) -> None:
        """Recalculates synthetic index from live exchange books at a fixed cadence."""
        await asyncio.sleep(3)
        logger.info("Index recalculation loop started (%.2fs interval)", recalc_interval)

        while True:
            now = time.time()
            exchange_books = get_exchange_books_ref()
            brti, depth, num_exchanges = self.calculator.calculate(exchange_books, now)

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

    def spawn_tasks(self, recalc_interval: float) -> list[asyncio.Task]:
        self.reset_state()
        profile = self.profile
        adapters = (
            CoinbaseAdapter(profile),
            KrakenAdapter(profile),
            GeminiAdapter(profile),
            BitstampAdapter(profile),
            PaxosAdapter(profile),
        )
        tasks = [asyncio.create_task(adapter.stream()) for adapter in adapters]
        tasks.append(asyncio.create_task(self.recalculate_loop(recalc_interval=recalc_interval)))
        return tasks
