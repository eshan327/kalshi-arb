from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any, Literal

from core.config import (
    EXECUTION_CONTRACT_SECONDS,
    EXECUTION_MAX_ORDER_CONTRACTS,
    EXECUTION_MAX_PROBABILITY,
    EXECUTION_MIN_CONFIDENCE,
    EXECUTION_MIN_EDGE_CENTS,
    EXECUTION_MIN_PROBABILITY,
    EXECUTION_MIN_TIMING_SCORE,
    EXECUTION_P_BOOK_MAX_DIVERGENCE,
    EXECUTION_P_BOOK_MIN_QUALITY,
    EXECUTION_REQUIRE_P_BOOK_CONFIRMATION,
    EXECUTION_TARGET_EDGE_CENTS,
)
from engine.book_microstructure import get_last_p_book_snapshot
from engine.execution.models import ExecutionIntent, ExecutionSignal
from engine.orderbook import OrderBook


@dataclass(frozen=True)
class PolicyDecision:
    signal: ExecutionSignal | None
    reason: str


def _resolve_bool(overrides: dict[str, Any], key: str, default: bool) -> bool:
    raw = overrides.get(key)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        val = raw.strip().lower()
        if val in {"1", "true", "yes", "y", "on"}:
            return True
        if val in {"0", "false", "no", "n", "off"}:
            return False
    return bool(default)


def _resolve_float(
    overrides: dict[str, Any],
    key: str,
    default: float,
    *,
    min_value: float | None = None,
    max_value: float | None = None,
) -> float:
    raw = overrides.get(key)
    value = float(default)
    if isinstance(raw, (int, float)):
        value = float(raw)
    elif isinstance(raw, str):
        try:
            value = float(raw.strip())
        except ValueError:
            value = float(default)

    if min_value is not None:
        value = max(float(min_value), value)
    if max_value is not None:
        value = min(float(max_value), value)
    return value


def _resolve_int(
    overrides: dict[str, Any],
    key: str,
    default: int,
    *,
    min_value: int | None = None,
    max_value: int | None = None,
) -> int:
    raw = overrides.get(key)
    value = int(default)
    if isinstance(raw, int):
        value = int(raw)
    elif isinstance(raw, float):
        value = int(raw)
    elif isinstance(raw, str):
        try:
            value = int(raw.strip())
        except ValueError:
            value = int(default)

    if min_value is not None:
        value = max(int(min_value), value)
    if max_value is not None:
        value = min(int(max_value), value)
    return value


def _policy_params(policy_overrides: dict[str, Any] | None) -> dict[str, Any]:
    overrides = dict(policy_overrides) if isinstance(policy_overrides, dict) else {}

    min_probability = _resolve_float(
        overrides,
        "min_probability",
        EXECUTION_MIN_PROBABILITY,
        min_value=0.0,
        max_value=1.0,
    )
    max_probability = _resolve_float(
        overrides,
        "max_probability",
        EXECUTION_MAX_PROBABILITY,
        min_value=min_probability,
        max_value=1.0,
    )

    return {
        "profile_name": str(overrides.get("profile_name") or "default"),
        "min_probability": min_probability,
        "max_probability": max_probability,
        "min_confidence": _resolve_float(
            overrides,
            "min_confidence",
            EXECUTION_MIN_CONFIDENCE,
            min_value=0.0,
            max_value=0.5,
        ),
        "min_edge_cents": _resolve_float(
            overrides,
            "min_edge_cents",
            EXECUTION_MIN_EDGE_CENTS,
            min_value=0.0,
        ),
        "min_timing_score": _resolve_float(
            overrides,
            "min_timing_score",
            EXECUTION_MIN_TIMING_SCORE,
            min_value=0.0,
            max_value=1.0,
        ),
        "require_p_book_confirmation": _resolve_bool(
            overrides,
            "require_p_book_confirmation",
            EXECUTION_REQUIRE_P_BOOK_CONFIRMATION,
        ),
        "p_book_min_quality": _resolve_float(
            overrides,
            "p_book_min_quality",
            EXECUTION_P_BOOK_MIN_QUALITY,
            min_value=0.0,
            max_value=1.0,
        ),
        "p_book_max_divergence": _resolve_float(
            overrides,
            "p_book_max_divergence",
            EXECUTION_P_BOOK_MAX_DIVERGENCE,
            min_value=0.01,
            max_value=0.49,
        ),
        "allow_taker": _resolve_bool(overrides, "allow_taker", False),
        "aggressive_edge_cents": _resolve_float(
            overrides,
            "aggressive_edge_cents",
            EXECUTION_TARGET_EDGE_CENTS,
            min_value=0.0,
        ),
        "max_order_contracts": _resolve_int(
            overrides,
            "max_order_contracts",
            EXECUTION_MAX_ORDER_CONTRACTS,
            min_value=1,
        ),
    }


