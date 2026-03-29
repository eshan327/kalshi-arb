"""Live TWAP + vol + Asian pricer snapshot for the Flask API."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

from engine.asian_pricer import AsianBinaryPricerResult, price_btwap_binary
from engine.twap import TwapCalculator
from engine.vol_estimator import realized_vol_from_brti_ticks
from feeds.brti_state import get_brti_state, get_brti_ticks

logger = logging.getLogger(__name__)

_VOL_WINDOW_SEC = 300.0
_BRTI_TICK_LOOKBACK = 4000
_FALLBACK_SIGMA_ANNUAL = 0.55

# Session key must uniquely identify each 15m contract (ticker+strike alone can repeat or be None).
_twap_key: tuple[str, float, str] | None = None
_twap: TwapCalculator | None = None


def _parse_close_time_epoch(value: str | None) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _ensure_twap(
    strike: float,
    market_ticker: str | None,
    close_time_iso: str | None,
) -> TwapCalculator:
    global _twap, _twap_key
    # close_time distinguishes back-to-back 15m windows with the same strike; ticker fallback when API omits it.
    key = (market_ticker or "", float(strike), close_time_iso or "")
    if _twap is None or _twap_key != key:
        _twap = TwapCalculator(strike_price=strike)
        _twap_key = key
        logger.info(
            "TwapCalculator bound to market=%s strike=%s close=%s",
            market_ticker,
            strike,
            close_time_iso,
        )
    return _twap


def reset_live_pricing_for_new_market() -> None:
    """Drop TWAP session when the streamer switches to the next KXBTC15M contract."""
    global _twap, _twap_key
    _twap = None
    _twap_key = None


def _json_safe_detail(detail: dict[str, float | int | str | None]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in detail.items():
        if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
            out[k] = None
        else:
            out[k] = v
    return out


def compute_live_pricing_snapshot(
    *,
    strike: float | None,
    market_ticker: str | None,
    close_time_iso: str | None,
) -> dict[str, Any]:
    brti_state = get_brti_state()
    spot = brti_state.get("brti")
    close_ts = _parse_close_time_epoch(close_time_iso)

    base: dict[str, Any] = {
        "seconds_to_expiry": None,
        "strike_usd": strike,
        "market_ticker": market_ticker,
        "spot_brti": float(spot) if isinstance(spot, (int, float)) else None,
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

    if close_ts is None:
        base["reason"] = "no_close_time"
        return base
    if strike is None:
        base["reason"] = "no_strike"
        return base
    if not isinstance(spot, (int, float)) or float(spot) <= 0:
        base["reason"] = "no_brti"
        return base

    sec_exp = max(0.0, float(close_ts) - time.time())
    base["seconds_to_expiry"] = round(sec_exp, 2)

    twap = _ensure_twap(float(strike), market_ticker, close_time_iso)

    if 0 < sec_exp <= 60:
        if not twap.settlement_window_started():
            twap.start_window()
        twap.add_price_tick(float(spot))

    ticks = get_brti_ticks(limit=_BRTI_TICK_LOOKBACK)
    sigma = realized_vol_from_brti_ticks(
        ticks,
        window_seconds=_VOL_WINDOW_SEC,
        min_samples=8,
    )
    vol_fallback = False
    if sigma is None:
        sigma = realized_vol_from_brti_ticks(ticks, window_seconds=None, min_samples=5)
    if sigma is None or sigma <= 0:
        sigma = _FALLBACK_SIGMA_ANNUAL
        vol_fallback = True

    base["sigma_annual"] = round(float(sigma), 6)
    base["vol_is_fallback"] = vol_fallback
    base["sigma_samples"] = len(
        [t for t in ticks if isinstance(t, dict) and t.get("status") == "ok" and t.get("brti") is not None]
    )

    twap_for_price: TwapCalculator | None = twap if sec_exp <= 60 else None
    result: AsianBinaryPricerResult = price_btwap_binary(
        float(spot),
        float(strike),
        float(sigma),
        sec_exp,
        twap_for_price,
        mu_fwd=float(spot),
    )

    base["p_model"] = round(result.p_model, 8)
    base["p_model_pct"] = round(100.0 * result.p_model, 4)
    base["regime"] = result.regime
    base["sigma_eff"] = None if result.sigma_eff is None else round(float(result.sigma_eff), 8)
    base["pricer_detail"] = _json_safe_detail(result.detail)
    base["twap_seconds_elapsed"] = twap.seconds_elapsed() if sec_exp <= 60 else 0
    base["twap_partial_avg"] = (
        round(twap.current_average(), 2)
        if sec_exp <= 60 and twap.settlement_window_started()
        else None
    )
    req = twap.required_average()
    base["twap_required_avg"] = round(req, 2) if req is not None else None
    base["ready"] = True
    base["reason"] = None
    return base
