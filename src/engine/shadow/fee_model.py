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
