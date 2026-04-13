"""
Realized volatility from BRTI (or any spot) time series for the Asian / collapsed-variance pricer.

Uses log-return standard deviation scaled to an annualized σ. BRTI prints ~1 Hz from the
aggregator; effective sample rate is passed explicitly when it differs.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

SECONDS_PER_YEAR = 365.25 * 24 * 3600.0


def _clean_prices(values: Sequence[float | int]) -> list[float]:
    out: list[float] = []
    for v in values:
        try:
            x = float(v)
        except (TypeError, ValueError):
            continue
        if x > 0 and math.isfinite(x):
            out.append(x)
    return out


def realized_vol_log_returns(
    prices: Sequence[float | int],
    *,
    samples_per_second: float,
    annualize: bool = True,
    min_returns: int = 2,
) -> float | None:
    """
    Sample standard deviation of log returns, optionally annualized.

    σ_annual ≈ stdev(ln(P_t/P_{t-1})) * sqrt(samples_per_second * SECONDS_PER_YEAR)

    For ~1 Hz BRTI, use samples_per_second=1.0 (or the observed fill rate).
    """
    series = _clean_prices(prices)
    if len(series) < min_returns + 1:
        return None

    log_rets: list[float] = []
    for a, b in zip(series, series[1:], strict=False):
        log_rets.append(math.log(b / a))

    if len(log_rets) < min_returns:
        return None

    m = sum(log_rets) / len(log_rets)
    var = sum((r - m) ** 2 for r in log_rets) / max(len(log_rets) - 1, 1)
    if var <= 0 or not math.isfinite(var):
        return None

    sigma_period = math.sqrt(var)
    if not annualize:
        return sigma_period

    hz = max(samples_per_second, 1e-9)
    return sigma_period * math.sqrt(hz * SECONDS_PER_YEAR)


def realized_vol_from_price_points(
    points: Iterable[tuple[float, float]],
    *,
    window_seconds: float | None = None,
    now_ts: float | None = None,
    min_samples: int = 5,
) -> float | None:
    """
    Computes annualized realized volatility from pre-extracted (ts, price) pairs.

    This avoids repeated tick-dict parsing when callers already hold cleaned points.
    """
    import time

    if now_ts is None:
        now_ts = time.time()

    cutoff = (now_ts - window_seconds) if window_seconds is not None else None
    prices: list[float] = []
    min_ts: float | None = None
    max_ts: float | None = None

    for ts, price in points:
        if cutoff is not None and ts < cutoff:
            continue

        px = float(price)
        if px <= 0 or not math.isfinite(px):
            continue

        prices.append(px)
        if min_ts is None or ts < min_ts:
            min_ts = ts
        if max_ts is None or ts > max_ts:
            max_ts = ts

    if len(prices) < min_samples:
        return None

    samples_per_second = 1.0
    if len(prices) >= 2 and min_ts is not None and max_ts is not None:
        span = max(max_ts - min_ts, 1e-3)
        samples_per_second = max((len(prices) - 1) / span, 1e-3)

    return realized_vol_log_returns(prices, samples_per_second=samples_per_second, annualize=True)
