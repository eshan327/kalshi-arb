from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from core.market_profiles import MarketProfile
from engine.asian_pricer import (
    AsianBinaryPricerResult,
    prob_collapsed_variance_binary,
    prob_levy_tw_binary,
)
from engine.settlement_sampling import (
    extract_valid_index_points,
    reconstruct_discrete_forward_fill_samples,
)
from engine.vol_estimator import realized_vol_from_price_points

_VOL_WINDOW_SEC = 300.0
_MAX_SAMPLE_STALENESS_SEC = 5.0


def parse_close_time_epoch(value: str | None) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _json_safe_detail(detail: dict[str, float | int | str | None]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in detail.items():
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            out[key] = None
        else:
            out[key] = value
    return out


def _build_base_snapshot(
    *,
    profile: MarketProfile,
    feed_asset: str,
    strike: float | None,
    market_ticker: str | None,
    spot: float | int | None,
    settlement_seconds: int,
) -> dict[str, Any]:
    return {
        "asset": profile.asset,
        "asset_display": profile.display_name,
        "feed_asset": feed_asset,
        "index_label": profile.index_label,
        "settlement_window_seconds": settlement_seconds,
        "seconds_to_expiry": None,
        "strike_usd": strike,
        "market_ticker": market_ticker,
        "spot_brti": float(spot) if isinstance(spot, (int, float)) else None,
        "spot_index": float(spot) if isinstance(spot, (int, float)) else None,
        "sigma_annual": None,
        "sigma_samples": 0,
        "vol_is_fallback": False,
        "p_model": None,
        "p_model_pct": None,
        "regime": None,
        "sigma_eff": None,
        "twap_seconds_elapsed": 0,
        "twap_partial_avg": None,
        "twap_required_avg": None,
        "pricer_detail": None,
        "ready": False,
        "reason": None,
    }


def _estimate_sigma(
    points: list[tuple[float, float]],
    *,
    fallback_sigma_annual: float,
    now_ts: float,
) -> tuple[float, bool]:
    sigma = realized_vol_from_price_points(
        points,
        window_seconds=_VOL_WINDOW_SEC,
        now_ts=now_ts,
        min_samples=8,
    )
    is_fallback = False

    if sigma is None:
        sigma = realized_vol_from_price_points(
            points,
            window_seconds=None,
            now_ts=now_ts,
            min_samples=5,
        )
    if sigma is None or sigma <= 0:
        sigma = float(fallback_sigma_annual)
        is_fallback = True

    return float(sigma), is_fallback


def compute_pricing_snapshot(
    *,
    profile: MarketProfile,
    feed_asset: str,
    spot: float | int | None,
    ticks: list[dict[str, Any]],
    strike: float | None,
    market_ticker: str | None,
    close_time_iso: str | None,
) -> dict[str, Any]:
    settlement_seconds = int(profile.settlement_window_seconds)
    close_ts = parse_close_time_epoch(close_time_iso)

    base = _build_base_snapshot(
        profile=profile,
        feed_asset=feed_asset,
        strike=strike,
        market_ticker=market_ticker,
        spot=spot,
        settlement_seconds=settlement_seconds,
    )

    if close_ts is None:
        base["reason"] = "no_close_time"
        return base
    if strike is None:
        base["reason"] = "no_strike"
        return base
    if feed_asset != profile.asset:
        base["reason"] = "asset_syncing"
        return base
    if not isinstance(spot, (int, float)) or float(spot) <= 0:
        base["reason"] = "no_brti"
        return base

    now_ts = time.time()
    sec_exp = max(0.0, float(close_ts) - now_ts)
    base["seconds_to_expiry"] = round(sec_exp, 2)

    points = extract_valid_index_points(ticks)
    sigma, vol_is_fallback = _estimate_sigma(
        points,
        fallback_sigma_annual=profile.fallback_sigma_annual,
        now_ts=now_ts,
    )

    base["sigma_annual"] = round(sigma, 6)
    base["vol_is_fallback"] = vol_is_fallback
    base["sigma_samples"] = len(points)

    twap_elapsed_seconds = 0
    twap_partial_avg: float | None = None
    twap_required_avg: float | None = None

    result: AsianBinaryPricerResult
    if sec_exp > settlement_seconds:
        result = prob_levy_tw_binary(
            float(spot),
            float(strike),
            sigma,
            sec_exp,
            n_fixes=settlement_seconds,
        )
    else:
        window_start_ts = float(close_ts) - settlement_seconds
        observed_end_ts = min(now_ts, float(close_ts))
        samples, twap_elapsed_seconds = reconstruct_discrete_forward_fill_samples(
            points,
            window_start_ts,
            observed_end_ts,
            max_staleness_sec=_MAX_SAMPLE_STALENESS_SEC,
        )

        sample_count = len(samples)
        known_mean = (sum(samples) / len(samples)) if samples else None

        result = prob_collapsed_variance_binary(
            float(strike),
            sigma,
            n=settlement_seconds,
            k=sample_count,
            mean_known_samples=known_mean,
            mu_fwd=float(spot),
        )

        if known_mean is not None:
            twap_partial_avg = round(known_mean, 2)

        if sample_count < settlement_seconds and samples:
            remaining = settlement_seconds - sample_count
            needed_sum = float(strike) * settlement_seconds - sum(samples)
            twap_required_avg = round(needed_sum / remaining, 2)

    base["p_model"] = round(result.p_model, 8)
    base["p_model_pct"] = round(100.0 * result.p_model, 4)
    base["regime"] = result.regime
    base["sigma_eff"] = None if result.sigma_eff is None else round(float(result.sigma_eff), 8)
    base["pricer_detail"] = _json_safe_detail(result.detail)
    base["twap_seconds_elapsed"] = twap_elapsed_seconds
    base["twap_partial_avg"] = twap_partial_avg
    base["twap_required_avg"] = twap_required_avg
    base["ready"] = True
    base["reason"] = None
    return base
