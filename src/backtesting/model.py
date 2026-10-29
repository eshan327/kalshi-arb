"""Build fixed-horizon baseline observations from historical inputs."""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass
from typing import Any

from core.markets import (
    MarketProfile,
    extract_settlement_decimals,
    extract_suggested_strike,
    get_market_profile,
    parse_iso8601_to_epoch,
)
from pricing.baseline import compute_pricing_snapshot
from pricing.vol_estimator import BASELINE_VOL_WINDOW_SECONDS
from backtesting.cf import _latest_tick, _parse_timestamp, _settlement_state, _ticks_between

DEFAULT_HORIZONS_SECONDS = (600, 300, 120, 90, 60, 45, 30, 20, 10, 5, 1)


@dataclass(frozen=True)
class ModelObservation:
    market_ticker: str
    close_ts: float
    outcome_ts: float
    eval_ts: float
    nominal_horizon_seconds: int
    seconds_to_expiry: float
    actual_yes: int
    p_model: float
    spot: float
    strike: float
    model_strike: float
    settlement_decimals: int
    sigma_annual: float
    known_fix_count: int
    partial_average: float | None
    required_remaining_average: float | None
    regime: str
    p_market: float | None
    market_trade_ts: float | None
    market_trade_age_seconds: float | None
    yes_bid_cents: float | None = None
    yes_ask_cents: float | None = None
    quote_age_seconds: float | None = None

def pricing_at(
    *,
    profile: MarketProfile,
    asset: str,
    market: dict,
    strike: float,
    decimals: int,
    eval_ts: float,
    spot_ticks: list[dict[str, float]],
    fix_ticks: list[dict[str, float]],
    vol_window_seconds: float = BASELINE_VOL_WINDOW_SECONDS,
) -> dict[str, Any]:
    latest_spot = _latest_tick(spot_ticks, eval_ts)
    latest_fix = _latest_tick(fix_ticks, eval_ts)
    if latest_spot is None or latest_fix is None:
        return {"ready": False, "reason": "missing_history"}

    state = _settlement_state(
        fix_ticks,
        now_ts=eval_ts,
        close_ts=parse_iso8601_to_epoch(market.get("close_time")) or 0.0,
        window=profile.settlement_window_seconds,
        spot_ts=latest_spot["ts"],
    )
    available_fixes = _ticks_between(
        fix_ticks,
        eval_ts - max(float(vol_window_seconds), 1.0),
        eval_ts,
    )
    snapshot = compute_pricing_snapshot(
        profile=profile,
        feed_asset=asset,
        spot=latest_spot["price"],
        ticks=available_fixes,
        strike=strike,
        market_ticker=str(market.get("ticker") or ""),
        close_time_iso=market.get("close_time"),
        settlement_decimals=decimals,
        index_state=state,
        now_ts=eval_ts,
        vol_window_seconds=vol_window_seconds,
    )
    return snapshot


