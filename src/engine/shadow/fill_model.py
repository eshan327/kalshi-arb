from __future__ import annotations

import time

from engine.orderbook import OrderBook
from engine.shadow.models import PaperFillQuote


def simulate_taker_fill(
    *,
    book: OrderBook | None,
    side: str,
    slippage_ticks: int,
    now_ts: float | None = None,
) -> PaperFillQuote:
    ts = time.time() if now_ts is None else float(now_ts)
    normalized_side = "yes" if str(side).strip().lower() == "yes" else "no"

    if book is None or not book.initialized:
        return PaperFillQuote(
            ts=ts,
            can_fill=False,
            reason="no_live_orderbook",
            side=normalized_side,
            best_bid_cents=None,
            best_ask_cents=None,
            spread_cents=None,
            fill_price_cents=None,
            slippage_cents=0.0,
        )

    yes_bid, yes_ask, no_bid, no_ask = book.get_best_prices()
    if normalized_side == "yes":
        best_bid = int(round(float(yes_bid))) if isinstance(yes_bid, (int, float)) else None
        best_ask = int(round(float(yes_ask))) if isinstance(yes_ask, (int, float)) else None
    else:
        best_bid = int(round(float(no_bid))) if isinstance(no_bid, (int, float)) else None
        best_ask = int(round(float(no_ask))) if isinstance(no_ask, (int, float)) else None

    if best_bid is None or best_ask is None:
        return PaperFillQuote(
            ts=ts,
            can_fill=False,
            reason="missing_top_of_book",
            side=normalized_side,
            best_bid_cents=best_bid,
            best_ask_cents=best_ask,
            spread_cents=None,
            fill_price_cents=None,
            slippage_cents=0.0,
        )

    ticks = max(0, int(slippage_ticks))
    spread = max(0.0, float(best_ask - best_bid))
    fill_price = max(1, min(99, best_ask + ticks))
    slippage = max(0.0, float(fill_price - best_ask))

    return PaperFillQuote(
        ts=ts,
        can_fill=True,
        reason="crossed_spread",
        side=normalized_side,
        best_bid_cents=best_bid,
        best_ask_cents=best_ask,
        spread_cents=spread,
        fill_price_cents=fill_price,
        slippage_cents=slippage,
    )