def timing_score_from_seconds_to_expiry(
    seconds_to_expiry: float,
    *,
    contract_seconds: int = EXECUTION_CONTRACT_SECONDS,
) -> float:
    """Returns a deterministic score in [0,1] for placement timing in the 15m window."""
    if seconds_to_expiry <= 0:
        return 0.0

    total = max(1.0, float(contract_seconds))
    progress = 1.0 - max(0.0, min(1.0, float(seconds_to_expiry) / total))

    if progress < 0.15:
        return 0.35
    if progress < 0.60:
        return 1.00
    if progress < 0.85:
        return 0.80
    if progress < 0.95:
        return 0.55
    return 0.25


def _extract_best_quotes(book: OrderBook | None) -> dict[str, float | None]:
    if book is None or not book.initialized:
        return {
            "yes_bid": None,
            "yes_ask": None,
            "no_bid": None,
            "no_ask": None,
        }

    yes_bid, yes_ask, no_bid, no_ask = book.get_best_prices()
    return {
        "yes_bid": float(yes_bid) if isinstance(yes_bid, (int, float)) else None,
        "yes_ask": float(yes_ask) if isinstance(yes_ask, (int, float)) else None,
        "no_bid": float(no_bid) if isinstance(no_bid, (int, float)) else None,
        "no_ask": float(no_ask) if isinstance(no_ask, (int, float)) else None,
    }


def _clip_price(price_cents: float) -> int:
    return max(1, min(99, int(round(price_cents))))


def _scaled_size(
    *,
    edge_cents: float,
    confidence: float,
    timing_score: float,
    min_confidence: float,
    min_edge_cents: float,
    max_order_contracts: int = EXECUTION_MAX_ORDER_CONTRACTS,
) -> int:
    cap = max(1, int(max_order_contracts))
    edge_denom = max(0.01, EXECUTION_TARGET_EDGE_CENTS - min_edge_cents)
    edge_norm = max(0.0, min(1.0, (edge_cents - min_edge_cents) / edge_denom))

    confidence_denom = max(0.01, 0.5 - min_confidence)
    confidence_norm = max(0.0, min(1.0, (confidence - min_confidence) / confidence_denom))

    score = 0.55 * edge_norm + 0.30 * confidence_norm + 0.15 * max(0.0, min(1.0, timing_score))
    raw = int(round(1 + score * (cap - 1)))
    return max(1, min(cap, raw))


def _candidate_order_price(
    *,
    fair_price_cents: float,
    best_bid: float,
    best_ask: float | None,
    edge_before_price: float,
    min_edge_cents: float,
    timing_score: float,
    allow_taker: bool,
    aggressive_edge_cents: float,
    force_taker: bool,
) -> tuple[int, float, ExecutionIntent]:
    """Select maker or taker price while preserving minimum edge constraints."""
    if allow_taker and best_ask is not None and (force_taker or edge_before_price >= aggressive_edge_cents):
        taker_price = _clip_price(best_ask)
        taker_edge = fair_price_cents - taker_price
        if taker_edge >= min_edge_cents:
            return taker_price, taker_edge, "taker"

    price = _clip_price(best_bid)
    edge = fair_price_cents - price

    if (
        best_ask is not None
        and (price + 1) < best_ask
        and timing_score >= 0.75
        and (fair_price_cents - (price + 1)) >= min_edge_cents
    ):
        price = _clip_price(price + 1)
        edge = fair_price_cents - price

    return price, edge, "maker"


def _get_p_book_inputs() -> tuple[float | None, float | None]:
    snapshot = get_last_p_book_snapshot()
    if not isinstance(snapshot, dict):
        return None, None

    p_book = snapshot.get("p_book")
    quality = snapshot.get("p_book_quality")
    if quality is None:
        quality = snapshot.get("reliability")

    p_book_value = float(p_book) if isinstance(p_book, (int, float)) else None
    quality_value = float(quality) if isinstance(quality, (int, float)) else None
    return p_book_value, quality_value


def _p_book_alignment_status(
    p_model: float,
    p_book: float,
    *,
    max_divergence: float,
) -> tuple[bool, str]:
    model_side = "yes" if p_model >= 0.5 else "no"
    book_side = "yes" if p_book >= 0.5 else "no"
    if model_side != book_side:
        return False, "p_book_side_disagreement"

    divergence = abs(float(p_model) - float(p_book))
    if divergence > max_divergence:
        return False, "p_book_divergence_too_high"

    return True, "aligned"


