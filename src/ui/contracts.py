from __future__ import annotations

from typing import Any

STATE_PAYLOAD_KEYS: tuple[str, ...] = (
    "orderbook",
    "brti",
    "synthetic_settlement_proxy",
    "ws_log_size",
    "kalshi_ws_stats",
    "brti_ws_stats",
    "market_info",
    "asset",
    "asset_display",
    "feed_asset",
    "asset_syncing",
    "index_label",
    "active_series",
    "market_selection",
    "settlement_benchmark_label",
    "settlement_rule_text",
    "settlement_window_seconds",
    "suggested_strike",
    "pricing",
    "microstructure",
)

MARKET_SELECTION_PAYLOAD_KEYS: tuple[str, ...] = (
    "active_asset",
    "active_asset_display",
    "active_series",
    "requested_asset",
    "options",
    "applies_on_market_close",
)

MARKET_SELECTION_POST_RESPONSE_KEYS: tuple[str, ...] = (
    "ok",
    "status",
    "message",
    "active_asset",
    "requested_asset",
    "applies_on_market_close",
)


def enforce_payload_contract(payload: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    """Return a deterministic payload shape while preserving any additive fields."""
    out: dict[str, Any] = {key: payload.get(key) for key in keys}
    for key, value in payload.items():
        if key not in out:
            out[key] = value
    return out