def evaluate_market(
    market: dict,
    *,
    asset: str,
    spot_ticks: list[dict[str, float]],
    fix_ticks: list[dict[str, float]],
    horizons: tuple[int, ...] = DEFAULT_HORIZONS_SECONDS,
    subsecond_offset: float = 0.0,
    trades: list[dict] | None = None,
    quotes: list[dict] | None = None,
    max_trade_age_seconds: float = 5.0,
    max_quote_age_seconds: float = 2.0,
    vol_window_seconds: float = BASELINE_VOL_WINDOW_SECONDS,
    failure_counts: dict[str, int] | None = None,
) -> list[ModelObservation]:
    """Export raw baseline state; every input to a prediction is available by eval_ts.

    A recent trade is only a market-probability proxy, never an executable quote.
    Its timestamp and age remain visible; missing/stale comparisons stay null.
    """
    if (
        not 0 <= subsecond_offset < 1
        or not math.isfinite(max_trade_age_seconds)
        or max_trade_age_seconds < 0
    ):
        raise ValueError("Invalid observation offset or maximum trade age")
    if not horizons or any(not isinstance(h, int) or h <= 0 for h in horizons):
        raise ValueError("Horizons must be positive whole seconds")
    profile = get_market_profile(asset)
    if not profile.index_id:
        raise ValueError("Pyth-settled metals require a separate price model; use historical_data.py for market history")
    ticker = str(market.get("ticker") or "")
    strike = extract_suggested_strike(market)
    close = parse_iso8601_to_epoch(market.get("close_time"))
    outcome_ts = parse_iso8601_to_epoch(market.get("settlement_ts")) or close
    opened = parse_iso8601_to_epoch(market.get("open_time"))
    outcome = str(market.get("result") or "").lower()
    if not ticker or strike is None or close is None or outcome not in {"yes", "no"}:
        raise ValueError("A settled market with explicit terms is required")
    decimals = extract_settlement_decimals(market, profile.settlement_decimals_fallback)
    tape = []
    for trade in trades or []:
        ts = _parse_timestamp(trade.get("created_time"))
        try:
            probability = float(trade["yes_price_dollars"])
        except (KeyError, TypeError, ValueError):
            continue
        if ts is not None and 0 <= probability <= 1 and not trade.get("is_block_trade"):
            tape.append((ts, probability))
    tape.sort(key=lambda item: item[0])
    quote_tape = sorted(quotes or [], key=lambda row: row["ts"])
    rows = []
    for horizon in sorted(set(horizons), reverse=True):
        now = close - horizon + subsecond_offset
        if opened is not None and now < opened:
            reason = "before_market_open"
            snapshot = {}
        else:
            snapshot = pricing_at(
                profile=profile,
                asset=profile.asset,
                market=market,
                strike=strike,
                decimals=decimals,
                eval_ts=now,
                spot_ticks=spot_ticks,
                fix_ticks=fix_ticks,
                vol_window_seconds=vol_window_seconds,
            )
            reason = snapshot.get("reason")
        if not snapshot.get("ready"):
            if failure_counts is not None:
                key = f"{horizon}s:{reason or 'not_ready'}"
                failure_counts[key] = failure_counts.get(key, 0) + 1
            continue
        index = bisect_right(tape, now, key=lambda item: item[0])
        trade_ts, p_market = tape[index - 1] if index else (None, None)
        age = None if trade_ts is None else now - trade_ts
        if age is not None and age > max_trade_age_seconds:
            trade_ts, p_market, age = None, None, None
        quote_index = bisect_right(quote_tape, now, key=lambda item: item["ts"])
        quote = quote_tape[quote_index - 1] if quote_index else None
        quote_age = now - quote["ts"] if quote else None
        if quote_age is not None and (
            quote_age > max_quote_age_seconds
            or quote["yes_bid_cents"] is None
            or quote["yes_ask_cents"] is None
        ):
            quote, quote_age = None, None
        rows.append(
            ModelObservation(
                market_ticker=ticker,
                close_ts=close,
                outcome_ts=outcome_ts,
                eval_ts=now,
                nominal_horizon_seconds=horizon,
                seconds_to_expiry=close - now,
                actual_yes=int(outcome == "yes"),
                p_model=snapshot["p_model"],
                spot=snapshot["spot_index"],
                strike=strike,
                model_strike=snapshot["model_strike_usd"],
                settlement_decimals=decimals,
                sigma_annual=snapshot["sigma_annual"],
                known_fix_count=snapshot["twap_samples_observed"],
                partial_average=snapshot["twap_partial_avg_raw"],
                required_remaining_average=snapshot["twap_required_avg"],
                regime=snapshot["regime"],
                p_market=p_market,
                market_trade_ts=trade_ts,
                market_trade_age_seconds=age,
                yes_bid_cents=quote["yes_bid_cents"] if quote else None,
                yes_ask_cents=quote["yes_ask_cents"] if quote else None,
                quote_age_seconds=quote_age,
            )
        )
    return rows
