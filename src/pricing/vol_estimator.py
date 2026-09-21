"""Annualized sample volatility of consecutive official one-second log returns."""

from __future__ import annotations

import math
from collections.abc import Iterable
from statistics import stdev

from pricing.asian_pricer import SECONDS_PER_YEAR

BASELINE_VOL_WINDOW_SECONDS = 300.0


def realized_vol_from_price_points(
    points: Iterable[tuple[float, float]],
    *,
    window_seconds: float = BASELINE_VOL_WINDOW_SECONDS,
    now_ts: float,
) -> float | None:
    """Require the complete fixing grid; never bridge gaps or include future/5 Hz ticks."""
    if (
        not math.isfinite(now_ts)
        or not math.isfinite(window_seconds)
        or window_seconds < 2
    ):
        raise ValueError(
            "Finite clock and a volatility window of at least two seconds required"
        )
    first, last = math.ceil(now_ts - window_seconds), math.floor(now_ts)
    values = {}
    for ts, price in points:
        if (
            not math.isfinite(ts)
            or not first <= ts <= last
            or not float(ts).is_integer()
        ):
            continue
        if not math.isfinite(price) or price <= 0:
            raise ValueError("Invalid official fixing price")
        if ts in values and values[ts] != price:
            raise ValueError("Conflicting official fixing prices")
        values[ts] = price
    if len(values) != last - first + 1:
        return None
    returns = [math.log(values[t] / values[t - 1]) for t in range(first + 1, last + 1)]
    return stdev(returns) * math.sqrt(SECONDS_PER_YEAR)
