from __future__ import annotations

import time
from typing import Any

from engine.asian_pricer import prob_collapsed_variance_binary, prob_levy_tw_binary
from engine.book_microstructure import get_last_p_book_snapshot
from engine.orderbook import OrderBook
from engine.shadow.fee_model import (
    expected_value_no_cents,
    expected_value_yes_cents,
    quarter_kelly_fraction_binary,
)
from engine.shadow.models import ShadowSignal
from engine.shadow.settings_state import ShadowSettings

PROBABILITY_LOWER_BOUND = 0.20
PROBABILITY_UPPER_BOUND = 0.80
MAX_POSITION_USD_HARD_CAP = 50.0
MAX_CLIP_CONTRACTS = 50


def _safe_float(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _best_quotes(book: OrderBook | None) -> tuple[int | None, int | None, int | None, int | None]:
    if book is None or not book.initialized:
        return None, None, None, None

    yes_bid, yes_ask, no_bid, no_ask = book.get_best_prices()

    return (
        int(round(float(yes_bid))) if isinstance(yes_bid, (int, float)) else None,
        int(round(float(yes_ask))) if isinstance(yes_ask, (int, float)) else None,
        int(round(float(no_bid))) if isinstance(no_bid, (int, float)) else None,
        int(round(float(no_ask))) if isinstance(no_ask, (int, float)) else None,
    )


def _kelly_target_contracts(
    *,
    p_win: float,
    quote_price_cents: int,
    bankroll_cents: int,
    max_position_usd: float,
) -> int:
    px = max(1, int(quote_price_cents))
    kelly_fraction = quarter_kelly_fraction_binary(p_win=float(p_win), cost_cents=float(px))
    cap_by_kelly = float(max(0, int(bankroll_cents))) * float(kelly_fraction)
    cap_by_fixed = min(float(max_position_usd), MAX_POSITION_USD_HARD_CAP) * 100.0
    notional_cap_cents = max(0.0, min(cap_by_kelly, cap_by_fixed))
    return max(0, int(notional_cap_cents // float(px)))


def apply_pricing_overrides(pricing: dict[str, Any], settings: ShadowSettings) -> dict[str, Any]:
    """Optionally re-runs pricer with volatility override and responsiveness scaling."""
    out = dict(pricing) if isinstance(pricing, dict) else {}
    if not bool(out.get("ready")):
        return out

    spot = _safe_float(out.get("spot_index"))
    strike = _safe_float(out.get("strike_usd"))
    sec_exp = _safe_float(out.get("seconds_to_expiry"))
    base_sigma = _safe_float(out.get("sigma_annual"))
    settlement_window = int(_safe_float(out.get("settlement_window_seconds")) or 60)

    if spot is None or strike is None or sec_exp is None or base_sigma is None:
        return out

    sigma = settings.volatility_override if isinstance(settings.volatility_override, float) else base_sigma
    sigma = max(0.01, float(sigma) * float(settings.levy_responsiveness))

    if sec_exp > float(settlement_window):
        result = prob_levy_tw_binary(
            S0=spot,
            strike=strike,
            sigma_annual=sigma,
            seconds_to_expiry=sec_exp,
            n_fixes=settlement_window,
        )
    else:
        k = max(0, int(_safe_float(out.get("twap_seconds_elapsed")) or 0))
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
    out["regime"] = result.regime
    return out


def build_shadow_signal(
    *,
    pricing: dict[str, Any],
    market_ticker: str,
    book: OrderBook | None,
    settings: ShadowSettings,
    bankroll_cents: int,
    open_yes_contracts: int = 0,
    open_no_contracts: int = 0,
    available_cash_cents: int | None = None,
    now_ts: float | None = None,
) -> tuple[ShadowSignal | None, str, dict[str, Any]]:
    ts = time.time() if now_ts is None else float(now_ts)
    diagnostics: dict[str, Any] = {}

    if not settings.strategy_enabled:
        return None, "strategy_disabled", diagnostics

    if not isinstance(pricing, dict) or not bool(pricing.get("ready")):
        return None, "pricing_not_ready", diagnostics

    p_model_value = _safe_float(pricing.get("p_model"))
    if p_model_value is None or not (0.0 < p_model_value < 1.0):
        return None, "invalid_model_probability", diagnostics

    if not (PROBABILITY_LOWER_BOUND <= float(p_model_value) <= PROBABILITY_UPPER_BOUND):
        diagnostics.update(
            {
                "probability_lower_bound": PROBABILITY_LOWER_BOUND,
                "probability_upper_bound": PROBABILITY_UPPER_BOUND,
            }
        )
        return None, "model_probability_out_of_bounds", diagnostics

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

    fair_yes_cents = float(p_model_value) * 100.0
    fair_no_cents = (1.0 - float(p_model_value)) * 100.0

    diagnostics["fair_yes_cents"] = round(fair_yes_cents, 6)
    diagnostics["fair_no_cents"] = round(fair_no_cents, 6)
    diagnostics["open_yes_contracts"] = int(max(0, open_yes_contracts))
    diagnostics["open_no_contracts"] = int(max(0, open_no_contracts))

    p_book_snapshot = get_last_p_book_snapshot() or {}
    p_book = _safe_float(p_book_snapshot.get("p_book"))
    p_book_quality = _safe_float(p_book_snapshot.get("p_book_quality"))
    if p_book_quality is None:
        p_book_quality = _safe_float(p_book_snapshot.get("reliability"))

    diagnostics["p_book"] = p_book
    diagnostics["p_book_quality"] = p_book_quality

    if settings.use_p_book_hard_gate:
        if p_book is None:
            return None, "p_book_unavailable", diagnostics
        if p_book_quality is None or p_book_quality < settings.p_book_min_quality:
            return None, "p_book_quality_low", diagnostics
        divergence = abs(float(p_model_value) - float(p_book))
        diagnostics["p_book_divergence"] = divergence
        if divergence > settings.p_book_max_divergence:
            return None, "p_book_divergence_high", diagnostics

    # Dynamic scale-out: if held side is richer than model fair value, sell clips to reduce risk.
    if int(open_yes_contracts) > 0 and yes_bid is not None and float(yes_bid) > (fair_yes_cents + float(settings.min_edge_cents)):
        clip_count = min(int(open_yes_contracts), int(MAX_CLIP_CONTRACTS))
        diagnostics["exit_trigger"] = "yes_bid_above_fair"
        signal = ShadowSignal(
            ts=ts,
            market_ticker=str(market_ticker),
            side="yes",
            action="sell",
            intent="taker",
            count=int(clip_count),
            quote_price_cents=int(yes_bid),
            fair_price_cents=round(float(fair_yes_cents), 6),
            edge_cents=round(float(fair_yes_cents - float(yes_bid)), 6),
            edge_probability=round(float(p_model_value) - (float(yes_bid) / 100.0), 8),
            confidence=round(abs(float(p_model_value) - 0.5), 8),
            model_probability=round(float(p_model_value), 8),
            market_implied_probability=round(float(yes_bid) / 100.0, 8),
            reason="edge_reversal_exit_yes",
            diagnostics=diagnostics,
        )
        return signal, signal.reason, diagnostics

    if int(open_no_contracts) > 0 and no_bid is not None and float(no_bid) > (fair_no_cents + float(settings.min_edge_cents)):
        clip_count = min(int(open_no_contracts), int(MAX_CLIP_CONTRACTS))
        diagnostics["exit_trigger"] = "no_bid_above_fair"
        signal = ShadowSignal(
            ts=ts,
            market_ticker=str(market_ticker),
            side="no",
            action="sell",
            intent="taker",
            count=int(clip_count),
            quote_price_cents=int(no_bid),
            fair_price_cents=round(float(fair_no_cents), 6),
            edge_cents=round(float(fair_no_cents - float(no_bid)), 6),
            edge_probability=round((1.0 - float(p_model_value)) - (float(no_bid) / 100.0), 8),
            confidence=round(abs(float(p_model_value) - 0.5), 8),
            model_probability=round(float(p_model_value), 8),
            market_implied_probability=round(float(no_bid) / 100.0, 8),
            reason="edge_reversal_exit_no",
            diagnostics=diagnostics,
        )
        return signal, signal.reason, diagnostics

    edge_yes = expected_value_yes_cents(
        p_model=p_model_value,
        ask_price_cents=yes_ask,
        fee_curve_coeff=settings.taker_fee_curve_coeff,
    )
    edge_no = expected_value_no_cents(
        p_model=p_model_value,
        ask_price_cents=no_ask,
        fee_curve_coeff=settings.taker_fee_curve_coeff,
    )
    diagnostics["edge_yes_cents"] = round(edge_yes, 6)
    diagnostics["edge_no_cents"] = round(edge_no, 6)

    if edge_yes >= edge_no:
        side = "yes"
        ask = int(yes_ask)
        model_side_prob = float(p_model_value)
        edge_cents = float(edge_yes)
        fair_price_cents = fair_yes_cents
        current_contracts = max(0, int(open_yes_contracts))
    else:
        side = "no"
        ask = int(no_ask)
        model_side_prob = 1.0 - float(p_model_value)
        edge_cents = float(edge_no)
        fair_price_cents = fair_no_cents
        current_contracts = max(0, int(open_no_contracts))

    edge_probability = model_side_prob - (float(ask) / 100.0)
    confidence = abs(float(p_model_value) - 0.5)

    if edge_cents < float(settings.min_edge_cents):
        return None, "edge_below_threshold", diagnostics

    target_contracts = _kelly_target_contracts(
        p_win=model_side_prob,
        quote_price_cents=ask,
        bankroll_cents=max(0, int(bankroll_cents)),
        max_position_usd=float(settings.max_position_usd),
    )

    remaining_to_target = max(0, int(target_contracts) - int(current_contracts))
    count = min(int(MAX_CLIP_CONTRACTS), int(remaining_to_target))

    if isinstance(available_cash_cents, int) and available_cash_cents >= 0:
        max_by_cash = int(available_cash_cents) // max(1, int(ask))
        count = min(int(count), int(max_by_cash))

    diagnostics["kelly_fraction_quarter"] = round(
        quarter_kelly_fraction_binary(p_win=model_side_prob, cost_cents=float(ask)),
        8,
    )
    diagnostics["kelly_target_contracts"] = int(target_contracts)
    diagnostics["current_contracts"] = int(current_contracts)
    diagnostics["clip_contracts"] = int(count)

    if count <= 0:
        return None, "at_target_allocation", diagnostics

    signal = ShadowSignal(
        ts=ts,
        market_ticker=str(market_ticker),
        side=side,
        action="buy",
        intent="taker",
        count=int(count),
        quote_price_cents=int(ask),
        fair_price_cents=round(float(fair_price_cents), 6),
        edge_cents=round(float(edge_cents), 6),
        edge_probability=round(float(edge_probability), 8),
        confidence=round(float(confidence), 8),
        model_probability=round(float(p_model_value), 8),
        market_implied_probability=round(float(ask) / 100.0, 8),
        reason="ev_signal_ready",
        diagnostics=diagnostics,
    )
    return signal, signal.reason, diagnostics
