from __future__ import annotations

import math
import time
from typing import Any

from core.market_profiles import MarketProfile
from engine.asian_pricer import (
    AsianBinaryPricerResult,
    prob_collapsed_variance_binary,
    prob_levy_tw_binary,
)
from engine.market_stream.discovery import parse_iso8601_to_epoch
from engine.settlement_sampling import (
    extract_valid_index_points,
    reconstruct_discrete_forward_fill_samples,
)
from engine.vol_estimator import realized_vol_from_price_points

_VOL_WINDOW_SEC = 300.0
_MAX_SAMPLE_STALENESS_SEC = 5.0
_MIN_INDEX_EXCHANGES = 2
_MIN_ANCHOR_SAMPLES = 55
_MARKET_INTERVAL_SECONDS = 15 * 60


def _json_safe_detail(detail: dict[str, float | int | str | None]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in detail.items():
        if isinstance(value, float) and not math.isfinite(value):
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
    spot: float | None,
    settlement_seconds: int,
    settlement_decimals: int,
    source_exchanges: int,
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
        "spot_index_raw": float(spot) if isinstance(spot, (int, float)) else None,
        "spot_index": None,
        "source_exchanges": source_exchanges,
        "settlement_decimals": settlement_decimals,
        "model_strike_usd": None,
        "rounding_half_unit": None,
        "proxy_reference_avg": None,
        "proxy_basis_adjustment": None,
        "proxy_anchor_samples": 0,
        "proxy_anchor_ready": False,
        "sigma_annual": None,
        "sigma_samples": 0,
        "index_age_seconds": None,
        "vol_is_fallback": False,
        "p_model": None,
        "p_model_pct": None,
        "regime": None,
        "sigma_eff": None,
        "twap_seconds_elapsed": 0,
        "twap_samples_observed": 0,
        "twap_partial_avg": None,
        "twap_partial_avg_raw": None,
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
    spot: float | None,
    ticks: list[dict[str, Any]],
    strike: float | None,
    market_ticker: str | None,
    close_time_iso: str | None,
    settlement_decimals: int | None = None,
    source_exchanges: int = 0,
) -> dict[str, Any]:
    settlement_seconds = int(profile.settlement_window_seconds)
    settlement_decimals = max(
        0,
        min(
            12,
            int(
                profile.settlement_decimals_fallback
                if settlement_decimals is None
                else settlement_decimals
            ),
        ),
    )
    source_exchanges = max(0, int(source_exchanges))
    close_ts = parse_iso8601_to_epoch(close_time_iso)

    base = _build_base_snapshot(
        profile=profile,
        feed_asset=feed_asset,
        strike=strike,
        market_ticker=market_ticker,
        spot=spot,
        settlement_seconds=settlement_seconds,
        settlement_decimals=settlement_decimals,
        source_exchanges=source_exchanges,
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
    if source_exchanges < _MIN_INDEX_EXCHANGES:
        base["reason"] = "insufficient_index_sources"
        return base

    now_ts = time.time()
    sec_exp = max(0.0, float(close_ts) - now_ts)
    base["seconds_to_expiry"] = round(sec_exp, 2)

    points = extract_valid_index_points(ticks)
    if not points:
        base["reason"] = "no_index_ticks"
        return base
    index_age_seconds = max(0.0, now_ts - points[-1][0])
    base["index_age_seconds"] = round(index_age_seconds, 3)
    if index_age_seconds > _MAX_SAMPLE_STALENESS_SEC:
        base["reason"] = "stale_index"
        return base

    anchor_samples, anchor_elapsed = reconstruct_discrete_forward_fill_samples(
        points,
        float(close_ts) - _MARKET_INTERVAL_SECONDS - settlement_seconds,
        float(close_ts) - _MARKET_INTERVAL_SECONDS,
        max_staleness_sec=_MAX_SAMPLE_STALENESS_SEC,
    )
    base["proxy_anchor_samples"] = len(anchor_samples)
    if (
        anchor_elapsed != settlement_seconds
        or len(anchor_samples) < _MIN_ANCHOR_SAMPLES
    ):
        base["reason"] = "proxy_anchor_unavailable"
        return base

    proxy_reference_avg = sum(anchor_samples) / len(anchor_samples)
    proxy_basis_adjustment = float(strike) - proxy_reference_avg
    adjusted_spot = float(spot) + proxy_basis_adjustment
    adjusted_points = [
        (ts, value + proxy_basis_adjustment)
        for ts, value in points
        if value + proxy_basis_adjustment > 0
    ]
    if adjusted_spot <= 0 or not adjusted_points:
        base["reason"] = "invalid_proxy_anchor"
        return base

    rounding_half_unit = 0.5 * (10.0 ** (-settlement_decimals))
    model_strike = float(strike) - rounding_half_unit
    base.update(
        {
            "spot_index": adjusted_spot,
            "model_strike_usd": model_strike,
            "rounding_half_unit": rounding_half_unit,
            "proxy_reference_avg": round(proxy_reference_avg, settlement_decimals + 2),
            "proxy_basis_adjustment": round(
                proxy_basis_adjustment, settlement_decimals + 2
            ),
            "proxy_anchor_ready": True,
        }
    )

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
            adjusted_spot,
            model_strike,
            sigma,
            sec_exp,
            n_fixes=settlement_seconds,
        )
    else:
        window_start_ts = float(close_ts) - settlement_seconds
        observed_end_ts = min(now_ts, float(close_ts))
        samples, twap_elapsed_seconds = reconstruct_discrete_forward_fill_samples(
            adjusted_points,
            window_start_ts,
            observed_end_ts,
            max_staleness_sec=_MAX_SAMPLE_STALENESS_SEC,
        )

        sample_count = len(samples)
        known_mean = (sum(samples) / len(samples)) if samples else None

        result = prob_collapsed_variance_binary(
            model_strike,
            sigma,
            n=settlement_seconds,
            k=sample_count,
            mean_known_samples=known_mean,
            mu_fwd=adjusted_spot,
        )

        if known_mean is not None:
            twap_partial_avg = round(known_mean, settlement_decimals)
            base["twap_partial_avg_raw"] = known_mean

        if sample_count < settlement_seconds and samples:
            remaining = settlement_seconds - sample_count
            needed_sum = model_strike * settlement_seconds - sum(samples)
            twap_required_avg = round(needed_sum / remaining, settlement_decimals)

    base["p_model"] = round(result.p_model, 8)
    base["p_model_pct"] = round(100.0 * result.p_model, 4)
    base["regime"] = result.regime
    base["sigma_eff"] = (
        None if result.sigma_eff is None else round(float(result.sigma_eff), 8)
    )
    base["pricer_detail"] = _json_safe_detail(result.detail)
    base["twap_seconds_elapsed"] = twap_elapsed_seconds
    base["twap_samples_observed"] = len(samples) if sec_exp <= settlement_seconds else 0
    base["twap_partial_avg"] = twap_partial_avg
    base["twap_required_avg"] = twap_required_avg
    base["ready"] = True
    base["reason"] = None
    return base
