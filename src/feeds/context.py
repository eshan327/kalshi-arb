from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from core.market_profiles import MarketProfile
from feeds.brti_calc import calculate_brti, reset_brti_calc_state
from feeds.exchanges.bitstamp import BitstampAdapter
from feeds.exchanges.coinbase import CoinbaseAdapter
from feeds.exchanges.gemini import GeminiAdapter
from feeds.exchanges.kraken import KrakenAdapter
from feeds.exchanges.paxos import PaxosAdapter
from feeds.state.book_store import get_exchange_books_ref
from feeds.state.runtime_state import reset_brti_runtime_state
from feeds.state.tick_store import record_brti_tick, set_brti_state

logger = logging.getLogger(__name__)


@dataclass
class FeedsRuntimeContext:
    profile: MarketProfile

    def reset_state(self) -> None:
        reset_brti_calc_state()
        reset_brti_runtime_state(self.profile.asset)

    async def recalculate_loop(self, recalc_interval: float = 1.0) -> None:
        """Recalculates synthetic index from live exchange books at a fixed cadence."""
        await asyncio.sleep(3)
        logger.info(
            "Index recalculation loop started (%.2fs interval)", recalc_interval
        )

        while True:
            now = time.time()
            exchange_books = get_exchange_books_ref()
            brti, depth, num_exchanges = calculate_brti(
                exchange_books,
                now,
                spacing=self.profile.index_spacing_units,
                deviation_threshold=self.profile.index_deviation_threshold,
                potentially_erroneous_param=self.profile.index_erroneous_threshold,
                stale_threshold=self.profile.index_stale_threshold_sec,
                price_decimals=self.profile.index_price_decimals,
            )

            if brti is not None:
                set_brti_state(
                    brti=brti, depth=depth, exchanges=num_exchanges, timestamp=now
                )
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
        adapters = [
            CoinbaseAdapter(profile),
            KrakenAdapter(profile),
            BitstampAdapter(profile),
        ]
        if profile.gemini_symbol:
            adapters.append(GeminiAdapter(profile))
        if profile.paxos_symbol:
            adapters.append(PaxosAdapter(profile))
        tasks = [asyncio.create_task(adapter.stream()) for adapter in adapters]
        tasks.append(
            asyncio.create_task(self.recalculate_loop(recalc_interval=recalc_interval))
        )
        return tasks
