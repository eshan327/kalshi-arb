from __future__ import annotations

import math


def extract_suggested_strike(market_info: dict) -> float | None:
    """Read structured exchange terms; missing terms must not become guessed strikes."""
    if not market_info:
        return None

    direct_keys = [
        "strike_price",
        "strike",
        "target_price",
        "floor_strike",
        "cap_strike",
    ]
    for key in direct_keys:
        value = market_info.get(key)
        try:
            number = float(value)
            if math.isfinite(number) and number > 0:
                return number
        except (TypeError, ValueError, OverflowError):
            pass

    return None


def extract_settlement_decimals(market_info: dict, fallback: int) -> int:
    custom_strike = market_info.get("custom_strike") if market_info else None
    value = (
        custom_strike.get("round_digits") if isinstance(custom_strike, dict) else None
    )
    try:
        return max(0, min(12, int(value)))
    except (TypeError, ValueError, OverflowError):
        return fallback
