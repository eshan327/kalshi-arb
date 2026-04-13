"""Live index + vol + Asian pricer snapshot for the Flask API."""

from __future__ import annotations

import time
from threading import Lock
from typing import Any

from core.asset_context import get_active_asset_context
from engine.pricing.pipeline import compute_pricing_snapshot
from feeds.state.tick_store import get_brti_state, get_brti_tick_version, get_brti_ticks

_BRTI_TICK_LOOKBACK = 4000
_snapshot_cache_lock = Lock()
_snapshot_cache_key: tuple[Any, ...] | None = None
_snapshot_cache_value: dict[str, Any] | None = None


def _build_snapshot_cache_key(
    *,
    profile_asset: str,
    feed_asset: str,
    spot: float | int | None,
    strike: float | None,
    market_ticker: str | None,
    close_time_iso: str | None,
    tick_version: int,
    second_bucket: int,
) -> tuple[Any, ...]:
    spot_key = float(spot) if isinstance(spot, (int, float)) else None
    strike_key = float(strike) if isinstance(strike, (int, float)) else None
    return (
        profile_asset,
        feed_asset,
        spot_key,
        strike_key,
        market_ticker or "",
        close_time_iso or "",
        int(tick_version),
        int(second_bucket),
    )


def reset_live_pricing_for_new_market() -> None:
    """Clears cached snapshot state when market streamer rotates contracts."""
    global _snapshot_cache_key, _snapshot_cache_value
    with _snapshot_cache_lock:
        _snapshot_cache_key = None
        _snapshot_cache_value = None


def compute_live_pricing_snapshot(
    *,
    strike: float | None,
    market_ticker: str | None,
    close_time_iso: str | None,
) -> dict[str, Any]:
    global _snapshot_cache_key, _snapshot_cache_value

    context = get_active_asset_context()
    profile = context.profile

    brti_state = get_brti_state()
    spot = brti_state.get("brti")
    feed_asset_raw = brti_state.get("asset")
    feed_asset = feed_asset_raw.upper().strip() if isinstance(feed_asset_raw, str) else profile.asset

    now_ts = time.time()
    second_bucket = int(now_ts)
    tick_version = get_brti_tick_version()
    cache_key = _build_snapshot_cache_key(
        profile_asset=profile.asset,
        feed_asset=feed_asset,
        spot=spot,
        strike=strike,
        market_ticker=market_ticker,
        close_time_iso=close_time_iso,
        tick_version=tick_version,
        second_bucket=second_bucket,
    )

    with _snapshot_cache_lock:
        if _snapshot_cache_key == cache_key and _snapshot_cache_value is not None:
            return dict(_snapshot_cache_value)

    ticks = get_brti_ticks(limit=_BRTI_TICK_LOOKBACK)
    snapshot = compute_pricing_snapshot(
        profile=profile,
        feed_asset=feed_asset,
        spot=spot,
        ticks=ticks,
        strike=strike,
        market_ticker=market_ticker,
        close_time_iso=close_time_iso,
    )

    with _snapshot_cache_lock:
        _snapshot_cache_key = cache_key
        _snapshot_cache_value = dict(snapshot)

    return snapshot
