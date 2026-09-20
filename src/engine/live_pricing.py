"""Shared pricing snapshot for trading and the dashboard."""

from __future__ import annotations

import time
from functools import lru_cache

from core.asset_context import get_active_market_profile
from engine.pricing.pipeline import compute_pricing_snapshot
from engine.trading.settings import get_trading_settings_model
from feeds.state.tick_store import (
    get_index_state,
    get_index_tick_version,
    get_index_ticks,
)


@lru_cache(maxsize=1)
def _compute_cached_pricing_snapshot(
    profile,
    strike,
    ticker,
    close,
    decimals,
    version,
    clock_bucket,
    vol_window_seconds,
    pre_settlement_vol_window_seconds,
    pre_settlement_until_seconds,
):
    state = get_index_state()
    return compute_pricing_snapshot(
        profile=profile,
        feed_asset=state.get("asset", ""),
        spot=state.get("price"),
        ticks=get_index_ticks(),
        strike=strike,
        market_ticker=ticker,
        close_time_iso=close,
        settlement_decimals=decimals,
        index_state=state,
        vol_window_seconds=vol_window_seconds,
        pre_settlement_vol_window_seconds=pre_settlement_vol_window_seconds,
        pre_settlement_until_seconds=pre_settlement_until_seconds,
    )


def reset_live_pricing_for_new_market() -> None:
    _compute_cached_pricing_snapshot.cache_clear()


def compute_live_pricing_snapshot(
    *, strike, market_ticker, close_time_iso, settlement_decimals=None
):
    settings = get_trading_settings_model()
    return dict(
        _compute_cached_pricing_snapshot(
            get_active_market_profile(),
            strike,
            market_ticker,
            close_time_iso,
            settlement_decimals,
            get_index_tick_version(),
            int(time.time() * 5),
            settings.volatility_window_seconds,
            settings.pre_settlement_volatility_window_seconds,
            settings.pre_settlement_until_seconds,
        )
    )
