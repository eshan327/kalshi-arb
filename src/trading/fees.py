from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

TAKER_FEE_COEFF = 7.0


def taker_fee_cents_per_contract(
    price_cents: float,
    count: int = 1,
    fee_multiplier: float = 1.0,
    action: str = "buy",
) -> float:
    """Kalshi taker fee plus the order's cent-alignment rounding."""
    if action not in {"buy", "sell"}:
        raise ValueError("action must be buy or sell")
    contracts = max(1, int(count))
    price = Decimal(str(max(0.01, min(99.99, float(price_cents)))))
    p = price / Decimal("100")
    trade_fee = (
        Decimal(str(TAKER_FEE_COEFF))
        * Decimal(str(max(0.0, float(fee_multiplier))))
        * contracts
        * p
        * (1 - p)
    ).quantize(Decimal("0.01"), rounding=ROUND_CEILING)
    notional = price * contracts
    effective_fee = (
        (notional + trade_fee).quantize(Decimal("1"), rounding=ROUND_CEILING) - notional
        if action == "buy"
        else notional
        - (notional - trade_fee).quantize(Decimal("1"), rounding=ROUND_FLOOR)
    )
    return float(effective_fee / contracts)


def kelly_fraction_binary(*, p_win: float, cost_cents: float) -> float:
    p = max(0.0, min(1.0, float(p_win)))
    c = max(0.01, min(99.99, float(cost_cents)))
    odds = (100.0 - c) / c
    return max(0.0, p - ((1.0 - p) / odds))
