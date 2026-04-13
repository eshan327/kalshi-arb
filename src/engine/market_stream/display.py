from __future__ import annotations

MIN_ACTIONABLE_PRICE_CENTS = 1.0
MAX_ACTIONABLE_PRICE_CENTS = 99.0


def is_actionable_display_level(level: tuple[float, float]) -> bool:
    price, _qty = level
    return MIN_ACTIONABLE_PRICE_CENTS <= float(price) <= MAX_ACTIONABLE_PRICE_CENTS


def top_levels_for_display(levels: list[tuple[float, float]], depth: int) -> list[tuple[float, float]]:
    """Prefer 1-99c levels for UI display with fallback when those are unavailable."""
    if depth <= 0:
        return []

    filtered = [level for level in levels if is_actionable_display_level(level)]
    selected = filtered if filtered else levels
    return selected[:depth]
