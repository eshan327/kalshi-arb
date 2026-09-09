from __future__ import annotations

import math
import time
from typing import Any

from core.market_profiles import MarketProfile
from engine.asian_pricer import prob_collapsed_variance_binary, prob_levy_tw_binary
from engine.market_stream.discovery import parse_iso8601_to_epoch
from engine.vol_estimator import realized_vol_from_price_points


def compute_pricing_snapshot(
    *,
    profile: MarketProfile,
    feed_asset: str,
    spot: float | None,
    ticks: list[dict],
    strike: float | None,
    market_ticker: str | None,
    close_time_iso: str | None,
    settlement_decimals: int | None = None,
    index_state: dict | None = None,
) -> dict[str, Any]:
    state = index_state or {}
    now = time.time()
    close = parse_iso8601_to_epoch(close_time_iso)
    decimals = (
        profile.settlement_decimals_fallback
        if settlement_decimals is None
        else settlement_decimals
    )
    window = profile.settlement_window_seconds
    base = dict(
        asset=profile.asset,
        feed_asset=feed_asset,
        index_label=profile.index_label,
        settlement_window_seconds=window,
        strike_usd=strike,
        market_ticker=market_ticker,
        spot_index=spot,
        settlement_decimals=decimals,
        ready=False,
        reason=None,
        seconds_to_expiry=None if close is None else max(0.0, close - now),
        p_model=None,
        p_model_pct=None,
        vol_is_fallback=True,
        twap_samples_observed=0,
        twap_partial_avg=None,
        twap_partial_avg_raw=None,
        twap_required_avg=None,
        twap_seconds_elapsed=0,
    )

    def fail(reason):
        return {**base, "reason": reason}

    if close is None or strike is None or not math.isfinite(strike):
        return fail("missing_market_terms")
    if feed_asset != profile.asset or not state.get("connected"):
        return fail("index_disconnected")
    if spot is None or not math.isfinite(spot) or spot <= 0:
        return fail("no_index")
    age = now - float(state.get("timestamp", 0))
    base["index_age_seconds"] = age
    if not 0 <= age <= 5:
        return fail("stale_index")
    if close <= now:
        return fail("market_closed")

    points = [(t["ts"], t["price"]) for t in ticks]
    sigma = realized_vol_from_price_points(
        points, window_seconds=300, now_ts=now, min_samples=8
    )
    fallback = sigma is None or sigma <= 0
    sigma = profile.fallback_sigma_annual if fallback else sigma
    model_strike = strike - 0.5 * 10**-decimals
    seconds = close - now
    base.update(
        sigma_annual=sigma,
        sigma_samples=len(points),
        vol_is_fallback=fallback,
        model_strike_usd=model_strike,
        rounding_half_unit=0.5 * 10**-decimals,
    )

    if seconds > window:
        result = prob_levy_tw_binary(spot, model_strike, sigma, seconds, n_fixes=window)
    else:
        # Use Kalshi's accumulation, including its boundary semantics and sample count.
        # Never manufacture missing fixes by forward filling or mixing in 5 Hz ticks.
        avg = state.get("final_average")
        elapsed = max(0, math.floor(now - (close - window)))
        base["twap_seconds_elapsed"] = elapsed
        if elapsed == 0 and avg is None:
            count, mean = 0, None
        else:
            if not avg or abs(avg["start"] - (close - window)) > 0.001:
                return fail("settlement_average_unavailable")
            if not 0 <= now - float(state.get("average_ts", 0)) <= 2:
                return fail("stale_settlement_average")
            count, mean = avg["count"], avg["value"]
            if (
                not 0 <= count <= min(window, elapsed)
                or avg["end"] < avg["start"]
                or avg["end"] > close
                or avg["end"] > now + 0.001
            ):
                return fail("invalid_settlement_average")
            # One-second tolerance for delivery; larger holes cannot become known fixes.
            if count < max(0, elapsed - 1):
                return fail("incomplete_settlement_average")
        base.update(
            twap_samples_observed=count,
            twap_partial_avg_raw=mean,
            twap_partial_avg=None if mean is None else round(mean, decimals),
        )
        if count and count < window:
            base["twap_required_avg"] = (model_strike * window - mean * count) / (
                window - count
            )
        result = prob_collapsed_variance_binary(
            model_strike, sigma, n=window, k=count, mean_known_samples=mean, mu_fwd=spot
        )
    base.update(
        p_model=result.p_model,
        p_model_pct=100 * result.p_model,
        regime=result.regime,
        sigma_eff=result.sigma_eff,
        pricer_detail={
            k: (None if isinstance(v, float) and not math.isfinite(v) else v)
            for k, v in result.detail.items()
        },
        ready=True,
    )
    return base
