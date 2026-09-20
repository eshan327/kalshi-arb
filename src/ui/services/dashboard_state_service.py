from __future__ import annotations

from typing import Any

from core.asset_context import get_active_market_profile
from core.market_metadata import extract_settlement_decimals, extract_suggested_strike
from engine.live_pricing import compute_live_pricing_snapshot
from engine.streamer import get_live_market_info, get_live_orderbook_snapshot
from engine.trading.runtime import get_trading_runtime_snapshot
from engine.trading.settings import (
    get_trading_settings_model,
    get_trading_settings_snapshot,
)
from engine.trading.strategy import apply_pricing_overrides
from feeds.state.tick_store import get_index_state


def build_dashboard_state_payload(*, depth: int) -> dict[str, Any]:
    profile = get_active_market_profile()

    snapshot = get_live_orderbook_snapshot(depth=depth)
    index = get_index_state()
    active_asset = profile.asset
    feed_asset = str(index.get("asset") or active_asset)
    asset_syncing = feed_asset != active_asset

    market_info = get_live_market_info()
    settlement_decimals = extract_settlement_decimals(
        market_info, profile.settlement_decimals_fallback
    )
    suggested_strike = extract_suggested_strike(market_info)
    close_iso = (
        market_info.get("close_time")
        if isinstance(market_info.get("close_time"), str)
        else None
    )
    market_ticker = (
        market_info.get("ticker")
        if isinstance(market_info.get("ticker"), str)
        else None
    )

    settings_model = get_trading_settings_model()
    pricing = apply_pricing_overrides(
        compute_live_pricing_snapshot(
            strike=suggested_strike,
            market_ticker=market_ticker,
            close_time_iso=close_iso,
            settlement_decimals=settlement_decimals,
        ),
        settings_model,
    )
    trading_runtime = get_trading_runtime_snapshot()
    trading_settings = get_trading_settings_snapshot()
    account = (
        trading_runtime.get("account")
        if isinstance(trading_runtime.get("account"), dict)
        else {}
    )
    signal_monologue = (
        trading_runtime.get("signal_monologue")
        if snapshot.get("initialized")
        and isinstance(trading_runtime.get("signal_monologue"), dict)
        else {}
    )

    payload = {
        "orderbook": snapshot,
        "index": index,
        "market_info": market_info,
        "asset": profile.asset,
        "asset_display": profile.display_name,
        "feed_asset": feed_asset,
        "asset_syncing": asset_syncing,
        "index_label": profile.index_label,
        "active_series": profile.kalshi_series_ticker,
        "settlement_benchmark_label": profile.settlement_benchmark_label,
        "settlement_rule_text": profile.settlement_rule_text,
        "settlement_window_seconds": profile.settlement_window_seconds,
        "suggested_strike": suggested_strike,
        "pricing": pricing,
        "trading_settings": trading_settings,
        "trading_runtime": trading_runtime,
        "account": account,
        "signal_monologue": signal_monologue,
    }
    return payload
