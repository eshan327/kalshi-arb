from __future__ import annotations

import math

TAKER_FEE_COEFF = 7.0


def _price_probability(price_cents: float) -> float:
    return max(0.01, min(99.99, float(price_cents))) / 100.0


def taker_fee_cents_per_contract(
    price_cents: float, count: int = 1, fee_multiplier: float = 1.0
) -> float:
    """Current general Kalshi taker formula, including whole-cent order rounding."""
    contracts = max(1, int(count))
    p = _price_probability(price_cents)
    raw_fee = (
        TAKER_FEE_COEFF
        * max(0.0, float(fee_multiplier))
        * contracts
        * p
        * (1.0 - p)
    )
    return math.ceil(raw_fee) / contracts


def kelly_fraction_binary(*, p_win: float, cost_cents: float) -> float:
    p = max(0.0, min(1.0, float(p_win)))
    c = max(0.01, min(99.99, float(cost_cents)))
    odds = (100.0 - c) / c
    return max(0.0, p - ((1.0 - p) / odds))


def quarter_kelly_fraction_binary(*, p_win: float, cost_cents: float) -> float:
    return 0.25 * kelly_fraction_binary(p_win=p_win, cost_cents=cost_cents)


def time_progress_multiplier(
    *, seconds_elapsed: float, window_seconds: float = 900.0
) -> float:
    window = max(1.0, float(window_seconds))
    return max(0.0, min(window, float(seconds_elapsed))) / window


def time_weighted_quarter_kelly_fraction_binary(
    *,
    p_win: float,
    cost_cents: float,
    seconds_elapsed: float,
    window_seconds: float = 900.0,
) -> float:
    return quarter_kelly_fraction_binary(
        p_win=p_win, cost_cents=cost_cents
    ) * time_progress_multiplier(
        seconds_elapsed=seconds_elapsed, window_seconds=window_seconds
    )


def expected_value_cents(
    *, p_win: float, price_cents: float, fee_multiplier: float = 1.0
) -> float:
    return (
        max(0.0, min(1.0, float(p_win))) * 100.0
        - float(price_cents)
        - taker_fee_cents_per_contract(price_cents, fee_multiplier=fee_multiplier)
    )
