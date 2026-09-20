from __future__ import annotations

import argparse
import csv
import json
import math
import time
from bisect import bisect_left, bisect_right
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.config import KALSHI_ENV
from core.market_metadata import extract_settlement_decimals, extract_suggested_strike
from core.market_profiles import MarketProfile, get_market_profile
from data.kalshi_rest import (
    get_cfbenchmarks_history,
    get_market_candlesticks,
    get_market_trades,
    get_market_trades_page,
    get_settled_markets,
)
from engine.market_stream.discovery import parse_iso8601_to_epoch
from engine.pricing.pipeline import compute_pricing_snapshot
from engine.trading.fees import taker_fee_cents_per_contract
from engine.trading.settings import TradingSettings
from engine.trading.strategy import apply_pricing_overrides

DEFAULT_HORIZONS_SECONDS = (600, 300, 120, 90, 60, 45, 30, 20, 10, 5, 1)
CF_HISTORY_MIN_INTERVAL_SEC = 0.26
CF_HISTORY_DATA_LAG_BUFFER_SEC = 20 * 60


@dataclass(frozen=True)
class ModelObservation:
    market_ticker: str
    eval_ts: float
    nominal_horizon_seconds: int
    seconds_to_expiry: float
    spot: float
    one_second_spot: float
    strike: float
    p_model: float
    p_model_one_second_spot: float
    actual_yes: int
    sigma_annual: float
    regime: str
    known_fix_count: int
    brier: float
    log_loss: float
    one_second_spot_brier: float
    one_second_spot_log_loss: float
    fast_spot_changed_probability: bool


@dataclass(frozen=True)
class QuoteObservation:
    market_ticker: str
    eval_ts: float
    seconds_to_expiry: float
    spot: float
    strike: float
    p_model: float
    actual_yes: int
    yes_bid_cents: float | None
    yes_ask_cents: float | None
    no_ask_cents: float | None
    yes_edge_cents: float | None
    no_edge_cents: float | None
    sigma_annual: float
    regime: str


@dataclass(frozen=True)
class TapeObservation:
    market_ticker: str
    trade_id: str
    eval_ts: float
    seconds_to_expiry: float
    count: float
    yes_trade_cents: float
    p_market: float
    p_model: float
    actual_yes: int
    model_minus_market_cents: float
    model_brier: float
    market_brier: float
    model_log_loss: float
    market_log_loss: float
    taker_outcome_side: str | None
    taker_book_side: str | None
    regime: str


@dataclass(frozen=True)
class MarketRelativeObservation:
    market_ticker: str
    target_horizon_seconds: int
    trade_ts: float
    trade_age_seconds: float
    seconds_to_expiry: float
    yes_trade_cents: float
    p_market: float
    p_model: float
    actual_yes: int
    model_minus_market_cents: float
    model_brier: float
    market_brier: float
    model_log_loss: float
    market_log_loss: float
    taker_outcome_side: str | None
    taker_book_side: str | None
    regime: str


@dataclass(frozen=True)
class BacktestTrade:
    market_ticker: str
    eval_ts: float
    side: str
    model_probability: float
    entry_cents: float
    fee_cents: float
    edge_cents: float
    pnl_cents: float
    won: bool


