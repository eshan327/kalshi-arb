"""Read-only snapshot for the dashboard."""

from core.markets import (
    extract_settlement_decimals,
    extract_suggested_strike,
    get_active_market_profile,
)
from data.benchmark import get_index_state
from data.streamer import get_live_market_info, get_live_orderbook_snapshot
from pricing.live_pricing import compute_live_pricing_snapshot
from trading.runtime import get_trading_runtime_snapshot
from trading.settings import get_trading_settings_snapshot


def build_dashboard_state_payload(*, depth: int) -> dict:
    profile = get_active_market_profile()
    book = get_live_orderbook_snapshot(depth=depth)
    market = get_live_market_info()
    runtime = get_trading_runtime_snapshot()
    strike = extract_suggested_strike(market)
    return {
        "orderbook": book,
        "index": get_index_state(),
        "market_info": market,
        "asset": profile.asset,
        "asset_display": profile.display_name,
        "index_label": profile.index_label,
        "suggested_strike": strike,
        "pricing": compute_live_pricing_snapshot(
            strike=strike,
            market_ticker=market.get("ticker"),
            close_time_iso=market.get("close_time"),
            settlement_decimals=extract_settlement_decimals(
                market, profile.settlement_decimals_fallback
            ),
        ),
        "trading_settings": get_trading_settings_snapshot(),
        "trading_runtime": runtime,
        "account": runtime.get("account", {}),
        "signal_monologue": runtime.get("signal_monologue", {})
        if book["initialized"]
        else {},
    }
