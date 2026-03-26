from core.config import RECONCILIATION_PRICE_TOL_CENTS, RECONCILIATION_QTY_TOL


def compare_levels(live_levels, rest_levels, top_n):
    """Compares top-N orderbook levels between live and REST snapshots."""
    live = live_levels[:top_n]
    rest = rest_levels[:top_n]

    live_map = {px: qty for px, qty in live}
    rest_map = {px: qty for px, qty in rest}

    live_prices = set(live_map.keys())
    rest_prices = set(rest_map.keys())
    common_prices = live_prices & rest_prices

    missing_from_live = len(rest_prices - live_prices)
    extra_in_live = len(live_prices - rest_prices)

    qty_mismatch_count = 0
    for px in common_prices:
        if abs(live_map[px] - rest_map[px]) > RECONCILIATION_QTY_TOL:
            qty_mismatch_count += 1

    best_price_delta = 0.0
    if live and rest:
        best_price_delta = abs(live[0][0] - rest[0][0])

    return {
        "missing_from_live": missing_from_live,
        "extra_in_live": extra_in_live,
        "qty_mismatch_count": qty_mismatch_count,
        "best_price_delta": best_price_delta,
    }


def is_reconciliation_breach(metrics):
    """Determines whether reconciliation drift exceeds configured thresholds."""
    if metrics["yes"]["best_price_delta"] > RECONCILIATION_PRICE_TOL_CENTS:
        return True
    if metrics["no"]["best_price_delta"] > RECONCILIATION_PRICE_TOL_CENTS:
        return True

    combined_missing = metrics["yes"]["missing_from_live"] + metrics["no"]["missing_from_live"]
    combined_extra = metrics["yes"]["extra_in_live"] + metrics["no"]["extra_in_live"]
    combined_qty_mismatch = metrics["yes"]["qty_mismatch_count"] + metrics["no"]["qty_mismatch_count"]

    return (combined_missing + combined_extra + combined_qty_mismatch) > 0
