"""Live index + vol + Asian pricer snapshot for the operator dashboard."""

from __future__ import annotations

import time
from functools import lru_cache
from typing import Any

from core.asset_context import get_active_market_profile
from core.market_profiles import MarketProfile
from engine.pricing.pipeline import compute_pricing_snapshot
from feeds.state.tick_store import get_brti_state, get_brti_tick_version, get_brti_ticks

_BRTI_TICK_LOOKBACK = 4000


@lru_cache(maxsize=1)
def _compute_cached_pricing_snapshot(
    profile: MarketProfile,
    feed_asset: str,
    spot: float | None,
    strike: float | None,
    market_ticker: str | None,
    close_time_iso: str | None,
    settlement_decimals: int,
    source_exchanges: int,
    tick_version: int,
    second_bucket: int,
) -> dict[str, Any]:
    del tick_version, second_bucket
    return compute_pricing_snapshot(
        profile=profile,
        feed_asset=feed_asset,
        spot=spot,
        ticks=get_brti_ticks(limit=_BRTI_TICK_LOOKBACK),
        strike=strike,
        market_ticker=market_ticker,
        close_time_iso=close_time_iso,
        settlement_decimals=settlement_decimals,
        source_exchanges=source_exchanges,
    )


def reset_live_pricing_for_new_market() -> None:
    """Clears cached snapshot state when market streamer rotates contracts."""
    _compute_cached_pricing_snapshot.cache_clear()


def compute_live_pricing_snapshot(
    *,
    strike: float | None,
    market_ticker: str | None,
    close_time_iso: str | None,
    settlement_decimals: int | None = None,
) -> dict[str, Any]:
    profile = get_active_market_profile()

    brti_state = get_brti_state()
    spot = brti_state.get("brti")
    feed_asset_raw = brti_state.get("asset")
    feed_asset = (
        feed_asset_raw.upper().strip()
        if isinstance(feed_asset_raw, str)
        else profile.asset
    )

    spot_key = float(spot) if isinstance(spot, (int, float)) else None
    strike_key = float(strike) if isinstance(strike, (int, float)) else None
    exchanges_raw = brti_state.get("exchanges")
    source_exchanges = (
        int(exchanges_raw) if isinstance(exchanges_raw, (int, float)) else 0
    )
    decimals = (
        profile.settlement_decimals_fallback
        if settlement_decimals is None
        else max(0, min(12, int(settlement_decimals)))
    )
    snapshot = _compute_cached_pricing_snapshot(
        profile,
        feed_asset=feed_asset,
        spot=spot_key,
        strike=strike_key,
        market_ticker=market_ticker,
        close_time_iso=close_time_iso,
        settlement_decimals=decimals,
        source_exchanges=source_exchanges,
        tick_version=get_brti_tick_version(),
        second_bucket=int(time.time()),
    )
    return dict(snapshot)
