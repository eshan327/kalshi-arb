from __future__ import annotations


def _clip_price(price_cents: float) -> float:
    return max(1.0, min(99.0, float(price_cents)))


def taker_fee_cents_per_contract(price_cents: float, curve_coeff: float) -> float:
    """
    Smooth convex fee approximation in cents per contract.

    The curve keeps fees low near tails and highest near 50c, matching taker-cost shape.
    """
    px = _clip_price(price_cents) / 100.0
    coeff = max(0.0, float(curve_coeff))
    return coeff * px * (1.0 - px)


def kelly_fraction_binary(*, p_win: float, cost_cents: float) -> float:
    """Full Kelly fraction for a binary contract that costs c and pays 100 on win.

    b = (100 - c) / c
    f* = p - (1 - p) / b
    """
    p = max(0.0, min(1.0, float(p_win)))
    c = _clip_price(float(cost_cents))
    b = (100.0 - c) / c
    if b <= 0.0:
        return 0.0
    q = 1.0 - p
    frac = p - (q / b)
    return max(0.0, float(frac))


def quarter_kelly_fraction_binary(*, p_win: float, cost_cents: float) -> float:
    return 0.25 * kelly_fraction_binary(p_win=p_win, cost_cents=cost_cents)


def expected_value_yes_cents(
    *,
    p_model: float,
    ask_price_cents: float,
    fee_curve_coeff: float,
) -> float:
    payout_expectation = max(0.0, min(1.0, float(p_model))) * 100.0
    fee = taker_fee_cents_per_contract(ask_price_cents, fee_curve_coeff)
    return payout_expectation - float(ask_price_cents) - fee


def expected_value_no_cents(
    *,
    p_model: float,
    ask_price_cents: float,
    fee_curve_coeff: float,
) -> float:
    payout_expectation = max(0.0, min(1.0, 1.0 - float(p_model))) * 100.0
    fee = taker_fee_cents_per_contract(ask_price_cents, fee_curve_coeff)
    return payout_expectation - float(ask_price_cents) - fee
