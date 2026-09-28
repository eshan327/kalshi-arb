"""Read-only pricing snapshot from the live CF feed."""

from __future__ import annotations

from core.markets import get_active_market_profile
from data.benchmark import get_index_state, get_index_ticks
from pricing.baseline import compute_pricing_snapshot


def compute_live_pricing_snapshot(
    *, strike, market_ticker, close_time_iso, settlement_decimals=None, profile=None
):
    state = get_index_state()
    return compute_pricing_snapshot(
        profile=profile or get_active_market_profile(),
        feed_asset=state.get("asset", ""),
        spot=state.get("price"),
        ticks=get_index_ticks(),
        strike=strike,
        market_ticker=market_ticker,
        close_time_iso=close_time_iso,
        settlement_decimals=settlement_decimals,
        index_state=state,
    )
