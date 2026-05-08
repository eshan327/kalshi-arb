from __future__ import annotations

from typing import Any

from core.asset_context import get_active_asset_context
from core.market_selection import get_market_selection_state, request_asset_switch
from engine.book_microstructure import get_last_p_book_snapshot
from engine.live_pricing import compute_live_pricing_snapshot
from engine.streamer import (
    get_live_market_info,
    get_live_orderbook_snapshot,
    get_ws_message_log_size,
    get_ws_processing_stats,
)
from feeds.brti_aggregator import get_brti_settlement_proxy, get_brti_state, get_brti_ws_stats
from ui.contracts import (
    MARKET_SELECTION_PAYLOAD_KEYS,
    MARKET_SELECTION_POST_RESPONSE_KEYS,
    STATE_PAYLOAD_KEYS,
    enforce_payload_contract,
)
from ui.market_metadata import extract_suggested_strike


def clamped_limit(raw_limit: int | None, default: int, max_limit: int) -> int:
    if raw_limit is None:
        return default
    return max(1, min(raw_limit, max_limit))


def build_dashboard_state_payload(*, depth: int) -> dict[str, Any]:
    asset_context = get_active_asset_context()
    profile = asset_context.profile

    snapshot = get_live_orderbook_snapshot(depth=depth)
    brti = get_brti_state()
    log_size = get_ws_message_log_size()
    kalshi_stats = get_ws_processing_stats()
    brti_stats = get_brti_ws_stats()

    selection_state = get_market_selection_state()
    active_asset = profile.asset
    feed_asset = str(brti.get("asset") or active_asset)
    asset_syncing = feed_asset != active_asset

    settlement_proxy = get_brti_settlement_proxy(window_seconds=profile.settlement_window_seconds)
    market_info = get_live_market_info()
    suggested_strike = extract_suggested_strike(market_info)
    close_iso = market_info.get("close_time") if isinstance(market_info.get("close_time"), str) else None
    market_ticker = market_info.get("ticker") if isinstance(market_info.get("ticker"), str) else None

    pricing = compute_live_pricing_snapshot(
        strike=suggested_strike,
        market_ticker=market_ticker,
        close_time_iso=close_iso,
    )
    microstructure = get_last_p_book_snapshot()

    payload = {
        "orderbook": snapshot,
        "brti": brti,
        "synthetic_settlement_proxy": settlement_proxy,
        "ws_log_size": log_size,
        "kalshi_ws_stats": kalshi_stats,
        "brti_ws_stats": brti_stats,
        "market_info": market_info,
        "asset": profile.asset,
        "asset_display": profile.display_name,
        "feed_asset": feed_asset,
        "asset_syncing": asset_syncing,
        "index_label": profile.index_label,
        "active_series": profile.kalshi_series_ticker,
        "market_selection": selection_state,
        "settlement_benchmark_label": profile.settlement_benchmark_label,
        "settlement_rule_text": profile.settlement_rule_text,
        "settlement_window_seconds": profile.settlement_window_seconds,
        "suggested_strike": suggested_strike,
        "pricing": pricing,
        "microstructure": microstructure,
    }
    return enforce_payload_contract(payload, STATE_PAYLOAD_KEYS)


def build_market_selection_payload() -> dict[str, Any]:
    state = get_market_selection_state()
    active_asset = str(state.get("active_asset") or "BTC")
    context = get_active_asset_context()

    payload = {
        "active_asset": context.profile.asset if context.profile.asset == active_asset else active_asset,
        "active_asset_display": context.profile.display_name,
        "active_series": context.profile.kalshi_series_ticker,
        "requested_asset": state.get("requested_asset"),
        "options": state.get("options", []),
        "applies_on_market_close": True,
    }
    return enforce_payload_contract(payload, MARKET_SELECTION_PAYLOAD_KEYS)


def request_market_selection(asset: str) -> tuple[dict[str, Any], int]:
    if not isinstance(asset, str) or not asset.strip():
        return {"ok": False, "error": "Missing required field 'asset'."}, 400

    result = request_asset_switch(asset)
    if not bool(result.get("ok")):
        return {"ok": False, "error": result.get("message", "Invalid asset selection.")}, 400

    state = get_market_selection_state()
    payload = {
        "ok": True,
        "status": result.get("status"),
        "message": result.get("message"),
        "active_asset": state.get("active_asset"),
        "requested_asset": state.get("requested_asset"),
        "applies_on_market_close": True,
    }
    return enforce_payload_contract(payload, MARKET_SELECTION_POST_RESPONSE_KEYS), 200