def _parse_timestamp(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        return ts if math.isfinite(ts) and ts > 0 else None
    if isinstance(value, str):
        try:
            numeric = float(value)
        except ValueError:
            return parse_iso8601_to_epoch(value)
        return _parse_timestamp(numeric)
    return None


def normalize_cf_history(rows: list[dict]) -> list[dict[str, float]]:
    """
    Normalize CF history while preserving original sub-second publication timestamps.

    PER_200MS contains the exact second-boundary values used by the 1 Hz feed plus
    intermediate ticks. Flooring sub-second rows would silently replace official
    second fixes with later 5 Hz values and corrupt both volatility and settlement.
    """
    by_millisecond: dict[int, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        ts = None
        for key in ("time", "timestamp", "source_ts_ms", "ts"):
            if key in row:
                ts = _parse_timestamp(row.get(key))
                if ts is not None:
                    break
        price = None
        for key in ("value", "value_usd", "price"):
            if key not in row:
                continue
            try:
                candidate = float(row[key])
            except (TypeError, ValueError):
                continue
            if math.isfinite(candidate) and candidate > 0:
                price = candidate
                break
        if ts is None or price is None:
            continue
        by_millisecond[int(round(ts * 1000.0))] = price

    return [
        {"ts": ts_ms / 1000.0, "price": by_millisecond[ts_ms]}
        for ts_ms in sorted(by_millisecond)
    ]


def one_second_boundary_ticks(
    ticks: list[dict[str, float]],
) -> list[dict[str, float]]:
    """Return only exact second-boundary CF values, matching cfbenchmarks_value."""
    return [
        tick
        for tick in ticks
        if int(round(float(tick["ts"]) * 1000.0)) % 1000 == 0
    ]


def _hour_start(ts: float) -> float:
    return float(int(ts) // 3600 * 3600)


def _iso_hour(ts: float) -> str:
    return (
        datetime.fromtimestamp(_hour_start(ts), UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def fetch_cf_range(
    index_id: str,
    start_ts: float,
    end_ts: float,
) -> list[dict[str, float]]:
    """Fetch a contiguous CF historical range at its published resolution."""
    raw: list[dict] = []
    cursor = _hour_start(start_ts)
    final_hour = _hour_start(end_ts)
    first = True
    while cursor <= final_hour:
        if not first:
            # Passthrough history reads cost 50 read tokens. Basic accounts have a
            # 200-token/sec budget, so pace bulk research at just under 4 req/sec.
            time.sleep(CF_HISTORY_MIN_INTERVAL_SEC)
        raw.extend(
            get_cfbenchmarks_history(
                index_id,
                timestamp=_iso_hour(cursor),
                timespan="HOUR",
            )
        )
        first = False
        cursor += 3600

    return [
        tick
        for tick in normalize_cf_history(raw)
        if start_ts <= tick["ts"] <= end_ts
    ]


def fetch_cf_feeds(
    profile: MarketProfile,
    start_ts: float,
    end_ts: float,
) -> tuple[list[dict[str, float]], list[dict[str, float]], str]:
    """
    Recreate the live 5 Hz/1 Hz split from CF's historical tick stream.

    Historical /history/values does not accept maxResolution. For subsecond RTIs it
    returns the published 200 ms ticks, including exact second-boundary values.
    The live bot uses all ticks only for current spot while the 1 Hz channel owns
    volatility history and settlement fixes, so replay filters those exact
    second-boundary publications explicitly.
    """
    published = fetch_cf_range(profile.index_id, start_ts, end_ts)
    if not published:
        raise ValueError("CF historical values are unavailable")

    if not profile.high_frequency:
        return list(published), list(published), "PER_SECOND"

    fix_ticks = one_second_boundary_ticks(published)
    if not fix_ticks:
        raise ValueError("CF history contained no exact one-second boundary values")
    return published, fix_ticks, "PER_200MS"


def _ticks_between(
    ticks: list[dict[str, float]],
    start_ts: float,
    end_ts: float,
    *,
    start_inclusive: bool = True,
) -> list[dict[str, float]]:
    """Slice sorted ticks in O(log n + k) without rescanning the full history."""
    key = lambda row: row["ts"]
    left = (
        bisect_left(ticks, start_ts, key=key)
        if start_inclusive
        else bisect_right(ticks, start_ts, key=key)
    )
    right = bisect_right(ticks, end_ts, key=key)
    return ticks[left:right]


def _latest_tick(
    ticks: list[dict[str, float]], now_ts: float
) -> dict[str, float] | None:
    if not ticks:
        return None
    index = bisect_right(ticks, now_ts, key=lambda row: row["ts"])
    return None if index == 0 else ticks[index - 1]


def _settlement_state(
    fix_ticks: list[dict[str, float]],
    *,
    now_ts: float,
    close_ts: float,
    window: int,
    spot_ts: float | None = None,
) -> dict[str, Any]:
    latest_fix = _latest_tick(fix_ticks, now_ts)
    timestamp = spot_ts if spot_ts is not None else (
        latest_fix["ts"] if latest_fix is not None else 0.0
    )
    if latest_fix is None or timestamp <= 0:
        return {"connected": False, "timestamp": 0.0}

    state: dict[str, Any] = {
        "connected": True,
        "timestamp": float(timestamp),
    }
    start = close_ts - window
    if now_ts <= start:
        return state

    fixes = _ticks_between(
        fix_ticks,
        start,
        min(now_ts, close_ts),
        start_inclusive=False,
    )
    if not fixes:
        return state

    mean = sum(tick["price"] for tick in fixes) / len(fixes)
    state.update(
        average_ts=latest_fix["ts"],
        final_average={
            "start": start,
            "end": fixes[-1]["ts"],
            "count": len(fixes),
            "value": mean,
        },
    )
    return state


def _pricing_at(
    *,
    profile: MarketProfile,
    asset: str,
    market: dict,
    strike: float,
    decimals: int,
    eval_ts: float,
    spot_ticks: list[dict[str, float]],
    fix_ticks: list[dict[str, float]],
    force_one_second_spot: bool = False,
    vol_window_seconds: float = 300.0,
    volatility_scale: float = 1.0,
) -> dict[str, Any]:
    spot_source = fix_ticks if force_one_second_spot else spot_ticks
    latest_spot = _latest_tick(spot_source, eval_ts)
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
    if snapshot.get("ready") and abs(float(volatility_scale) - 1.0) > 1e-12:
        snapshot = apply_pricing_overrides(
            snapshot,
            TradingSettings(volatility_scale=max(0.01, float(volatility_scale))),
        )
    return snapshot


def _dollars_to_cents(value: Any) -> float | None:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x * 100.0 if math.isfinite(x) else None


def _candle_close(candle: dict, side: str) -> float | None:
    quote = candle.get(side) or {}
    return _dollars_to_cents(
        quote.get("close_dollars")
        if quote.get("close_dollars") is not None
        else quote.get("close")
    )


def _candle_quote(candle: dict) -> tuple[float | None, float | None, float | None]:
    # Current live-tier candles use close_dollars; archived candles use close.
    yes_bid = _candle_close(candle, "yes_bid")
    yes_ask = _candle_close(candle, "yes_ask")
    no_ask = None if yes_bid is None else 100.0 - yes_bid
    return yes_bid, yes_ask, no_ask


def _scores(p: float, y: int) -> tuple[float, float]:
    p = min(1.0 - 1e-12, max(1e-12, float(p)))
    return (p - y) ** 2, -(y * math.log(p) + (1 - y) * math.log(1 - p))


def calibrate_market(
    market: dict,
    *,
    asset: str,
    spot_ticks: list[dict[str, float]],
    fix_ticks: list[dict[str, float]],
    horizons: tuple[int, ...],
    subsecond_offset: float,
    vol_window_seconds: float = 300.0,
    volatility_scale: float = 1.0,
    failure_counts: dict[str, int] | None = None,
) -> list[ModelObservation]:
    """Evaluate the production model at fixed horizons, including the final minute."""
    profile = get_market_profile(asset)
    ticker = str(market.get("ticker") or "")
    strike = extract_suggested_strike(market)
    close_ts = parse_iso8601_to_epoch(market.get("close_time"))
    result = str(market.get("result") or "").lower()
    if not ticker or strike is None or close_ts is None or result not in {"yes", "no"}:
        return []

    decimals = extract_settlement_decimals(
        market, profile.settlement_decimals_fallback
    )
    actual_yes = 1 if result == "yes" else 0
    rows: list[ModelObservation] = []

    for horizon in horizons:
        eval_ts = close_ts - float(horizon) + float(subsecond_offset)
        if eval_ts >= close_ts:
            continue
        fast = _pricing_at(
            profile=profile,
            asset=asset,
            market=market,
            strike=strike,
            decimals=decimals,
            eval_ts=eval_ts,
            spot_ticks=spot_ticks,
            fix_ticks=fix_ticks,
            vol_window_seconds=vol_window_seconds,
            volatility_scale=volatility_scale,
        )
        slow = _pricing_at(
            profile=profile,
            asset=asset,
            market=market,
            strike=strike,
            decimals=decimals,
            eval_ts=eval_ts,
            spot_ticks=spot_ticks,
            fix_ticks=fix_ticks,
            force_one_second_spot=True,
            vol_window_seconds=vol_window_seconds,
            volatility_scale=volatility_scale,
        )
        rejection = None
        if not fast.get("ready"):
            rejection = f"fast:{fast.get('reason') or 'not_ready'}"
        elif fast.get("vol_is_fallback"):
            rejection = "fast:vol_fallback"
        elif not slow.get("ready"):
            rejection = f"slow:{slow.get('reason') or 'not_ready'}"
        elif slow.get("vol_is_fallback"):
            rejection = "slow:vol_fallback"
        if rejection is not None:
            if failure_counts is not None:
                key = f"{int(horizon)}s:{rejection}"
                failure_counts[key] = failure_counts.get(key, 0) + 1
            continue

        latest_fast = _latest_tick(spot_ticks, eval_ts)
        latest_slow = _latest_tick(fix_ticks, eval_ts)
        if latest_fast is None or latest_slow is None:
            continue
        p_fast = float(fast["p_model"])
        p_slow = float(slow["p_model"])
        brier, log_loss = _scores(p_fast, actual_yes)
        slow_brier, slow_log_loss = _scores(p_slow, actual_yes)
        rows.append(
            ModelObservation(
                market_ticker=ticker,
                eval_ts=eval_ts,
                nominal_horizon_seconds=int(horizon),
                seconds_to_expiry=float(close_ts - eval_ts),
                spot=float(latest_fast["price"]),
                one_second_spot=float(latest_slow["price"]),
                strike=float(strike),
                p_model=p_fast,
                p_model_one_second_spot=p_slow,
                actual_yes=actual_yes,
                sigma_annual=float(fast["sigma_annual"]),
                regime=str(fast["regime"]),
                known_fix_count=int(fast.get("twap_samples_observed") or 0),
                brier=brier,
                log_loss=log_loss,
                one_second_spot_brier=slow_brier,
                one_second_spot_log_loss=slow_log_loss,
                fast_spot_changed_probability=abs(p_fast - p_slow) > 1e-12,
            )
        )
    return rows


def quote_screen_market(
    market: dict,
    *,
    asset: str,
    spot_ticks: list[dict[str, float]],
    fix_ticks: list[dict[str, float]],
    min_edge_cents: float,
    vol_window_seconds: float = 300.0,
) -> tuple[list[QuoteObservation], BacktestTrade | None]:
    """
    Coarse quote/PnL screen at one-minute candle closes.

    This intentionally remains separate from fixed-horizon model calibration because
    1-minute candles do not expose the quote path inside Kalshi's final settlement
    minute.
    """
    profile = get_market_profile(asset)
    ticker = str(market.get("ticker") or "")
    strike = extract_suggested_strike(market)
    close_ts = parse_iso8601_to_epoch(market.get("close_time"))
    open_ts = parse_iso8601_to_epoch(market.get("open_time"))
    result = str(market.get("result") or "").lower()
    if not ticker or strike is None or close_ts is None or result not in {"yes", "no"}:
        return [], None

    start_ts = max(open_ts or close_ts - 900, close_ts - 900)
    historical = market.get("_data_tier") == "historical"
    candles = get_market_candlesticks(
        series_ticker=profile.kalshi_series_ticker,
        ticker=ticker,
        start_ts=int(start_ts),
        end_ts=int(close_ts),
        period_interval=1,
        historical=historical,
    )
    decimals = extract_settlement_decimals(
        market, profile.settlement_decimals_fallback
    )
    actual_yes = 1 if result == "yes" else 0
    observations: list[QuoteObservation] = []
    trade: BacktestTrade | None = None

    for candle in sorted(candles, key=lambda row: int(row.get("end_period_ts") or 0)):
        eval_ts = float(candle.get("end_period_ts") or 0)
        if eval_ts <= 0 or eval_ts >= close_ts:
            continue

        snapshot = _pricing_at(
            profile=profile,
            asset=asset,
            market=market,
            strike=strike,
            decimals=decimals,
            eval_ts=eval_ts,
            spot_ticks=spot_ticks,
            fix_ticks=fix_ticks,
            vol_window_seconds=vol_window_seconds,
        )
        if not snapshot.get("ready") or snapshot.get("vol_is_fallback"):
            continue

        latest = _latest_tick(spot_ticks, eval_ts)
        if latest is None:
            continue
        p = float(snapshot["p_model"])
        yes_bid, yes_ask, no_ask = _candle_quote(candle)
        yes_edge = None
        no_edge = None
        if yes_ask is not None:
            yes_edge = (
                p * 100.0
                - yes_ask
                - taker_fee_cents_per_contract(yes_ask)
            )
        if no_ask is not None:
            no_edge = (
                (1.0 - p) * 100.0
                - no_ask
                - taker_fee_cents_per_contract(no_ask)
            )

        observations.append(
            QuoteObservation(
                market_ticker=ticker,
                eval_ts=eval_ts,
                seconds_to_expiry=close_ts - eval_ts,
                spot=float(latest["price"]),
                strike=float(strike),
                p_model=p,
                actual_yes=actual_yes,
                yes_bid_cents=yes_bid,
                yes_ask_cents=yes_ask,
                no_ask_cents=no_ask,
                yes_edge_cents=yes_edge,
                no_edge_cents=no_edge,
                sigma_annual=float(snapshot["sigma_annual"]),
                regime=str(snapshot["regime"]),
            )
        )

        if trade is None:
            candidates: list[tuple[str, float, float, float]] = []
            if yes_edge is not None and yes_ask is not None:
                candidates.append(("yes", p, yes_ask, yes_edge))
            if no_edge is not None and no_ask is not None:
                candidates.append(("no", 1.0 - p, no_ask, no_edge))
            if candidates:
                side, model_p, entry, edge = max(candidates, key=lambda row: row[3])
                if edge >= min_edge_cents:
                    fee = taker_fee_cents_per_contract(entry)
                    won = (side == "yes" and actual_yes == 1) or (
                        side == "no" and actual_yes == 0
                    )
                    trade = BacktestTrade(
                        market_ticker=ticker,
                        eval_ts=eval_ts,
                        side=side,
                        model_probability=model_p,
                        entry_cents=entry,
                        fee_cents=fee,
                        edge_cents=edge,
                        pnl_cents=(100.0 if won else 0.0) - entry - fee,
                        won=won,
                    )

    return observations, trade


def market_relative_horizons(
    market: dict,
    *,
    asset: str,
    spot_ticks: list[dict[str, float]],
    fix_ticks: list[dict[str, float]],
    horizons: tuple[int, ...],
    subsecond_offset: float,
    vol_window_seconds: float = 300.0,
    max_trade_age_seconds: float = 5.0,
) -> list[MarketRelativeObservation]:
    """Compare model probability with the latest non-block trade at fixed horizons.

    Each market contributes at most one observation per horizon, avoiding the severe
    activity weighting of raw trade-tape scoring. The selected trade must precede the
    target timestamp and be no more than max_trade_age_seconds stale.
    """
    profile = get_market_profile(asset)
    ticker = str(market.get("ticker") or "")
    strike = extract_suggested_strike(market)
    close_ts = parse_iso8601_to_epoch(market.get("close_time"))
    open_ts = parse_iso8601_to_epoch(market.get("open_time"))
    result = str(market.get("result") or "").lower()
    if not ticker or strike is None or close_ts is None or result not in {"yes", "no"}:
        return []

    start_ts = max(open_ts or close_ts - 900, close_ts - 900)
    decimals = extract_settlement_decimals(
        market, profile.settlement_decimals_fallback
    )
    actual_yes = 1 if result == "yes" else 0
    rows: list[MarketRelativeObservation] = []
    historical = market.get("_data_tier") == "historical"

    for horizon in horizons:
        target_ts = close_ts - float(horizon) + float(subsecond_offset)
        window_start = max(start_ts, target_ts - max_trade_age_seconds)
        raw_trades = get_market_trades_page(
            ticker=ticker,
            min_ts=int(math.floor(window_start)),
            max_ts=int(math.ceil(target_ts)),
            include_block_trades=False,
            historical=historical,
            limit=100,
        )
        candidates: list[tuple[float, dict]] = []
        for trade in raw_trades:
            ts = _parse_timestamp(trade.get("created_time"))
            if ts is not None and window_start <= ts <= target_ts:
                candidates.append((ts, trade))
        if not candidates:
            continue
        trade_ts, trade = max(candidates, key=lambda row: row[0])
        age = target_ts - trade_ts
        yes_cents = _dollars_to_cents(trade.get("yes_price_dollars"))
        if yes_cents is None or not 0 < yes_cents < 100:
            continue

        snapshot = _pricing_at(
            profile=profile,
            asset=asset,
            market=market,
            strike=strike,
            decimals=decimals,
            eval_ts=trade_ts,
            spot_ticks=spot_ticks,
            fix_ticks=fix_ticks,
            vol_window_seconds=vol_window_seconds,
        )
        if not snapshot.get("ready") or snapshot.get("vol_is_fallback"):
            continue

        p_market = yes_cents / 100.0
        p_model = float(snapshot["p_model"])
        model_brier, model_log_loss = _scores(p_model, actual_yes)
        market_brier, market_log_loss = _scores(p_market, actual_yes)
        rows.append(
            MarketRelativeObservation(
                market_ticker=ticker,
                target_horizon_seconds=int(horizon),
                trade_ts=trade_ts,
                trade_age_seconds=age,
                seconds_to_expiry=close_ts - trade_ts,
                yes_trade_cents=yes_cents,
                p_market=p_market,
                p_model=p_model,
                actual_yes=actual_yes,
                model_minus_market_cents=p_model * 100.0 - yes_cents,
                model_brier=model_brier,
                market_brier=market_brier,
                model_log_loss=model_log_loss,
                market_log_loss=market_log_loss,
                taker_outcome_side=(
                    str(trade.get("taker_outcome_side"))
                    if trade.get("taker_outcome_side") is not None
                    else None
                ),
                taker_book_side=(
                    str(trade.get("taker_book_side"))
                    if trade.get("taker_book_side") is not None
                    else None
                ),
                regime=str(snapshot["regime"]),
            )
        )
    return rows


def trade_tape_market(
    market: dict,
    *,
    asset: str,
    spot_ticks: list[dict[str, float]],
    fix_ticks: list[dict[str, float]],
    vol_window_seconds: float = 300.0,
) -> list[TapeObservation]:
    """
    Compare the production model with actual public trade prices at trade timestamps.

    This is a market-relative diagnostic, not an execution backtest: an observed
    transaction proves that somebody traded at that price, not that our IOC would
    have filled concurrently.
    """
    profile = get_market_profile(asset)
    ticker = str(market.get("ticker") or "")
    strike = extract_suggested_strike(market)
    close_ts = parse_iso8601_to_epoch(market.get("close_time"))
    open_ts = parse_iso8601_to_epoch(market.get("open_time"))
    result = str(market.get("result") or "").lower()
    if not ticker or strike is None or close_ts is None or result not in {"yes", "no"}:
        return []

    start_ts = max(open_ts or close_ts - 900, close_ts - 900)
    trades = get_market_trades(
        ticker=ticker,
        min_ts=int(start_ts),
        max_ts=int(close_ts),
        include_block_trades=False,
        historical=market.get("_data_tier") == "historical",
    )
    decimals = extract_settlement_decimals(
        market, profile.settlement_decimals_fallback
    )
    actual_yes = 1 if result == "yes" else 0
    rows: list[TapeObservation] = []

    for trade in trades:
        eval_ts = _parse_timestamp(trade.get("created_time"))
        if eval_ts is None or not start_ts <= eval_ts < close_ts:
            continue
        yes_cents = _dollars_to_cents(trade.get("yes_price_dollars"))
        if yes_cents is None or not 0 < yes_cents < 100:
            continue
        snapshot = _pricing_at(
            profile=profile,
            asset=asset,
            market=market,
            strike=strike,
            decimals=decimals,
            eval_ts=eval_ts,
            spot_ticks=spot_ticks,
            fix_ticks=fix_ticks,
            vol_window_seconds=vol_window_seconds,
        )
        if not snapshot.get("ready") or snapshot.get("vol_is_fallback"):
            continue

        try:
            count = float(trade.get("count_fp") or 0.0)
        except (TypeError, ValueError):
            count = 0.0
        p_market = yes_cents / 100.0
        p_model = float(snapshot["p_model"])
        model_brier, model_log_loss = _scores(p_model, actual_yes)
        market_brier, market_log_loss = _scores(p_market, actual_yes)
        rows.append(
            TapeObservation(
                market_ticker=ticker,
                trade_id=str(trade.get("trade_id") or ""),
                eval_ts=eval_ts,
                seconds_to_expiry=close_ts - eval_ts,
                count=max(0.0, count),
                yes_trade_cents=yes_cents,
                p_market=p_market,
                p_model=p_model,
                actual_yes=actual_yes,
                model_minus_market_cents=p_model * 100.0 - yes_cents,
                model_brier=model_brier,
                market_brier=market_brier,
                model_log_loss=model_log_loss,
                market_log_loss=market_log_loss,
                taker_outcome_side=(
                    str(trade.get("taker_outcome_side"))
                    if trade.get("taker_outcome_side") is not None
                    else None
                ),
                taker_book_side=(
                    str(trade.get("taker_book_side"))
                    if trade.get("taker_book_side") is not None
                    else None
                ),
                regime=str(snapshot["regime"]),
            )
        )
    return rows


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def summarize(
    model_rows: list[ModelObservation],
    quote_rows: list[QuoteObservation],
    relative_rows: list[MarketRelativeObservation],
    tape_rows: list[TapeObservation],
    trades: list[BacktestTrade],
    *,
    cf_resolution: str,
    vol_window_seconds: float,
) -> dict[str, Any]:
    by_horizon = []
    for horizon in sorted({row.nominal_horizon_seconds for row in model_rows}, reverse=True):
        rows = [row for row in model_rows if row.nominal_horizon_seconds == horizon]
        by_horizon.append(
            {
                "horizon_seconds": horizon,
                "n": len(rows),
                "mean_brier": _mean([row.brier for row in rows]),
                "mean_log_loss": _mean([row.log_loss for row in rows]),
                "one_second_spot_mean_brier": _mean(
                    [row.one_second_spot_brier for row in rows]
                ),
                "one_second_spot_mean_log_loss": _mean(
                    [row.one_second_spot_log_loss for row in rows]
                ),
                "fast_spot_probability_change_rate": _mean(
                    [1.0 if row.fast_spot_changed_probability else 0.0 for row in rows]
                ),
                "mean_known_fix_count": _mean(
                    [float(row.known_fix_count) for row in rows]
                ),
            }
        )

    market_relative_by_horizon = []
    for horizon in sorted(
        {row.target_horizon_seconds for row in relative_rows}, reverse=True
    ):
        rows = [row for row in relative_rows if row.target_horizon_seconds == horizon]
        market_relative_by_horizon.append(
            {
                "horizon_seconds": horizon,
                "n": len(rows),
                "mean_trade_age_seconds": _mean(
                    [row.trade_age_seconds for row in rows]
                ),
                "model_mean_brier": _mean([row.model_brier for row in rows]),
                "market_mean_brier": _mean([row.market_brier for row in rows]),
                "model_mean_log_loss": _mean(
                    [row.model_log_loss for row in rows]
                ),
                "market_mean_log_loss": _mean(
                    [row.market_log_loss for row in rows]
                ),
                "mean_model_minus_market_cents": _mean(
                    [row.model_minus_market_cents for row in rows]
                ),
            }
        )

    calibration = []
    for lower_i in range(10):
        lower = lower_i / 10.0
        upper = (lower_i + 1) / 10.0
        rows = [
            row
            for row in model_rows
            if lower <= row.p_model < upper
            or (upper == 1.0 and row.p_model == 1.0)
        ]
        if rows:
            calibration.append(
                {
                    "range": f"{lower:.1f}-{upper:.1f}",
                    "n": len(rows),
                    "mean_model": _mean([row.p_model for row in rows]),
                    "actual_yes_rate": _mean([float(row.actual_yes) for row in rows]),
                }
            )

    pnl = sum(row.pnl_cents for row in trades)
    cost = sum(row.entry_cents + row.fee_cents for row in trades)
    return {
        "cf_spot_resolution": cf_resolution,
        "vol_window_seconds": float(vol_window_seconds),
        "model_observations": len(model_rows),
        "markets_calibrated": len({row.market_ticker for row in model_rows}),
        "mean_brier": _mean([row.brier for row in model_rows]),
        "mean_log_loss": _mean([row.log_loss for row in model_rows]),
        "one_second_spot_mean_brier": _mean(
            [row.one_second_spot_brier for row in model_rows]
        ),
        "one_second_spot_mean_log_loss": _mean(
            [row.one_second_spot_log_loss for row in model_rows]
        ),
        "by_horizon": by_horizon,
        "calibration": calibration,
        "market_relative_observations": len(relative_rows),
        "market_relative_markets": len({row.market_ticker for row in relative_rows}),
        "market_relative_model_mean_brier": _mean(
            [row.model_brier for row in relative_rows]
        ),
        "market_relative_market_mean_brier": _mean(
            [row.market_brier for row in relative_rows]
        ),
        "market_relative_model_mean_log_loss": _mean(
            [row.model_log_loss for row in relative_rows]
        ),
        "market_relative_market_mean_log_loss": _mean(
            [row.market_log_loss for row in relative_rows]
        ),
        "market_relative_by_horizon": market_relative_by_horizon,
        "tape_observations": len(tape_rows),
        "tape_markets": len({row.market_ticker for row in tape_rows}),
        "tape_model_mean_brier": _mean([row.model_brier for row in tape_rows]),
        "tape_market_mean_brier": _mean([row.market_brier for row in tape_rows]),
        "tape_model_mean_log_loss": _mean([row.model_log_loss for row in tape_rows]),
        "tape_market_mean_log_loss": _mean([row.market_log_loss for row in tape_rows]),
        "tape_model_minus_market_mean_cents": _mean(
            [row.model_minus_market_cents for row in tape_rows]
        ),
        "quote_observations": len(quote_rows),
        "quote_screen_trades": len(trades),
        "quote_screen_wins": sum(1 for row in trades if row.won),
        "quote_screen_pnl_cents": pnl,
        "quote_screen_roi_on_entry_cost": (pnl / cost) if cost > 0 else None,
        "quote_screen_mean_signal_edge_cents": _mean(
            [row.edge_cents for row in trades]
        ),
        "execution_note": (
            "Quote-screen PnL uses one-minute historical quote closes and is not an "
            "L2/latency-accurate execution replay. Public-trade tape comparisons are "
            "market-relative diagnostics, not hypothetical fills. Fixed-horizon model "
            "calibration is independent of quote availability and includes the final "
            "settlement minute."
        ),
    }


def run_backtest(
    *,
    asset: str,
    max_markets: int,
    min_edge_cents: float,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS_SECONDS,
    vol_window_seconds: float = 300.0,
    volatility_scale: float = 1.0,
    include_tape: bool = False,
    include_quotes: bool = True,
    include_market_relative: bool = True,
    max_trade_age_seconds: float = 5.0,
) -> tuple[
    list[ModelObservation],
    list[QuoteObservation],
    list[MarketRelativeObservation],
    list[TapeObservation],
    list[BacktestTrade],
    dict[str, Any],
]:
    profile = get_market_profile(asset)
    research_cutoff = time.time() - CF_HISTORY_DATA_LAG_BUFFER_SEC
    markets = [
        market
        for market in get_settled_markets(profile.kalshi_series_ticker)
        if str(market.get("result") or "").lower() in {"yes", "no"}
        and extract_suggested_strike(market) is not None
        and (parse_iso8601_to_epoch(market.get("close_time")) or float("inf"))
        <= research_cutoff
    ]
    markets.sort(
        key=lambda row: parse_iso8601_to_epoch(row.get("close_time")) or 0,
        reverse=True,
    )
    if max_markets > 0:
        markets = markets[:max_markets]
    if not markets:
        empty = summarize(
            [], [], [], [], [], cf_resolution="unavailable",
            vol_window_seconds=vol_window_seconds
        )
        return [], [], [], [], [], empty

    closes = [
        parse_iso8601_to_epoch(market.get("close_time"))
        for market in markets
    ]
    closes = [ts for ts in closes if ts is not None]
    max_horizon = max(horizons) if horizons else 600
    spot_ticks, fix_ticks, resolution = fetch_cf_feeds(
        profile,
        min(closes) - max(20 * 60, max_horizon + 600),
        max(closes) + 1,
    )
    subsecond_offset = 0.4 if resolution == "PER_200MS" else 0.0

    model_rows: list[ModelObservation] = []
    calibration_failures: dict[str, int] = {}
    quote_rows: list[QuoteObservation] = []
    relative_rows: list[MarketRelativeObservation] = []
    tape_rows: list[TapeObservation] = []
    trades: list[BacktestTrade] = []
    for market in markets:
        model_rows.extend(
            calibrate_market(
                market,
                asset=profile.asset,
                spot_ticks=spot_ticks,
                fix_ticks=fix_ticks,
                horizons=horizons,
                subsecond_offset=subsecond_offset,
                vol_window_seconds=vol_window_seconds,
                volatility_scale=volatility_scale,
                failure_counts=calibration_failures,
            )
        )
        if include_market_relative:
            relative_rows.extend(
                market_relative_horizons(
                    market,
                    asset=profile.asset,
                    spot_ticks=spot_ticks,
                    fix_ticks=fix_ticks,
                    horizons=horizons,
                    subsecond_offset=subsecond_offset,
                    vol_window_seconds=vol_window_seconds,
                    max_trade_age_seconds=max_trade_age_seconds,
                )
            )
        if include_tape:
            tape_rows.extend(
                trade_tape_market(
                    market,
                    asset=profile.asset,
                    spot_ticks=spot_ticks,
                    fix_ticks=fix_ticks,
                    vol_window_seconds=vol_window_seconds,
                )
            )
        if include_quotes:
            market_quotes, trade = quote_screen_market(
                market,
                asset=profile.asset,
                spot_ticks=spot_ticks,
                fix_ticks=fix_ticks,
                min_edge_cents=min_edge_cents,
                vol_window_seconds=vol_window_seconds,
            )
            quote_rows.extend(market_quotes)
            if trade is not None:
                trades.append(trade)

    summary = summarize(
        model_rows,
        quote_rows,
        relative_rows,
        tape_rows,
        trades,
        cf_resolution=resolution,
        vol_window_seconds=vol_window_seconds,
    )
    summary["calibration_rejections"] = dict(sorted(calibration_failures.items()))
    summary["volatility_scale"] = float(volatility_scale)
    return model_rows, quote_rows, relative_rows, tape_rows, trades, summary


def _write_csv(path: Path, rows: list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    records = [asdict(row) for row in rows]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)


def main() -> None:
    if KALSHI_ENV != "prod":
        raise RuntimeError(
            "Historical research uses production Kalshi/CF data; set KALSHI_ENV=prod."
        )
    parser = argparse.ArgumentParser(
        description="Backtest Kalshi 15-minute crypto model calibration and coarse alpha."
    )
    parser.add_argument("asset", nargs="?", default="BTC")
    parser.add_argument("--max-markets", type=int, default=100)
    parser.add_argument("--min-edge-cents", type=float, default=2.0)
    parser.add_argument(
        "--horizons",
        default=",".join(str(value) for value in DEFAULT_HORIZONS_SECONDS),
        help="Comma-separated seconds-to-expiry calibration horizons.",
    )
    parser.add_argument("--vol-window-seconds", type=float, default=300.0)
    parser.add_argument("--volatility-scale", type=float, default=1.0)
    parser.add_argument(
        "--full-tape",
        action="store_true",
        help="Also score every public trade; expensive and activity-weighted.",
    )
    parser.add_argument("--skip-market-relative", action="store_true")
    parser.add_argument("--skip-quotes", action="store_true")
    parser.add_argument("--max-trade-age-seconds", type=float, default=5.0)
    parser.add_argument("--output-dir", default="output/backtests")
    args = parser.parse_args()

    horizons = tuple(
        sorted(
            {
                int(value)
                for value in args.horizons.split(",")
                if value.strip() and int(value) > 0
            },
            reverse=True,
        )
    )
    model_rows, quote_rows, relative_rows, tape_rows, trades, summary = run_backtest(
        asset=args.asset,
        max_markets=max(0, args.max_markets),
        min_edge_cents=max(0.0, args.min_edge_cents),
        horizons=horizons,
        vol_window_seconds=max(1.0, args.vol_window_seconds),
        volatility_scale=max(0.01, args.volatility_scale),
        include_tape=args.full_tape,
        include_quotes=not args.skip_quotes,
        include_market_relative=not args.skip_market_relative,
        max_trade_age_seconds=max(0.0, args.max_trade_age_seconds),
    )
    out = Path(args.output_dir)
    _write_csv(out / "calibration.csv", model_rows)
    _write_csv(out / "quote_observations.csv", quote_rows)
    _write_csv(out / "market_relative.csv", relative_rows)
    _write_csv(out / "tape_observations.csv", tape_rows)
    _write_csv(out / "trades.csv", trades)
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
