from __future__ import annotations

import math
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from data.orderbook import OrderBook
from trading.fees import kelly_fraction_binary, taker_fee_cents_per_contract
from trading.settings import TradingSettings


@dataclass(frozen=True)
class TradeSignal:
    ts: float
    market_ticker: str
    side: Literal["yes", "no"]
    action: Literal["buy", "sell"]
    count: int
    quote_price_cents: float
    fair_price_cents: float
    credit_cents: float
    edge_cents: float
    model_probability: float
    reason: str
    diagnostics: dict[str, float | int | str | bool | None]


MAX_ORDERBOOK_AGE_SECONDS = 2.0


def _safe_float(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _best_quotes(
    book: OrderBook | None,
) -> tuple[float | None, float | None, float | None, float | None]:
    if book is None or not book.initialized or book.needs_resync:
        return None, None, None, None

    return book.get_best_prices()


def slipped_price_cents(
    price_cents: float,
    ticks: int,
    direction: str,
    price_ranges: list[dict[str, Any]] | None = None,
) -> float:
    price = round(float(price_cents), 4)
    steps = max(0, int(ticks))
    if steps == 0:
        return price

    levels: set[Decimal] = set()
    try:
        for price_range in price_ranges or []:
            current = Decimal(str(price_range["start"]))
            end = Decimal(str(price_range["end"]))
            step = Decimal(str(price_range["step"]))
            if step <= 0:
                continue
            while current <= end and len(levels) < 20_000:
                cents = current * 100
                if 0 < cents < 100:
                    levels.add(cents)
                current += step
    except (InvalidOperation, KeyError, TypeError, ValueError):
        levels.clear()

    sign = 1 if direction == "up" else -1
    if not levels:
        return round(max(0.01, min(99.99, price + sign * steps)), 4)
    ordered = sorted(float(level) for level in levels)
    candidates = (
        [level for level in ordered if level > price + 1e-8]
        if sign > 0
        else [level for level in reversed(ordered) if level < price - 1e-8]
    )
    return (
        round(candidates[min(steps - 1, len(candidates) - 1)], 4)
        if candidates
        else price
    )


def _kelly_target_contracts(
    *,
    p_win: float,
    quote_price_cents: float,
    bankroll_cents: int,
    max_position_usd: float,
    max_position_fraction: float,
    kelly_scale: float,
    fee_cents: float,
) -> tuple[int, float, float, float]:
    all_in_cost = max(0.01, float(quote_price_cents) + max(0.0, float(fee_cents)))
    scaled_kelly_fraction = max(
        0.0, min(1.0, float(kelly_scale))
    ) * kelly_fraction_binary(
        p_win=float(p_win),
        cost_cents=all_in_cost,
    )

    bankroll = float(max(0, int(bankroll_cents)))
    cap_by_kelly = bankroll * scaled_kelly_fraction
    cap_by_bankroll = bankroll * max(0.0, float(max_position_fraction))
    cap_by_fixed = max(0.0, float(max_position_usd)) * 100.0
    notional_cap_cents = max(0.0, min(cap_by_kelly, cap_by_bankroll, cap_by_fixed))
    target_contracts = max(0, int(notional_cap_cents // all_in_cost))
    return target_contracts, scaled_kelly_fraction, all_in_cost, notional_cap_cents


def build_trade_signal(
    *,
    pricing: dict[str, Any],
    market_ticker: str,
    book: OrderBook | None,
    settings: TradingSettings,
    bankroll_cents: int,
    open_yes_contracts: int = 0,
    open_no_contracts: int = 0,
    open_yes_avg_entry_cents: float | None = None,
    open_no_avg_entry_cents: float | None = None,
    available_cash_cents: int | None = None,
    fee_multiplier: float | None = None,
    fee_type: str | None = None,
    price_ranges: list[dict[str, Any]] | None = None,
    now_ts: float | None = None,
) -> tuple[TradeSignal | None, str, dict[str, Any]]:
    """Compare the unchanged forecast to IOC limits, then cap risk and cash exposure."""
    now = time.time() if now_ts is None else float(now_ts)
    diagnostics: dict[str, Any] = {}
    if not pricing.get("ready"):
        return None, "pricing_not_ready", diagnostics
    p = _safe_float(pricing.get("p_model"))
    if p is None or not 0 < p < 1:
        return None, "invalid_model_probability", diagnostics
    if book is None or book.market_ticker != market_ticker:
        return None, "orderbook_market_mismatch", diagnostics
    yes_bid, yes_ask, no_bid, no_ask = _best_quotes(book)
    diagnostics.update(
        yes_bid_cents=yes_bid,
        yes_ask_cents=yes_ask,
        no_bid_cents=no_bid,
        no_ask_cents=no_ask,
    )
    if any(quote is None for quote in (yes_bid, yes_ask, no_bid, no_ask)):
        return None, "missing_best_quotes", diagnostics
    updated = book.last_verified_ts or book.last_update_ts
    if updated is None or not 0 <= now - updated <= MAX_ORDERBOOK_AGE_SECONDS:
        return None, "stale_orderbook", diagnostics
    if (
        fee_multiplier is None
        or not math.isfinite(fee_multiplier)
        or fee_multiplier <= 0
        or fee_type not in {"quadratic", "quadratic_with_maker_fees"}
    ):
        return None, "fee_policy_unavailable", diagnostics
    diagnostics.update(
        fee_multiplier=fee_multiplier,
        fee_type=fee_type,
        market_probability=(yes_bid + yes_ask) / 200,
        required_taker_edge_cents=settings.min_edge_cents,
        entry_cutoff_seconds_to_expiry=settings.entry_cutoff_seconds_to_expiry,
    )
    yes_bids, yes_asks, no_bids, no_asks = book.get_orderbook_top_n(1)
    sides = (
        (
            "yes",
            p,
            yes_bid,
            yes_ask,
            open_yes_contracts,
            open_yes_avg_entry_cents,
            yes_bids,
            yes_asks,
        ),
        (
            "no",
            1 - p,
            no_bid,
            no_ask,
            open_no_contracts,
            open_no_avg_entry_cents,
            no_bids,
            no_asks,
        ),
    )

    def signal(side, action, count, price, fair, fee, reason):
        credit = fair - price if action == "buy" else price - fair
        diagnostics["credit_cents"] = round(credit, 6)
        result = TradeSignal(
            ts=now,
            market_ticker=market_ticker,
            side=side,
            action=action,
            count=count,
            quote_price_cents=price,
            fair_price_cents=fair,
            credit_cents=round(credit, 6),
            edge_cents=round(credit - fee, 6),
            model_probability=p,
            reason=reason,
            diagnostics=diagnostics,
        )
        return result, reason, diagnostics

    # Exits reduce inventory and remain available inside the entry cutoff.
    for side, probability, bid, ask, current, avg, bids, asks in sides:
        price = slipped_price_cents(bid, settings.slippage_ticks, "down", price_ranges)
        fee = taker_fee_cents_per_contract(
            price, fee_multiplier=fee_multiplier, action="sell"
        )
        count = min(max(0, current), settings.max_order_contracts, int(bids[0][1]))
        if count > 0 and price - fee - probability * 100 >= settings.min_edge_cents:
            return signal(
                side,
                "sell",
                count,
                price,
                probability * 100,
                fee,
                f"edge_reversal_exit_{side}",
            )

    seconds = _safe_float(pricing.get("seconds_to_expiry"))
    if (
        seconds is None
        or not math.isfinite(seconds)
        or seconds <= 0
        or seconds < settings.entry_cutoff_seconds_to_expiry
    ):
        return None, "entry_cutoff", diagnostics
    if available_cash_cents is None or available_cash_cents < 0:
        return None, "cash_unavailable", diagnostics

    candidates = []
    for side, probability, bid, ask, current, avg, bids, asks in sides:
        limit = slipped_price_cents(ask, settings.slippage_ticks, "up", price_ranges)
        fee = taker_fee_cents_per_contract(limit, fee_multiplier=fee_multiplier)
        edge = probability * 100 - limit - fee
        diagnostics[f"edge_{side}_cents"] = round(edge, 6)
        candidates.append(
            (edge, side, probability, limit, fee, current, avg, int(asks[0][1]))
        )
    edge, side, probability, price, fee, current, avg, depth = max(
        candidates, key=lambda c: c[0]
    )
    if edge < settings.min_edge_cents:
        return None, "edge_below_threshold", diagnostics
    # A single outcome-side position keeps live netting and paper accounting aligned.
    if (side == "yes" and open_no_contracts) or (side == "no" and open_yes_contracts):
        return None, "opposite_position_open", diagnostics
    target, _, cost, cap = _kelly_target_contracts(
        p_win=probability,
        quote_price_cents=price,
        bankroll_cents=bankroll_cents,
        max_position_usd=settings.max_position_usd,
        max_position_fraction=settings.max_position_fraction,
        kelly_scale=settings.kelly_fraction,
        fee_cents=fee,
    )
    if current and (avg is None or not math.isfinite(avg) or avg <= 0):
        return None, "position_cost_unavailable", diagnostics
    headroom = max(0.0, cap - current * (avg or 0))
    by_position, by_cash = int(headroom // cost), int(available_cash_cents // cost)
    count = min(
        settings.max_order_contracts,
        max(0, target - current),
        by_position,
        by_cash,
        depth,
    )
    diagnostics.update(
        kelly_target_contracts=target,
        position_notional_cap_cents=cap,
        all_in_cost_cents=cost,
        max_by_top_of_book=depth,
    )
    if count <= 0:
        reason = (
            "position_notional_cap_reached"
            if by_position <= 0
            else "insufficient_available_cash"
            if by_cash <= 0
            else "at_target_allocation"
        )
        return None, reason, diagnostics
    return signal(side, "buy", count, price, probability * 100, fee, "ev_signal_ready")
