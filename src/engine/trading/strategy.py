from __future__ import annotations

import math
import time
from decimal import Decimal, InvalidOperation
from typing import Any

from engine.asian_pricer import prob_collapsed_variance_binary, prob_levy_tw_binary
from engine.orderbook import OrderBook
from engine.trading.fees import kelly_fraction_binary, taker_fee_cents_per_contract
from engine.trading.models import TradeSignal
from engine.trading.settings import TradingSettings

ENTRY_CUTOFF_SECONDS_TO_EXPIRY = 20.0
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


def _price_at_or_below(
    price_cents: float, price_ranges: list[dict[str, Any]] | None = None
) -> float:
    target = max(0.01, min(99.99, float(price_cents)))
    levels: list[float] = []
    try:
        for price_range in price_ranges or []:
            current = Decimal(str(price_range["start"]))
            end = Decimal(str(price_range["end"]))
            step = Decimal(str(price_range["step"]))
            if step <= 0:
                continue
            while current <= end and len(levels) < 20_000:
                cents = float(current * 100)
                if 0 < cents < 100:
                    levels.append(cents)
                current += step
    except (InvalidOperation, KeyError, TypeError, ValueError):
        levels.clear()
    if levels:
        return round(
            max((level for level in levels if level <= target + 1e-8), default=0.01), 4
        )
    return round(max(0.01, math.floor(target + 1e-8)), 4)


