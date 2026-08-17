from __future__ import annotations

import time
from decimal import Decimal, InvalidOperation
from typing import Any

from engine.asian_pricer import prob_collapsed_variance_binary, prob_levy_tw_binary
from engine.book_microstructure import get_last_p_book_snapshot
from engine.orderbook import OrderBook
from engine.trading.fees import (
    expected_value_cents,
    quarter_kelly_fraction_binary,
    taker_fee_cents_per_contract,
)
from engine.trading.models import TradeSignal
from engine.trading.settings import TradingSettings

PROBABILITY_LOWER_BOUND = 0.20
PROBABILITY_UPPER_BOUND = 0.80
MAX_POSITION_USD_HARD_CAP = 50.0
TECHNICAL_WARMUP_SECONDS = 30.0
ENTRY_CUTOFF_SECONDS_TO_EXPIRY = 20.0
MAX_ORDERBOOK_AGE_SECONDS = 2.0
MARGINAL_CREDIT_STEP_CENTS = 0.5


def _safe_float(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _best_quotes(
    book: OrderBook | None,
) -> tuple[float | None, float | None, float | None, float | None]:
    if book is None or not book.initialized:
        return None, None, None, None

    yes_bid, yes_ask, no_bid, no_ask = book.get_best_prices()

    return (
        round(float(yes_bid), 4) if isinstance(yes_bid, (int, float)) else None,
        round(float(yes_ask), 4) if isinstance(yes_ask, (int, float)) else None,
        round(float(no_bid), 4) if isinstance(no_bid, (int, float)) else None,
        round(float(no_ask), 4) if isinstance(no_ask, (int, float)) else None,
    )


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
    fee_cents: float,
) -> tuple[int, float, float]:
    all_in_cost = max(0.01, float(quote_price_cents) + max(0.0, float(fee_cents)))
    kelly_fraction_quarter = quarter_kelly_fraction_binary(
        p_win=float(p_win),
        cost_cents=all_in_cost,
    )

    cap_by_kelly = float(max(0, int(bankroll_cents))) * kelly_fraction_quarter
    cap_by_fixed = min(float(max_position_usd), MAX_POSITION_USD_HARD_CAP) * 100.0
    notional_cap_cents = max(0.0, min(cap_by_kelly, cap_by_fixed))
    target_contracts = max(0, int(notional_cap_cents // all_in_cost))
    return target_contracts, kelly_fraction_quarter, all_in_cost


def _credit_target_contracts(
    *, credit_cents: float, minimum_credit_cents: float
) -> int:
    if credit_cents < minimum_credit_cents:
        return 0
    return 1 + int(
        (float(credit_cents) - float(minimum_credit_cents))
        // MARGINAL_CREDIT_STEP_CENTS
    )


def _build_sell_signal(
    *,
    ts: float,
    market_ticker: str,
    side: str,
    count: int,
    quote_price_cents: float,
    fair_price_cents: float,
    p_model: float,
    reason: str,
    diagnostics: dict[str, Any],
) -> TradeSignal:
    normalized_side = "yes" if str(side).strip().lower() == "yes" else "no"
    model_side_probability = (
        float(p_model) if normalized_side == "yes" else (1.0 - float(p_model))
    )
    implied_probability = float(quote_price_cents) / 100.0

    return TradeSignal(
        ts=float(ts),
        market_ticker=str(market_ticker),
        side=normalized_side,
        action="sell",
        count=max(1, int(count)),
        quote_price_cents=round(float(quote_price_cents), 4),
        fair_price_cents=round(float(fair_price_cents), 6),
        credit_cents=round(float(fair_price_cents) - float(quote_price_cents), 6),
        edge_cents=round(float(fair_price_cents) - float(quote_price_cents), 6),
        edge_probability=round(float(model_side_probability - implied_probability), 8),
        confidence=round(abs(float(p_model) - 0.5), 8),
        model_probability=round(float(p_model), 8),
        market_implied_probability=round(float(implied_probability), 8),
        reason=str(reason),
        diagnostics=diagnostics,
    )


def apply_pricing_overrides(
    pricing: dict[str, Any], settings: TradingSettings
) -> dict[str, Any]:
    """Optionally re-runs pricer with volatility override and responsiveness scaling."""
    out = dict(pricing) if isinstance(pricing, dict) else {}
    if not bool(out.get("ready")):
        return out

    spot = _safe_float(out.get("spot_index"))
    strike = _safe_float(out.get("model_strike_usd"))
    if strike is None:
        strike = _safe_float(out.get("strike_usd"))
    sec_exp = _safe_float(out.get("seconds_to_expiry"))
    base_sigma = _safe_float(out.get("sigma_annual"))
    settlement_window = int(_safe_float(out.get("settlement_window_seconds")) or 60)

    if spot is None or strike is None or sec_exp is None or base_sigma is None:
        return out

    sigma = (
        settings.volatility_override
        if isinstance(settings.volatility_override, float)
        else base_sigma
    )
    sigma = max(0.01, float(sigma) * float(settings.volatility_scale))

    if sec_exp > float(settlement_window):
        result = prob_levy_tw_binary(
            S0=spot,
            strike=strike,
            sigma_annual=sigma,
            seconds_to_expiry=sec_exp,
            n_fixes=settlement_window,
        )
    else:
        k = max(0, int(_safe_float(out.get("twap_samples_observed")) or 0))
        mean_known = _safe_float(out.get("twap_partial_avg_raw"))
        if mean_known is None:
            mean_known = _safe_float(out.get("twap_partial_avg"))
        result = prob_collapsed_variance_binary(
            strike=strike,
            sigma_annual=sigma,
            n=settlement_window,
            k=k,
            mean_known_samples=mean_known,
            mu_fwd=spot,
        )

    out["p_model_base"] = out.get("p_model")
    out["p_model"] = float(result.p_model)
    out["p_model_pct"] = round(float(result.p_model) * 100.0, 4)
    out["sigma_override_applied"] = round(float(sigma), 6)
    if settings.volatility_override is not None:
        out["vol_is_fallback_base"] = out.get("vol_is_fallback")
        out["vol_is_fallback"] = False
    out["regime"] = result.regime
    return out


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
    runtime_uptime_seconds: float | None = None,
    available_cash_cents: int | None = None,
    fee_multiplier: float | None = 1.0,
    fee_type: str | None = "quadratic",
    price_ranges: list[dict[str, Any]] | None = None,
    now_ts: float | None = None,
) -> tuple[TradeSignal | None, str, dict[str, Any]]:
    ts = time.time() if now_ts is None else float(now_ts)
    diagnostics: dict[str, Any] = {}

    if isinstance(runtime_uptime_seconds, (int, float)):
        uptime = max(0.0, float(runtime_uptime_seconds))
        diagnostics["runtime_uptime_seconds"] = round(float(uptime), 6)
        diagnostics["technical_warmup_seconds"] = float(TECHNICAL_WARMUP_SECONDS)
        if uptime < float(TECHNICAL_WARMUP_SECONDS):
            diagnostics["technical_warmup_remaining_seconds"] = round(
                float(TECHNICAL_WARMUP_SECONDS) - float(uptime),
                6,
            )

    if not isinstance(pricing, dict) or not bool(pricing.get("ready")):
        return None, "pricing_not_ready", diagnostics

    p_model_value = _safe_float(pricing.get("p_model"))
    if p_model_value is None or not (0.0 < p_model_value < 1.0):
        return None, "invalid_model_probability", diagnostics

    yes_bid, yes_ask, no_bid, no_ask = _best_quotes(book)
    diagnostics.update(
        {
            "yes_bid_cents": yes_bid,
            "yes_ask_cents": yes_ask,
            "no_bid_cents": no_bid,
            "no_ask_cents": no_ask,
        }
    )

    if yes_ask is None or no_ask is None:
        return None, "missing_best_quotes", diagnostics

    book_updated_ts = _safe_float(getattr(book, "last_update_ts", None))
    book_age_seconds = (
        None if book_updated_ts is None else max(0.0, ts - book_updated_ts)
    )
    diagnostics["orderbook_age_seconds"] = book_age_seconds
    if book_age_seconds is None or book_age_seconds > MAX_ORDERBOOK_AGE_SECONDS:
        return None, "stale_orderbook", diagnostics

    open_yes_contracts = max(0, int(open_yes_contracts))
    open_no_contracts = max(0, int(open_no_contracts))
    total_open_contracts = int(open_yes_contracts + open_no_contracts)

    yes_avg_entry = (
        float(open_yes_avg_entry_cents)
        if isinstance(open_yes_avg_entry_cents, (int, float))
        else float(yes_ask)
    )
    no_avg_entry = (
        float(open_no_avg_entry_cents)
        if isinstance(open_no_avg_entry_cents, (int, float))
        else float(no_ask)
    )

    fair_yes_cents = float(p_model_value) * 100.0
    fair_no_cents = (1.0 - float(p_model_value)) * 100.0

    diagnostics["fair_yes_cents"] = round(fair_yes_cents, 6)
    diagnostics["fair_no_cents"] = round(fair_no_cents, 6)
    diagnostics["open_yes_contracts"] = int(open_yes_contracts)
    diagnostics["open_no_contracts"] = int(open_no_contracts)
    diagnostics["open_yes_avg_entry_cents"] = round(float(yes_avg_entry), 6)
    diagnostics["open_no_avg_entry_cents"] = round(float(no_avg_entry), 6)
    diagnostics["total_open_contracts"] = int(total_open_contracts)

    p_book_snapshot = get_last_p_book_snapshot() or {}
    p_book = _safe_float(p_book_snapshot.get("p_book"))
    diagnostics["p_book"] = p_book

    sides: tuple[dict[str, Any], ...] = (
        {
            "name": "yes",
            "contracts": open_yes_contracts,
            "avg_entry": yes_avg_entry,
            "bid": yes_bid,
            "fair": fair_yes_cents,
            "probability": float(p_model_value),
        },
        {
            "name": "no",
            "contracts": open_no_contracts,
            "avg_entry": no_avg_entry,
            "bid": no_bid,
            "fair": fair_no_cents,
            "probability": 1.0 - float(p_model_value),
        },
    )

    def sell(
        side: dict[str, Any],
        *,
        count: int,
        reason: str,
        trigger: str,
    ) -> tuple[TradeSignal, str, dict[str, Any]]:
        diagnostics["exit_trigger"] = trigger
        signal = _build_sell_signal(
            ts=ts,
            market_ticker=market_ticker,
            side=str(side["name"]),
            count=count,
            quote_price_cents=slipped_price_cents(
                float(side["bid"]),
                settings.slippage_ticks,
                "down",
                price_ranges,
            ),
            fair_price_cents=float(side["fair"]),
            p_model=float(p_model_value),
            reason=reason,
            diagnostics=diagnostics,
        )
        return signal, signal.reason, diagnostics

    if settings.trading_style == "click":
        return None, "click_trading", diagnostics

    fee_policy_ready = fee_multiplier is not None and fee_type in {
        "quadratic",
        "quadratic_with_maker_fees",
    }
    for side in sides:
        name = str(side["name"])
        contracts = int(side["contracts"])
        avg_entry = float(side["avg_entry"])
        bid = side["bid"]
        fair = float(side["fair"])
        deployed = float(contracts) * avg_entry
        unrealized = (
            float(contracts) * (float(bid) - avg_entry) if bid is not None else None
        )
        exit_quote = (
            slipped_price_cents(
                float(bid), settings.slippage_ticks, "down", price_ranges
            )
            if bid is not None
            else None
        )
        exit_fee = (
            taker_fee_cents_per_contract(
                float(exit_quote), fee_multiplier=float(fee_multiplier)
            )
            if exit_quote is not None and fee_policy_ready
            else None
        )
        net_exit = (
            float(exit_quote) - float(exit_fee)
            if exit_quote is not None and exit_fee is not None
            else None
        )
        hold_edge = fair - net_exit if net_exit is not None else None
        side.update(
            deployed=deployed,
            unrealized=unrealized,
            hold_edge=hold_edge,
            net_exit=net_exit,
        )
        diagnostics[f"{name}_deployed_cents"] = round(deployed, 6)
        diagnostics[f"{name}_unrealized_cents"] = (
            None if unrealized is None else round(unrealized, 6)
        )
        diagnostics[f"{name}_hold_edge_cents"] = (
            None if hold_edge is None else round(hold_edge, 6)
        )
        diagnostics[f"{name}_net_exit_cents"] = (
            None if net_exit is None else round(net_exit, 6)
        )

    # Inventory exits never depend on entry gates or microstructure confirmation.
    for side in sides:
        name = str(side["name"])
        if (
            int(side["contracts"]) > 0
            and side["net_exit"] is not None
            and float(side["net_exit"])
            > float(side["fair"]) + float(settings.min_edge_cents)
        ):
            return sell(
                side,
                count=min(int(side["contracts"]), int(settings.max_order_contracts)),
                reason=f"edge_reversal_exit_{name}",
                trigger=f"{name}_bid_above_fair",
            )

    if settings.trading_style == "semi":
        return None, "manual_entries", diagnostics

    if diagnostics.get("technical_warmup_remaining_seconds") is not None:
        return None, "technical_warmup", diagnostics

    if settings.use_p_book_hard_gate:
        if p_book is None:
            return None, "p_book_unavailable", diagnostics
        divergence = abs(float(p_model_value) - float(p_book))
        diagnostics["p_book_divergence"] = divergence
        if (float(p_model_value) >= 0.5) != (float(p_book) >= 0.5):
            return None, "p_book_direction_conflict", diagnostics
        if divergence > settings.p_book_max_divergence:
            return None, "p_book_divergence_high", diagnostics

    if not (PROBABILITY_LOWER_BOUND <= float(p_model_value) <= PROBABILITY_UPPER_BOUND):
        diagnostics.update(
            {
                "probability_lower_bound": PROBABILITY_LOWER_BOUND,
                "probability_upper_bound": PROBABILITY_UPPER_BOUND,
            }
        )
        return None, "model_probability_out_of_bounds", diagnostics

    if bool(pricing.get("vol_is_fallback")):
        return None, "volatility_fallback", diagnostics

    seconds_to_expiry = _safe_float(pricing.get("seconds_to_expiry"))
    if seconds_to_expiry is None or seconds_to_expiry < ENTRY_CUTOFF_SECONDS_TO_EXPIRY:
        return None, "entry_cutoff", diagnostics

    diagnostics["fee_type"] = fee_type
    diagnostics["fee_multiplier"] = fee_multiplier
    if not fee_policy_ready:
        return None, "fee_policy_unavailable", diagnostics

    yes_limit = slipped_price_cents(
        yes_ask, settings.slippage_ticks, "up", price_ranges
    )
    no_limit = slipped_price_cents(no_ask, settings.slippage_ticks, "up", price_ranges)
    edge_yes = expected_value_cents(
        p_win=p_model_value,
        price_cents=yes_limit,
        fee_multiplier=fee_multiplier,
    )
    edge_no = expected_value_cents(
        p_win=1.0 - p_model_value,
        price_cents=no_limit,
        fee_multiplier=fee_multiplier,
    )
    diagnostics["edge_yes_cents"] = round(edge_yes, 6)
    diagnostics["edge_no_cents"] = round(edge_no, 6)
    diagnostics["credit_yes_cents"] = round(fair_yes_cents - yes_limit, 6)
    diagnostics["credit_no_cents"] = round(fair_no_cents - no_limit, 6)

    if edge_yes >= edge_no:
        side = "yes"
        ask = yes_limit
        model_side_prob = float(p_model_value)
        edge_cents = float(edge_yes)
        credit_cents = fair_yes_cents - float(ask)
        fair_price_cents = fair_yes_cents
        current_contracts = int(open_yes_contracts)
        current_avg_entry = float(yes_avg_entry)
    else:
        side = "no"
        ask = no_limit
        model_side_prob = 1.0 - float(p_model_value)
        edge_cents = float(edge_no)
        credit_cents = fair_no_cents - float(ask)
        fair_price_cents = fair_no_cents
        current_contracts = int(open_no_contracts)
        current_avg_entry = float(no_avg_entry)

    edge_probability = model_side_prob - (float(ask) / 100.0)
    confidence = abs(float(p_model_value) - 0.5)

    if edge_cents < float(settings.min_edge_cents):
        return None, "edge_below_threshold", diagnostics

    fee_cents = taker_fee_cents_per_contract(ask, fee_multiplier=float(fee_multiplier))
    target_contracts, kelly_fraction_quarter, all_in_cost = _kelly_target_contracts(
        p_win=model_side_prob,
        quote_price_cents=ask,
        bankroll_cents=max(0, int(bankroll_cents)),
        max_position_usd=float(settings.max_position_usd),
        fee_cents=fee_cents,
    )

    remaining_to_target = max(0, int(target_contracts) - int(current_contracts))
    minimum_credit_cents = float(settings.min_edge_cents) + fee_cents
    credit_target_contracts = _credit_target_contracts(
        credit_cents=credit_cents,
        minimum_credit_cents=minimum_credit_cents,
    )
    remaining_by_credit = max(0, int(credit_target_contracts) - int(current_contracts))
    count = min(
        int(settings.max_order_contracts),
        int(remaining_to_target),
        int(remaining_by_credit),
    )

    notional_cap_cents = (
        min(float(settings.max_position_usd), float(MAX_POSITION_USD_HARD_CAP)) * 100.0
    )
    current_side_notional_cents = float(current_contracts) * float(current_avg_entry)
    remaining_notional_headroom_cents = max(
        0.0, float(notional_cap_cents) - float(current_side_notional_cents)
    )
    max_by_notional_cap = int(remaining_notional_headroom_cents // all_in_cost)
    count = min(int(count), int(max_by_notional_cap))

    max_by_cash: int | None = None
    if isinstance(available_cash_cents, int) and available_cash_cents >= 0:
        max_by_cash = int(available_cash_cents // all_in_cost)
        count = min(int(count), int(max_by_cash))

    diagnostics["kelly_fraction_quarter"] = round(float(kelly_fraction_quarter), 8)
    diagnostics["all_in_cost_cents"] = round(float(all_in_cost), 6)
    diagnostics["kelly_target_contracts"] = int(target_contracts)
    diagnostics["credit_cents"] = round(float(credit_cents), 6)
    diagnostics["minimum_credit_cents"] = round(float(minimum_credit_cents), 6)
    diagnostics["marginal_credit_step_cents"] = MARGINAL_CREDIT_STEP_CENTS
    diagnostics["credit_target_contracts"] = int(credit_target_contracts)
    diagnostics["remaining_by_credit"] = int(remaining_by_credit)
    diagnostics["next_contract_required_credit_cents"] = round(
        minimum_credit_cents + current_contracts * MARGINAL_CREDIT_STEP_CENTS,
        6,
    )
    diagnostics["current_contracts"] = int(current_contracts)
    diagnostics["clip_contracts"] = int(count)
    diagnostics["position_notional_cap_cents"] = round(float(notional_cap_cents), 6)
    diagnostics["current_side_notional_cents"] = round(
        float(current_side_notional_cents), 6
    )
    diagnostics["remaining_notional_headroom_cents"] = round(
        float(remaining_notional_headroom_cents), 6
    )
    diagnostics["max_by_notional_cap"] = int(max_by_notional_cap)
    diagnostics["max_by_cash"] = None if max_by_cash is None else int(max_by_cash)

    if count <= 0:
        if max_by_notional_cap <= 0:
            return None, "position_notional_cap_reached", diagnostics
        if max_by_cash is not None and max_by_cash <= 0:
            return None, "insufficient_available_cash", diagnostics
        if remaining_by_credit <= 0:
            return None, "credit_size_target_reached", diagnostics
        return None, "at_target_allocation", diagnostics

    signal = TradeSignal(
        ts=ts,
        market_ticker=str(market_ticker),
        side=side,
        action="buy",
        count=int(count),
        quote_price_cents=round(float(ask), 4),
        fair_price_cents=round(float(fair_price_cents), 6),
        credit_cents=round(float(credit_cents), 6),
        edge_cents=round(float(edge_cents), 6),
        edge_probability=round(float(edge_probability), 8),
        confidence=round(float(confidence), 8),
        model_probability=round(float(p_model_value), 8),
        market_implied_probability=round(float(ask) / 100.0, 8),
        reason="ev_signal_ready",
        diagnostics=diagnostics,
    )
    return signal, signal.reason, diagnostics