def build_policy_decision(
    *,
    pricing: dict[str, Any],
    market_ticker: str,
    book: OrderBook | None,
    policy_overrides: dict[str, Any] | None = None,
    force_fallback_attempt: bool = False,
    now_ts: float | None = None,
) -> PolicyDecision:
    ts = time.time() if now_ts is None else float(now_ts)
    params = _policy_params(policy_overrides)

    min_probability = float(params["min_probability"])
    max_probability = float(params["max_probability"])
    min_confidence = float(params["min_confidence"])
    min_edge_cents = float(params["min_edge_cents"])
    min_timing_score = float(params["min_timing_score"])
    require_p_book_confirmation = bool(params["require_p_book_confirmation"])
    p_book_min_quality = float(params["p_book_min_quality"])
    p_book_max_divergence = float(params["p_book_max_divergence"])
    allow_taker = bool(params["allow_taker"])
    aggressive_edge_cents = float(params["aggressive_edge_cents"])
    max_order_contracts = int(params["max_order_contracts"])
    profile_name = str(params["profile_name"])

    if not isinstance(pricing, dict) or not pricing.get("ready"):
        return PolicyDecision(signal=None, reason="pricing_not_ready")

    p_model = pricing.get("p_model")
    if not isinstance(p_model, (int, float)):
        return PolicyDecision(signal=None, reason="missing_probability")
    p = float(p_model)

    if p <= 0.0 or p >= 1.0:
        return PolicyDecision(signal=None, reason="invalid_probability")

    if p < min_probability or p > max_probability:
        return PolicyDecision(signal=None, reason="probability_exclusion_band")

    confidence = abs(p - 0.5)
    if confidence < min_confidence:
        return PolicyDecision(signal=None, reason="confidence_below_threshold")

    p_book_value, p_book_quality = _get_p_book_inputs()
    alignment_status = "missing"

    if require_p_book_confirmation:
        if p_book_value is None:
            return PolicyDecision(signal=None, reason="p_book_unavailable")
        if p_book_quality is None:
            return PolicyDecision(signal=None, reason="p_book_quality_missing")
        if p_book_quality < p_book_min_quality:
            return PolicyDecision(signal=None, reason="p_book_quality_low")

        aligned, alignment_status = _p_book_alignment_status(
            p,
            p_book_value,
            max_divergence=p_book_max_divergence,
        )
        if not aligned:
            return PolicyDecision(signal=None, reason=alignment_status)
    elif p_book_value is not None:
        aligned, alignment_status = _p_book_alignment_status(
            p,
            p_book_value,
            max_divergence=p_book_max_divergence,
        )
        if not aligned:
            alignment_status = f"non_blocking_{alignment_status}"

    seconds_to_expiry = pricing.get("seconds_to_expiry")
    if not isinstance(seconds_to_expiry, (int, float)):
        return PolicyDecision(signal=None, reason="missing_seconds_to_expiry")

    sec_exp = max(0.0, float(seconds_to_expiry))
    timing_score = timing_score_from_seconds_to_expiry(sec_exp)
    if timing_score < min_timing_score:
        return PolicyDecision(signal=None, reason="timing_score_below_threshold")

    quotes = _extract_best_quotes(book)
    yes_bid = quotes["yes_bid"]
    no_bid = quotes["no_bid"]
    yes_ask = quotes["yes_ask"]
    no_ask = quotes["no_ask"]

    if yes_bid is None and no_bid is None:
        return PolicyDecision(signal=None, reason="missing_actionable_bids")

    fair_yes = max(1.0, min(99.0, p * 100.0))
    fair_no = max(1.0, min(99.0, 100.0 - fair_yes))

    edge_yes = -math.inf if yes_bid is None else fair_yes - float(yes_bid)
    edge_no = -math.inf if no_bid is None else fair_no - float(no_bid)

    side: Literal["yes", "no"]
    if edge_yes >= edge_no:
        side = "yes"
        fair = fair_yes
        best_bid = yes_bid
        best_ask = yes_ask
        edge = edge_yes
    else:
        side = "no"
        fair = fair_no
        best_bid = no_bid
        best_ask = no_ask
        edge = edge_no

    if best_bid is None or edge < min_edge_cents:
        return PolicyDecision(signal=None, reason="edge_below_threshold")

    quote_price_cents, edge_after_price, execution_intent = _candidate_order_price(
        fair_price_cents=fair,
        best_bid=float(best_bid),
        best_ask=float(best_ask) if isinstance(best_ask, (int, float)) else None,
        edge_before_price=edge,
        min_edge_cents=min_edge_cents,
        timing_score=timing_score,
        allow_taker=allow_taker,
        aggressive_edge_cents=aggressive_edge_cents,
        force_taker=force_fallback_attempt,
    )

    if edge_after_price < min_edge_cents:
        return PolicyDecision(signal=None, reason="edge_below_threshold_after_pricing")

    count = _scaled_size(
        edge_cents=edge_after_price,
        confidence=confidence,
        timing_score=timing_score,
        min_confidence=min_confidence,
        min_edge_cents=min_edge_cents,
        max_order_contracts=max_order_contracts,
    )

    signal_reason = "fallback_signal_ready" if force_fallback_attempt else "signal_ready"

    signal = ExecutionSignal(
        ts=ts,
        market_ticker=str(market_ticker),
        side=side,
        action="buy",
        execution_intent=execution_intent,
        is_fallback_attempt=bool(force_fallback_attempt),
        policy_profile=profile_name,
        quote_price_cents=quote_price_cents,
        fair_price_cents=round(fair, 4),
        edge_cents=round(edge_after_price, 4),
        confidence=round(confidence, 8),
        p_model=round(p, 8),
        p_book=None if p_book_value is None else round(p_book_value, 8),
        p_book_quality=None if p_book_quality is None else round(p_book_quality, 8),
        p_book_alignment=alignment_status,
        seconds_to_expiry=round(sec_exp, 3),
        timing_score=round(timing_score, 6),
        count=count,
        reason=signal_reason,
    )
    return PolicyDecision(signal=signal, reason=signal_reason)