def _build_sell_signal(
    *,
    ts: float,
    market_ticker: str,
    side: str,
    count: int,
    quote_price_cents: float,
    fair_price_cents: float,
    fee_cents: float,
    p_model: float,
    reason: str,
    diagnostics: dict[str, Any],
) -> TradeSignal:
    normalized_side = "yes" if str(side).strip().lower() == "yes" else "no"
    model_side_probability = (
        float(p_model) if normalized_side == "yes" else (1.0 - float(p_model))
    )
    implied_probability = float(quote_price_cents) / 100.0
    net_exit_cents = float(quote_price_cents) - max(0.0, float(fee_cents))
    edge_cents = net_exit_cents - float(fair_price_cents)

    return TradeSignal(
        ts=float(ts),
        market_ticker=str(market_ticker),
        side=normalized_side,
        action="sell",
        count=max(1, int(count)),
        quote_price_cents=round(float(quote_price_cents), 4),
        fair_price_cents=round(float(fair_price_cents), 6),
        credit_cents=round(edge_cents, 6),
        edge_cents=round(edge_cents, 6),
        edge_probability=round(float(implied_probability - model_side_probability), 8),
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

    if not isinstance(pricing, dict) or not bool(pricing.get("ready")):
        return None, "pricing_not_ready", diagnostics

    p_model_value = _safe_float(pricing.get("p_model"))
    if p_model_value is None or not (0.0 < p_model_value < 1.0):
        return None, "invalid_model_probability", diagnostics

    if book is None or book.market_ticker != market_ticker:
        return None, "orderbook_market_mismatch", diagnostics

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

    book_updated_ts = _safe_float(
        (
            getattr(book, "last_verified_ts", None)
            or getattr(book, "last_update_ts", None)
        )
    )
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

    yes_levels, yes_asks, _, no_asks = book.get_orderbook_top_n(1)
    if yes_levels and yes_asks:
        bid_price, bid_size = yes_levels[0]
        ask_price, ask_size = yes_asks[0]
        total_size = float(bid_size) + float(ask_size)
        diagnostics["market_microprice"] = (
            (float(ask_price) * float(bid_size) + float(bid_price) * float(ask_size))
            / total_size
            / 100.0
            if total_size > 0
            else None
        )

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
            fee_cents=float(side.get("exit_fee") or 0.0),
            p_model=float(p_model_value),
            reason=reason,
            diagnostics=diagnostics,
        )
        return signal, signal.reason, diagnostics

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
                float(exit_quote),
                fee_multiplier=float(fee_multiplier),
                action="sell",
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
            exit_fee=exit_fee,
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

    # Inventory exits never depend on entry gates.
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

    market_probability = (
        (float(yes_bid) + float(yes_ask)) / 200.0
        if yes_bid is not None and yes_ask is not None
        else None
    )
    diagnostics["market_probability"] = market_probability
    if market_probability is None:
        return None, "market_probability_unavailable", diagnostics
    pricer_detail = pricing.get("pricer_detail") or {}
    required_future_avg = _safe_float(pricer_detail.get("required_future_avg"))
    deterministic_outcome = pricing.get("regime") == "terminal" or (
        pricing.get("regime") == "collapsed"
        and required_future_avg is not None
        and required_future_avg <= 0
    )
    diagnostics["deterministic_outcome"] = deterministic_outcome

    if bool(pricing.get("vol_is_fallback")):
        return None, "volatility_fallback", diagnostics

    seconds_to_expiry = _safe_float(pricing.get("seconds_to_expiry"))
    if (
        seconds_to_expiry is None
        or seconds_to_expiry <= 0
        or (
            seconds_to_expiry < ENTRY_CUTOFF_SECONDS_TO_EXPIRY
            and not deterministic_outcome
        )
    ):
        return None, "entry_cutoff", diagnostics

    diagnostics["fee_type"] = fee_type
    diagnostics["fee_multiplier"] = fee_multiplier
    if not fee_policy_ready:
        return None, "fee_policy_unavailable", diagnostics

    required_taker_edge = (
        float(settings.deterministic_min_edge_cents)
        if deterministic_outcome
        else float(settings.min_edge_cents)
    )
    diagnostics["required_taker_edge_cents"] = required_taker_edge
    diagnostics["kelly_scale"] = float(settings.kelly_fraction)
    diagnostics["max_position_fraction"] = float(settings.max_position_fraction)

    side_inputs = [
        {
            "name": "yes",
            "bid": yes_bid,
            "ask": yes_ask,
            "top_size": int(yes_asks[0][1]) if yes_asks else 0,
            "fair": fair_yes_cents,
            "probability": float(p_model_value),
            "contracts": int(open_yes_contracts),
            "avg_entry": float(yes_avg_entry),
        },
        {
            "name": "no",
            "bid": no_bid,
            "ask": no_ask,
            "top_size": int(no_asks[0][1]) if no_asks else 0,
            "fair": fair_no_cents,
            "probability": 1.0 - float(p_model_value),
            "contracts": int(open_no_contracts),
            "avg_entry": float(no_avg_entry),
        },
    ]
    for candidate in side_inputs:
        slipped_limit = slipped_price_cents(
            float(candidate["ask"]), settings.slippage_ticks, "up", price_ranges
        )
        limit_fee = taker_fee_cents_per_contract(
            slipped_limit, fee_multiplier=float(fee_multiplier)
        )
        maximum_limit = _price_at_or_below(
            float(candidate["fair"]) - required_taker_edge - limit_fee,
            price_ranges,
        )
        candidate["taker_limit"] = min(slipped_limit, maximum_limit)
        candidate["taker_fee"] = taker_fee_cents_per_contract(
            float(candidate["ask"]), fee_multiplier=float(fee_multiplier)
        )
        candidate["taker_edge"] = (
            float(candidate["fair"])
            - float(candidate["ask"])
            - float(candidate["taker_fee"])
        )
        name = str(candidate["name"])
        diagnostics[f"edge_{name}_cents"] = round(float(candidate["taker_edge"]), 6)
        diagnostics[f"credit_{name}_cents"] = round(
            float(candidate["fair"]) - float(candidate["ask"]), 6
        )

    def allocation(candidate: dict[str, Any], fee_cents: float) -> dict[str, Any]:
        target, scaled_kelly, all_in_cost, notional_cap = _kelly_target_contracts(
            p_win=float(candidate["probability"]),
            quote_price_cents=float(candidate["price"]),
            bankroll_cents=max(0, int(bankroll_cents)),
            max_position_usd=float(settings.max_position_usd),
            max_position_fraction=float(settings.max_position_fraction),
            kelly_scale=float(settings.kelly_fraction),
            fee_cents=fee_cents,
        )
        current = int(candidate["contracts"])
        current_notional = current * float(candidate["avg_entry"])
        headroom = max(0.0, notional_cap - current_notional)
        max_by_notional = int(headroom // all_in_cost)
        max_by_cash = (
            int(available_cash_cents // all_in_cost)
            if isinstance(available_cash_cents, int) and available_cash_cents >= 0
            else None
        )
        count = min(
            int(settings.max_order_contracts),
            max(0, target - current),
            max_by_notional,
            max_by_cash
            if max_by_cash is not None
            else int(settings.max_order_contracts),
        )
        return {
            "count": count,
            "target": target,
            "scaled_kelly": scaled_kelly,
            "all_in_cost": all_in_cost,
            "notional_cap": notional_cap,
            "current_notional": current_notional,
            "headroom": headroom,
            "max_by_notional": max_by_notional,
            "max_by_cash": max_by_cash,
        }

    def record_allocation(candidate: dict[str, Any], result: dict[str, Any]) -> None:
        target = int(result["target"])
        signed_target = target if candidate["name"] == "yes" else -target
        diagnostics.update(
            {
                "target_side": str(candidate["name"]),
                "credit_cents": round(
                    float(candidate["fair"]) - float(candidate["price"]), 6
                ),
                "target_position_contracts": signed_target,
                "current_signed_position_contracts": int(open_yes_contracts)
                - int(open_no_contracts),
                "scaled_kelly_fraction": round(float(result["scaled_kelly"]), 8),
                "all_in_cost_cents": round(float(result["all_in_cost"]), 6),
                "kelly_target_contracts": target,
                "current_contracts": int(candidate["contracts"]),
                "clip_contracts": int(result["count"]),
                "position_notional_cap_cents": round(float(result["notional_cap"]), 6),
                "current_side_notional_cents": round(
                    float(result["current_notional"]), 6
                ),
                "remaining_notional_headroom_cents": round(
                    float(result["headroom"]), 6
                ),
                "max_by_notional_cap": int(result["max_by_notional"]),
                "max_by_cash": result["max_by_cash"],
            }
        )

    best_taker = max(side_inputs, key=lambda candidate: float(candidate["taker_edge"]))
    if float(best_taker["taker_edge"]) >= required_taker_edge and float(
        best_taker["taker_limit"]
    ) >= float(best_taker["ask"]):
        best_taker["price"] = float(best_taker["ask"])
        result = allocation(best_taker, float(best_taker["taker_fee"]))
        result["count"] = min(int(result["count"]), int(best_taker["top_size"]))
        diagnostics["max_by_top_of_book"] = int(best_taker["top_size"])
        record_allocation(best_taker, result)
        if int(result["count"]) <= 0:
            if int(result["max_by_notional"]) <= 0:
                return None, "position_notional_cap_reached", diagnostics
            if result["max_by_cash"] is not None and int(result["max_by_cash"]) <= 0:
                return None, "insufficient_available_cash", diagnostics
            return None, "at_target_allocation", diagnostics
        signal = TradeSignal(
            ts=ts,
            market_ticker=str(market_ticker),
            side=str(best_taker["name"]),
            action="buy",
            count=int(result["count"]),
            quote_price_cents=round(float(best_taker["taker_limit"]), 4),
            fair_price_cents=round(float(best_taker["fair"]), 6),
            credit_cents=round(
                float(best_taker["fair"]) - float(best_taker["price"]), 6
            ),
            edge_cents=round(float(best_taker["taker_edge"]), 6),
            edge_probability=round(
                float(best_taker["probability"]) - float(best_taker["price"]) / 100.0,
                8,
            ),
            confidence=round(abs(float(p_model_value) - 0.5), 8),
            model_probability=round(float(p_model_value), 8),
            market_implied_probability=round(float(best_taker["price"]) / 100.0, 8),
            reason="ev_signal_ready",
            diagnostics=diagnostics,
        )
        return signal, signal.reason, diagnostics

    return None, "edge_below_threshold", diagnostics
