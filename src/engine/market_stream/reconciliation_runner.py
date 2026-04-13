from __future__ import annotations

import asyncio
import time

from data.kalshi_rest import get_market_orderbook
from engine.market_stream.bootstrap import levels_from_rest_snapshot
from engine.orderbook import OrderBook
from engine.reconciliation import compare_levels, is_reconciliation_breach
from engine.stream_metrics import _record_reconciliation, _record_ws_event


async def run_recalibration(
    *,
    market_ticker: str,
    book: OrderBook,
    consecutive_recon_breaches: int,
    recon_top_n: int,
    recon_consecutive_breaches: int,
) -> tuple[int, str]:
    """Returns (updated_consecutive_breaches, action)."""
    recal_snapshot = await asyncio.to_thread(get_market_orderbook, market_ticker)
    rest_yes_bids, rest_no_bids = levels_from_rest_snapshot(book, recal_snapshot)
    live_yes_bids, _, live_no_bids, _ = book.get_orderbook()

    metrics = {
        "yes": compare_levels(live_yes_bids, rest_yes_bids, recon_top_n),
        "no": compare_levels(live_no_bids, rest_no_bids, recon_top_n),
    }

    breach = is_reconciliation_breach(metrics)
    if breach:
        consecutive_recon_breaches += 1
    else:
        consecutive_recon_breaches = 0

    action = "none"
    if consecutive_recon_breaches >= recon_consecutive_breaches:
        action = "trigger_resync"
        book.needs_resync = True

    _record_reconciliation(
        {
            "ts": time.time(),
            "market_ticker": market_ticker,
            "breach": breach,
            "consecutive_breaches": consecutive_recon_breaches,
            "action": action,
            "metrics": metrics,
        }
    )

    _record_ws_event(
        "recalibration",
        recal_snapshot.get("seq", recal_snapshot.get("sequence")),
        {
            "breach": breach,
            "consecutive_breaches": consecutive_recon_breaches,
            "action": action,
        },
        "rest_snapshot_reconciled",
    )

    return consecutive_recon_breaches, action
