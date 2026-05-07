from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from threading import RLock
from typing import Any

from core.asset_context import get_active_asset_context
from core.config import (
    EXECUTION_ALLOW_LIVE_IN_DEMO_ENV,
    EXECUTION_ENABLED,
    EXECUTION_LOOP_INTERVAL_SEC,
    EXECUTION_MAX_PROBABILITY,
    EXECUTION_MIN_CONFIDENCE,
    EXECUTION_MIN_EDGE_CENTS,
    EXECUTION_MIN_PROBABILITY,
    EXECUTION_MIN_TIMING_SCORE,
    EXECUTION_MODE,
    EXECUTION_ORDER_STALE_SEC,
    EXECUTION_P_BOOK_MAX_DIVERGENCE,
    EXECUTION_P_BOOK_MIN_QUALITY,
    EXECUTION_POST_ONLY,
    EXECUTION_REQUIRE_P_BOOK_CONFIRMATION,
    KALSHI_ENV,
    PAPER_ACTIVITY_AGGRESSIVE_EDGE_CENTS,
    PAPER_ACTIVITY_ALLOW_TAKER,
    PAPER_ACTIVITY_FALLBACK_BYPASS_P_BOOK,
    PAPER_ACTIVITY_FALLBACK_MIN_EDGE_CENTS,
    PAPER_ACTIVITY_FALLBACK_RETRY_SEC,
    PAPER_ACTIVITY_FALLBACK_TRIGGER_SEC,
    PAPER_ACTIVITY_FORCE_FILL_PER_WINDOW,
    PAPER_ACTIVITY_MIN_CONFIDENCE,
    PAPER_ACTIVITY_MIN_EDGE_CENTS,
    PAPER_ACTIVITY_MIN_TIMING_SCORE,
    PAPER_ACTIVITY_ORDER_STALE_SEC,
    PAPER_ACTIVITY_P_BOOK_MAX_DIVERGENCE,
    PAPER_ACTIVITY_P_BOOK_MIN_QUALITY,
    PAPER_ACTIVITY_PROFILE,
    PAPER_ACTIVITY_PROFILE_ENABLED,
    PAPER_ACTIVITY_REQUIRE_P_BOOK_CONFIRMATION,
)
from core.market_metadata import extract_suggested_strike
from data.kalshi_trading import (
    cancel_order,
    get_balance_summary,
    get_open_orders,
    get_positions,
    get_recent_fills,
    place_limit_order,
)
from engine.book_microstructure import get_last_p_book_snapshot
from engine.execution.metrics import (
    get_daily_realized_pnl_cents,
    record_execution_event,
    record_fill,
    record_paper_account_snapshot,
    record_policy_decision,
    record_realized_pnl_delta,
    record_window_participation,
    record_window_summary,
)
from engine.execution.models import ExecutionSignal
from engine.execution.paper_account import PaperAccount
from engine.execution.paper_models import PaperOrder
from engine.execution.paper_simulator import PaperFillSimulator
from engine.execution.policy import build_policy_decision
from engine.execution.risk import ExecutionRiskGuard
from engine.live_pricing import compute_live_pricing_snapshot
from engine.streamer import get_live_book, get_live_market_info
from feeds.brti_aggregator import get_brti_settlement_proxy

logger = logging.getLogger(__name__)


def _paper_activity_profile() -> dict[str, Any]:
    profile_name = str(PAPER_ACTIVITY_PROFILE or "high_activity").strip().lower()
    if profile_name not in {"high_activity", "balanced"}:
        profile_name = "high_activity"
    use_high_activity = bool(PAPER_ACTIVITY_PROFILE_ENABLED) and profile_name == "high_activity"

    if not use_high_activity:
        return {
            "enabled": False,
            "profile_name": profile_name,
            "allow_taker": False,
            "aggressive_edge_cents": 1_000_000.0,
            "order_stale_sec": float(EXECUTION_ORDER_STALE_SEC),
            "force_fill_per_window": False,
            "fallback_trigger_sec": float(PAPER_ACTIVITY_FALLBACK_TRIGGER_SEC),
            "fallback_retry_sec": float(PAPER_ACTIVITY_FALLBACK_RETRY_SEC),
            "fallback_min_edge_cents": float(PAPER_ACTIVITY_FALLBACK_MIN_EDGE_CENTS),
            "fallback_bypass_p_book": False,
            "policy_overrides": {
                "profile_name": profile_name,
                "min_probability": float(EXECUTION_MIN_PROBABILITY),
                "max_probability": float(EXECUTION_MAX_PROBABILITY),
                "min_confidence": float(EXECUTION_MIN_CONFIDENCE),
                "min_edge_cents": float(EXECUTION_MIN_EDGE_CENTS),
                "min_timing_score": float(EXECUTION_MIN_TIMING_SCORE),
                "require_p_book_confirmation": bool(EXECUTION_REQUIRE_P_BOOK_CONFIRMATION),
                "p_book_min_quality": float(EXECUTION_P_BOOK_MIN_QUALITY),
                "p_book_max_divergence": float(EXECUTION_P_BOOK_MAX_DIVERGENCE),
                "allow_taker": False,
                "aggressive_edge_cents": 1_000_000.0,
            },
        }

    return {
        "enabled": True,
        "profile_name": profile_name,
        "allow_taker": bool(PAPER_ACTIVITY_ALLOW_TAKER),
        "aggressive_edge_cents": float(PAPER_ACTIVITY_AGGRESSIVE_EDGE_CENTS),
        "order_stale_sec": float(PAPER_ACTIVITY_ORDER_STALE_SEC),
        "force_fill_per_window": bool(PAPER_ACTIVITY_FORCE_FILL_PER_WINDOW),
        "fallback_trigger_sec": float(PAPER_ACTIVITY_FALLBACK_TRIGGER_SEC),
        "fallback_retry_sec": float(PAPER_ACTIVITY_FALLBACK_RETRY_SEC),
        "fallback_min_edge_cents": float(PAPER_ACTIVITY_FALLBACK_MIN_EDGE_CENTS),
        "fallback_bypass_p_book": bool(PAPER_ACTIVITY_FALLBACK_BYPASS_P_BOOK),
        "policy_overrides": {
            "profile_name": profile_name,
            "min_probability": float(EXECUTION_MIN_PROBABILITY),
            "max_probability": float(EXECUTION_MAX_PROBABILITY),
            "min_confidence": float(PAPER_ACTIVITY_MIN_CONFIDENCE),
            "min_edge_cents": float(PAPER_ACTIVITY_MIN_EDGE_CENTS),
            "min_timing_score": float(PAPER_ACTIVITY_MIN_TIMING_SCORE),
            "require_p_book_confirmation": bool(PAPER_ACTIVITY_REQUIRE_P_BOOK_CONFIRMATION),
            "p_book_min_quality": float(PAPER_ACTIVITY_P_BOOK_MIN_QUALITY),
            "p_book_max_divergence": float(PAPER_ACTIVITY_P_BOOK_MAX_DIVERGENCE),
            "allow_taker": bool(PAPER_ACTIVITY_ALLOW_TAKER),
            "aggressive_edge_cents": float(PAPER_ACTIVITY_AGGRESSIVE_EDGE_CENTS),
        },
    }


def get_effective_execution_profile_snapshot() -> dict[str, Any]:
    return {
        "mode": EXECUTION_MODE,
        "paper_profile": _paper_activity_profile(),
    }

_execution_state_lock = RLock()
_execution_state: dict[str, Any] = {
    "enabled": bool(EXECUTION_ENABLED),
    "mode": EXECUTION_MODE,
    "env": KALSHI_ENV,
    "status": "booting",
    "last_reason": None,
    "last_error": None,
    "last_cycle_ts": None,
    "current_market_ticker": None,
    "open_orders": 0,
    "open_orders_total": 0,
    "market_position_contracts": 0,
    "daily_realized_pnl_cents": 0,
    "available_balance_cents": 0,
    "last_signal": None,
    "last_order": None,
    "paper_profile": _paper_activity_profile(),
    "paper_window": None,
    "paper_account": None,
}

_paper_account = PaperAccount()
_paper_simulator = PaperFillSimulator()


@dataclass
class _LoopState:
    market_ticker: str | None = None
    seen_fill_ids: set[str] = field(default_factory=set)
    signal_by_client_order_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_fill_poll_ts: int = field(default_factory=lambda: int(time.time()) - 120)
    last_realized_pnl_cents: int | None = None
    market_strike_by_ticker: dict[str, float] = field(default_factory=dict)
    paper_windows: dict[str, dict[str, Any]] = field(default_factory=dict)


def _set_execution_state(**kwargs: Any) -> None:
    with _execution_state_lock:
        _execution_state.update(kwargs)


def get_execution_state_snapshot() -> dict[str, Any]:
    with _execution_state_lock:
        return dict(_execution_state)


def _signal_payload(signal: ExecutionSignal | None) -> dict[str, Any] | None:
    if signal is None:
        return None
    return {
        "market_ticker": signal.market_ticker,
        "side": signal.side,
        "action": signal.action,
        "execution_intent": signal.execution_intent,
        "is_fallback_attempt": signal.is_fallback_attempt,
        "policy_profile": signal.policy_profile,
        "count": signal.count,
        "quote_price_cents": signal.quote_price_cents,
        "fair_price_cents": signal.fair_price_cents,
        "edge_cents": signal.edge_cents,
        "confidence": signal.confidence,
        "p_model": signal.p_model,
        "p_book": signal.p_book,
        "p_book_quality": signal.p_book_quality,
        "p_book_alignment": signal.p_book_alignment,
        "seconds_to_expiry": signal.seconds_to_expiry,
        "timing_score": signal.timing_score,
        "reason": signal.reason,
    }


def _safe_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _safe_float(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _value_ts(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        return float(value.timestamp())
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _order_price_for_side(order: dict[str, Any], side: str) -> int | None:
    key = "yes_price" if side == "yes" else "no_price"
    value = order.get(key)
    if isinstance(value, (int, float)):
        return int(value)
    return None


def _order_remaining_count(order: dict[str, Any]) -> int:
    value = order.get("remaining_count")
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, int(value))
    return 0


def _paper_order_age_seconds(order: PaperOrder) -> float:
    return max(0.0, time.time() - float(order.created_ts))


def _paper_order_matches_signal(order: PaperOrder, signal: ExecutionSignal) -> bool:
    return (
        order.market_ticker == signal.market_ticker
        and order.side == signal.side
        and order.action == signal.action
        and order.execution_intent == signal.execution_intent
        and int(order.remaining_count) == int(signal.count)
        and int(order.price_cents) == int(signal.quote_price_cents)
    )


def _market_position_contracts(positions: list[dict[str, Any]], market_ticker: str) -> int:
    total = 0
    for pos in positions:
        if str(pos.get("ticker") or "") != market_ticker:
            continue
        val = pos.get("position")
        if isinstance(val, (int, float)):
            total += int(val)
    return total


def _realized_pnl_total(positions: list[dict[str, Any]]) -> int:
    total = 0
    for pos in positions:
        val = pos.get("realized_pnl")
        if isinstance(val, (int, float)):
            total += int(val)
    return total


def _resting_contracts(open_orders: list[dict[str, Any]]) -> int:
    return sum(_order_remaining_count(order) for order in open_orders)


def _order_matches_signal(order: dict[str, Any], signal: ExecutionSignal) -> bool:
    if str(order.get("ticker") or "") != signal.market_ticker:
        return False
    if str(order.get("side") or "") != signal.side:
        return False
    if str(order.get("action") or "") != signal.action:
        return False

    px = _order_price_for_side(order, signal.side)
    if px is None:
        return False

    count = _order_remaining_count(order)
    return px == signal.quote_price_cents and count == signal.count


def _order_age_seconds(order: dict[str, Any]) -> float:
    created = _value_ts(order.get("created_time"))
    if created is None:
        return 0.0
    return max(0.0, time.time() - created)


async def _cancel_orders(orders: list[dict[str, Any]], reason: str) -> int:
    canceled = 0
    for order in orders:
        order_id = _safe_str(order.get("order_id"))
        if order_id is None:
            continue

        try:
            await asyncio.to_thread(cancel_order, order_id)
            canceled += 1
            record_execution_event(
                "order_canceled",
                {
                    "order_id": order_id,
                    "ticker": order.get("ticker"),
                    "side": order.get("side"),
                    "reason": reason,
                },
            )
        except Exception as exc:  # pragma: no cover - network failure path
            record_execution_event(
                "order_cancel_error",
                {
                    "order_id": order_id,
                    "ticker": order.get("ticker"),
                    "reason": reason,
                    "error": str(exc),
                },
            )
            logger.warning("Failed to cancel order %s: %s", order_id, exc)

    return canceled


def _cancel_paper_orders(orders: list[PaperOrder], reason: str) -> int:
    canceled = 0
    for order in orders:
        canceled_order = _paper_account.cancel_order(order.order_id, reason=reason)
        if canceled_order is None:
            continue
        canceled += 1
        record_execution_event(
            "paper_order_canceled",
            {
                "order_id": canceled_order.order_id,
                "ticker": canceled_order.market_ticker,
                "side": canceled_order.side,
                "reason": reason,
            },
        )
    return canceled


def _paper_fill_to_payload(fill) -> dict[str, Any]:
    yes_price = fill.price_cents if fill.side == "yes" else 100 - fill.price_cents
    no_price = fill.price_cents if fill.side == "no" else 100 - fill.price_cents
    return {
        "fill_id": fill.fill_id,
        "order_id": fill.order_id,
        "client_order_id": fill.client_order_id,
        "ticker": fill.market_ticker,
        "side": fill.side,
        "action": fill.action,
        "execution_intent": fill.execution_intent,
        "is_fallback_attempt": bool(fill.is_fallback_attempt),
        "window_id": fill.window_id,
        "ts": fill.ts,
        "count": fill.count,
        "yes_price": yes_price,
        "no_price": no_price,
        "is_taker": fill.is_taker,
        "fill_latency_ms": fill.fill_latency_ms,
        "expected_edge_cents": fill.expected_edge_cents,
        "confidence": fill.confidence,
        "seconds_to_expiry": fill.seconds_to_expiry,
        "paper_fill_reason": fill.reason,
        "paper_mode": True,
    }


def _paper_snapshot(curve_limit: int = 900) -> dict[str, Any]:
    snapshot = _paper_account.snapshot(curve_limit=curve_limit)
    record_paper_account_snapshot(snapshot)
    return snapshot


def _resolve_window_id(*, market_ticker: str, close_time_iso: str | None) -> str:
    if isinstance(close_time_iso, str) and close_time_iso.strip():
        return f"{market_ticker}|{close_time_iso.strip()}"
    return str(market_ticker)


def _ensure_paper_window_state(
    loop: _LoopState,
    *,
    market_ticker: str,
    close_time_iso: str | None,
    now_ts: float,
) -> dict[str, Any]:
    window_id = _resolve_window_id(market_ticker=market_ticker, close_time_iso=close_time_iso)
    existing = loop.paper_windows.get(window_id)
    if isinstance(existing, dict):
        existing["last_ts"] = float(now_ts)
        return existing

    state = {
        "window_id": window_id,
        "market_ticker": market_ticker,
        "close_time_iso": close_time_iso,
        "started_ts": float(now_ts),
        "last_ts": float(now_ts),
        "attempts": 0,
        "fallback_attempts": 0,
        "fills": 0,
        "has_fill": False,
        "first_fill_ts": None,
        "fallback_triggered": False,
        "last_fallback_attempt_ts": None,
        "settlement_realized_delta_cents": 0,
        "summary_emitted": False,
    }
    loop.paper_windows[window_id] = state
    return state


def _window_public_state(window_state: dict[str, Any], *, seconds_to_expiry: float | None = None) -> dict[str, Any]:
    return {
        "window_id": window_state.get("window_id"),
        "market_ticker": window_state.get("market_ticker"),
        "close_time_iso": window_state.get("close_time_iso"),
        "started_ts": window_state.get("started_ts"),
        "last_ts": window_state.get("last_ts"),
        "attempts": int(window_state.get("attempts", 0) or 0),
        "fallback_attempts": int(window_state.get("fallback_attempts", 0) or 0),
        "fills": int(window_state.get("fills", 0) or 0),
        "has_fill": bool(window_state.get("has_fill")),
        "first_fill_ts": window_state.get("first_fill_ts"),
        "fallback_triggered": bool(window_state.get("fallback_triggered")),
        "seconds_to_expiry": seconds_to_expiry,
    }


def _publish_window_participation(window_state: dict[str, Any], *, seconds_to_expiry: float | None = None) -> None:
    snapshot = _window_public_state(window_state, seconds_to_expiry=seconds_to_expiry)
    record_window_participation(snapshot)


def _mark_window_fill(window_state: dict[str, Any], *, fill_ts: float) -> None:
    window_state["fills"] = int(window_state.get("fills", 0) or 0) + 1
    window_state["has_fill"] = True
    if window_state.get("first_fill_ts") is None:
        window_state["first_fill_ts"] = float(fill_ts)


def _finalize_window_state(loop: _LoopState, *, window_id: str, reason: str, now_ts: float | None = None) -> None:
    state = loop.paper_windows.get(window_id)
    if not isinstance(state, dict):
        return
    if bool(state.get("summary_emitted")):
        return

    ts = time.time() if now_ts is None else float(now_ts)
    state["summary_emitted"] = True
    state["last_ts"] = ts
    summary = {
        "window_id": state.get("window_id"),
        "market_ticker": state.get("market_ticker"),
        "close_time_iso": state.get("close_time_iso"),
        "started_ts": state.get("started_ts"),
        "ended_ts": ts,
        "duration_sec": max(0.0, ts - float(state.get("started_ts", ts))),
        "attempts": int(state.get("attempts", 0) or 0),
        "fallback_attempts": int(state.get("fallback_attempts", 0) or 0),
        "fills": int(state.get("fills", 0) or 0),
        "has_fill": bool(state.get("has_fill")),
        "first_fill_ts": state.get("first_fill_ts"),
        "fallback_triggered": bool(state.get("fallback_triggered")),
        "settlement_realized_delta_cents": int(state.get("settlement_realized_delta_cents", 0) or 0),
        "finalize_reason": reason,
    }
    record_window_summary(summary)


def _current_p_book_quality() -> float | None:
    snapshot = get_last_p_book_snapshot()
    if not isinstance(snapshot, dict):
        return None

    quality = snapshot.get("p_book_quality")
    if quality is None:
        quality = snapshot.get("reliability")
    return _safe_float(quality)


def _simulate_paper_order_fill(order: PaperOrder) -> dict[str, Any] | None:
    decision = _paper_simulator.simulate(
        order=order,
        book=get_live_book(),
        p_book_quality=_current_p_book_quality(),
    )

    if not decision.would_fill or decision.fill_price_cents is None:
        return None

    fill = _paper_account.apply_fill(
        order=order,
        fill_count=decision.fill_count,
        fill_price_cents=decision.fill_price_cents,
        is_taker=decision.is_taker,
        reason=decision.reason,
        now_ts=decision.ts,
    )
    fill_payload = _paper_fill_to_payload(fill)
    fill_payload["best_bid_cents"] = decision.best_bid_cents
    fill_payload["best_ask_cents"] = decision.best_ask_cents
    fill_payload["spread_cents"] = decision.spread_cents
    fill_payload["price_deviation_cents"] = abs(float(fill.price_cents) - float(order.price_cents))

    record_fill(fill_payload)
    record_execution_event(
        "paper_fill",
        {
            "order_id": order.order_id,
            "fill_id": fill.fill_id,
            "ticker": fill.market_ticker,
            "side": fill.side,
            "execution_intent": fill.execution_intent,
            "is_fallback_attempt": bool(fill.is_fallback_attempt),
            "window_id": fill.window_id,
            "count": fill.count,
            "price_cents": fill.price_cents,
            "is_taker": fill.is_taker,
            "fill_latency_ms": fill.fill_latency_ms,
            "spread_cents": decision.spread_cents,
            "reason": fill.reason,
            "fill_probability": decision.fill_probability,
        },
    )
    return fill_payload


def _settle_paper_market(loop: _LoopState, market_ticker: str) -> int:
    profile = get_active_asset_context().profile
    settlement_proxy = get_brti_settlement_proxy(window_seconds=profile.settlement_window_seconds)
    settlement_avg = _safe_float(settlement_proxy.get("average"))

    strike = loop.market_strike_by_ticker.get(market_ticker)
    strike_cents = int(round(strike)) if isinstance(strike, (int, float)) else None

    realized_delta = _paper_account.settle_market(
        market_ticker=market_ticker,
        strike_cents=strike_cents,
        settlement_price_cents=settlement_avg,
    )
    if realized_delta != 0:
        record_realized_pnl_delta(
            delta_cents=realized_delta,
            source="paper_settlement",
            market_ticker=market_ticker,
        )

    record_execution_event(
        "paper_market_settled",
        {
            "ticker": market_ticker,
            "realized_delta_cents": realized_delta,
            "strike_cents": strike_cents,
            "settlement_avg": settlement_avg,
            "samples": settlement_proxy.get("samples"),
            "method": settlement_proxy.get("method"),
        },
    )
    return realized_delta


def _live_mode_allowed() -> bool:
    if EXECUTION_MODE != "live":
        return False
    if KALSHI_ENV == "prod":
        return True
    return bool(EXECUTION_ALLOW_LIVE_IN_DEMO_ENV)


async def _poll_and_record_fills(loop: _LoopState, market_ticker: str) -> None:
    try:
        fills = await asyncio.to_thread(
            get_recent_fills,
            market_ticker=market_ticker,
            min_ts=loop.last_fill_poll_ts,
            limit=200,
        )
    except Exception as exc:  # pragma: no cover - network failure path
        record_execution_event("fills_poll_error", {"error": str(exc), "ticker": market_ticker})
        logger.warning("Failed to poll fills for %s: %s", market_ticker, exc)
        return

    max_ts = loop.last_fill_poll_ts
    for fill in fills:
        fill_id = _safe_str(fill.get("fill_id") or fill.get("trade_id"))
        if fill_id is None or fill_id in loop.seen_fill_ids:
            continue

        loop.seen_fill_ids.add(fill_id)

        ts_val = fill.get("ts")
        if isinstance(ts_val, int):
            max_ts = max(max_ts, ts_val)

        client_order_id = _safe_str(fill.get("client_order_id"))
        signal_ctx = loop.signal_by_client_order_id.get(client_order_id or "", {})
        count = int(fill.get("count") or 0)
        fill_ts = _safe_float(fill.get("ts"))
        if isinstance(fill_ts, float) and fill_ts > 10_000_000_000:
            fill_ts = fill_ts / 1000.0

        expected_edge_per_contract = signal_ctx.get("edge_cents")
        expected_edge_cents = None
        if isinstance(expected_edge_per_contract, (int, float)):
            expected_edge_cents = round(float(expected_edge_per_contract) * max(0, count), 6)

        order_created_ts = signal_ctx.get("order_created_ts")
        fill_latency_ms = None
        if isinstance(order_created_ts, (int, float)) and isinstance(fill_ts, float):
            fill_latency_ms = round(max(0.0, (float(fill_ts) - float(order_created_ts)) * 1000.0), 3)

        quote_price = signal_ctx.get("quote_price_cents")
        yes_px = _safe_float(fill.get("yes_price"))
        no_px = _safe_float(fill.get("no_price"))
        fill_side = str(fill.get("side") or "").lower()
        fill_price = yes_px if fill_side == "yes" else no_px
        price_deviation = None
        if isinstance(fill_price, float) and isinstance(quote_price, (int, float)):
            price_deviation = round(abs(float(fill_price) - float(quote_price)), 6)

        payload = {
            "fill_id": fill_id,
            "order_id": fill.get("order_id"),
            "client_order_id": client_order_id,
            "ticker": fill.get("ticker"),
            "side": fill.get("side"),
            "action": fill.get("action"),
            "execution_intent": signal_ctx.get("execution_intent"),
            "is_fallback_attempt": bool(signal_ctx.get("is_fallback_attempt")),
            "window_id": signal_ctx.get("window_id"),
            "count": count,
            "yes_price": fill.get("yes_price"),
            "no_price": fill.get("no_price"),
            "is_taker": bool(fill.get("is_taker")),
            "fill_latency_ms": fill_latency_ms,
            "price_deviation_cents": price_deviation,
            "expected_edge_cents": expected_edge_cents,
            "confidence": signal_ctx.get("confidence"),
            "seconds_to_expiry": signal_ctx.get("seconds_to_expiry"),
        }
        record_fill(payload)

    if len(loop.seen_fill_ids) > 100_000:
        loop.seen_fill_ids.clear()

    loop.last_fill_poll_ts = max(max_ts, int(time.time()) - 2)


async def _run_single_cycle_observe(loop: _LoopState) -> None:
    market_info = get_live_market_info()
    market_ticker = _safe_str(market_info.get("ticker"))

    if market_ticker is None:
        _set_execution_state(
            status="observe_waiting_market",
            last_reason="no_active_market",
            paper_account=_paper_snapshot(curve_limit=400),
        )
        return

    if loop.market_ticker and loop.market_ticker != market_ticker:
        record_execution_event(
            "observe_market_rotated",
            {
                "previous_market_ticker": loop.market_ticker,
                "new_market_ticker": market_ticker,
            },
        )
        loop.signal_by_client_order_id.clear()

    loop.market_ticker = market_ticker
    strike = extract_suggested_strike(market_info)
    if isinstance(strike, (int, float)):
        loop.market_strike_by_ticker[market_ticker] = float(strike)

    close_time_iso = market_info.get("close_time") if isinstance(market_info.get("close_time"), str) else None
    pricing = compute_live_pricing_snapshot(
        strike=strike,
        market_ticker=market_ticker,
        close_time_iso=close_time_iso,
    )

    decision = build_policy_decision(
        pricing=pricing,
        market_ticker=market_ticker,
        book=get_live_book(),
    )

    signal_payload = _signal_payload(decision.signal)
    record_policy_decision(reason=decision.reason, signal_payload=signal_payload)
    if signal_payload is not None:
        record_execution_event("observe_signal", signal_payload)

    _set_execution_state(
        status="observe_signal_ready" if signal_payload else "observe_no_signal",
        last_reason=decision.reason,
        current_market_ticker=market_ticker,
        open_orders=0,
        open_orders_total=0,
        market_position_contracts=0,
        available_balance_cents=0,
        daily_realized_pnl_cents=get_daily_realized_pnl_cents(),
        last_signal=signal_payload,
        paper_account=_paper_snapshot(curve_limit=400),
        last_error=None,
    )


async def _run_single_cycle_paper(loop: _LoopState, guard: ExecutionRiskGuard) -> None:
    now_ts = time.time()
    paper_profile = _paper_activity_profile()
    market_info = get_live_market_info()
    market_ticker = _safe_str(market_info.get("ticker"))
    close_time_iso = market_info.get("close_time") if isinstance(market_info.get("close_time"), str) else None

    if market_ticker is None:
        if loop.market_ticker:
            pending_windows = [
                key
                for key, value in loop.paper_windows.items()
                if isinstance(value, dict)
                and str(value.get("market_ticker") or "") == str(loop.market_ticker)
                and not bool(value.get("summary_emitted"))
            ]
            for window_id in pending_windows:
                _finalize_window_state(loop, window_id=window_id, reason="market_unavailable", now_ts=now_ts)

        _set_execution_state(
            status="paper_waiting_market",
            last_reason="no_active_market",
            paper_profile=paper_profile,
            paper_window=None,
            paper_account=_paper_snapshot(curve_limit=500),
        )
        return

    if loop.market_ticker and loop.market_ticker != market_ticker:
        prior_market = loop.market_ticker
        prior_orders = _paper_account.list_open_orders(prior_market)
        canceled = _cancel_paper_orders(prior_orders, reason="market_rotation")
        realized_delta = _settle_paper_market(loop, prior_market)

        prior_window_candidates = [
            key
            for key, value in loop.paper_windows.items()
            if isinstance(value, dict)
            and str(value.get("market_ticker") or "") == prior_market
            and not bool(value.get("summary_emitted"))
        ]
        for window_id in prior_window_candidates:
            window_state = loop.paper_windows.get(window_id)
            if isinstance(window_state, dict):
                window_state["settlement_realized_delta_cents"] = int(realized_delta)
            _finalize_window_state(loop, window_id=window_id, reason="market_rotation", now_ts=now_ts)

        record_execution_event(
            "paper_market_rotated",
            {
                "previous_market_ticker": prior_market,
                "new_market_ticker": market_ticker,
                "canceled_orders": canceled,
                "realized_delta_cents": realized_delta,
            },
        )
        loop.signal_by_client_order_id.clear()

    loop.market_ticker = market_ticker
    window_state = _ensure_paper_window_state(
        loop,
        market_ticker=market_ticker,
        close_time_iso=close_time_iso,
        now_ts=now_ts,
    )
    window_id = str(window_state["window_id"])

    strike = extract_suggested_strike(market_info)
    if isinstance(strike, (int, float)):
        loop.market_strike_by_ticker[market_ticker] = float(strike)

    pricing = compute_live_pricing_snapshot(
        strike=strike,
        market_ticker=market_ticker,
        close_time_iso=close_time_iso,
    )

    seconds_to_expiry_value = _safe_float(pricing.get("seconds_to_expiry"))
    seconds_to_expiry = (
        max(0.0, float(seconds_to_expiry_value))
        if isinstance(seconds_to_expiry_value, float)
        else None
    )

    policy_overrides = dict(paper_profile.get("policy_overrides") or {})
    decision = build_policy_decision(
        pricing=pricing,
        market_ticker=market_ticker,
        book=get_live_book(),
        policy_overrides=policy_overrides,
    )

    signal = decision.signal
    fallback_armed = False
    if (
        bool(paper_profile.get("force_fill_per_window"))
        and not bool(window_state.get("has_fill"))
        and isinstance(seconds_to_expiry, float)
        and 0.0 < seconds_to_expiry <= float(paper_profile.get("fallback_trigger_sec", 0.0))
    ):
        last_fallback_attempt = window_state.get("last_fallback_attempt_ts")
        retry_sec = max(0.25, float(paper_profile.get("fallback_retry_sec", 1.0)))
        should_retry_fallback = (
            last_fallback_attempt is None
            or (now_ts - float(last_fallback_attempt)) >= retry_sec
        )
        if should_retry_fallback:
            fallback_armed = True
            window_state["fallback_triggered"] = True
            window_state["last_fallback_attempt_ts"] = now_ts

            fallback_overrides = dict(policy_overrides)
            fallback_overrides.update(
                {
                    "profile_name": "paper_activity_fallback",
                    "min_probability": 0.0,
                    "max_probability": 1.0,
                    "min_confidence": 0.0,
                    "min_timing_score": 0.0,
                    "min_edge_cents": float(paper_profile.get("fallback_min_edge_cents", 0.0)),
                    "allow_taker": True,
                    "aggressive_edge_cents": 0.0,
                }
            )
            if bool(paper_profile.get("fallback_bypass_p_book")):
                fallback_overrides["require_p_book_confirmation"] = False

            fallback_decision = build_policy_decision(
                pricing=pricing,
                market_ticker=market_ticker,
                book=get_live_book(),
                policy_overrides=fallback_overrides,
                force_fallback_attempt=True,
            )
            if fallback_decision.signal is not None or decision.signal is None:
                decision = fallback_decision
                signal = fallback_decision.signal

    signal_payload = _signal_payload(signal)
    record_policy_decision(reason=decision.reason, signal_payload=signal_payload)

    open_orders_market = _paper_account.list_open_orders(market_ticker)
    for order in list(open_orders_market):
        fill_payload = _simulate_paper_order_fill(order)
        if isinstance(fill_payload, dict):
            fill_ts = _safe_float(fill_payload.get("ts")) or now_ts
            _mark_window_fill(window_state, fill_ts=fill_ts)

    _paper_account.mark_to_market(market_ticker, get_live_book())

    open_orders_market = _paper_account.list_open_orders(market_ticker)
    open_orders_total = _paper_account.get_open_orders_total()
    market_position_contracts = _paper_account.get_market_position_contracts(market_ticker)
    resting_market_contracts = _paper_account.get_resting_contracts(market_ticker)
    available_balance = _paper_account.get_available_balance_cents()
    daily_pnl = _paper_account.get_daily_realized_pnl_cents()
    stale_sec = float(paper_profile.get("order_stale_sec", EXECUTION_ORDER_STALE_SEC))

    if signal is None:
        canceled = _cancel_paper_orders(open_orders_market, reason=decision.reason)
        snapshot = _paper_snapshot(curve_limit=900)
        _publish_window_participation(window_state, seconds_to_expiry=seconds_to_expiry)
        _set_execution_state(
            status="paper_no_signal",
            last_reason=decision.reason,
            current_market_ticker=market_ticker,
            open_orders=max(0, len(open_orders_market) - canceled),
            open_orders_total=max(0, open_orders_total - canceled),
            market_position_contracts=market_position_contracts,
            available_balance_cents=int(snapshot.get("available_balance_cents", available_balance)),
            daily_realized_pnl_cents=int(snapshot.get("daily_realized_pnl_cents", daily_pnl)),
            last_signal=None,
            paper_profile=paper_profile,
            paper_window=_window_public_state(window_state, seconds_to_expiry=seconds_to_expiry),
            paper_account=snapshot,
            last_error=None,
        )
        return

    risk = guard.can_place(
        signal=signal,
        market_position_contracts=market_position_contracts,
        resting_market_order_contracts=resting_market_contracts,
        open_orders_total=open_orders_total,
        available_balance_cents=available_balance,
        daily_realized_pnl_cents=daily_pnl,
    )
    if not risk.ok:
        if risk.reason in {"daily_loss_limit", "cash_buffer"}:
            _cancel_paper_orders(open_orders_market, reason=f"paper_risk_{risk.reason}")

        record_execution_event(
            "paper_risk_block",
            {
                "reason": risk.reason,
                "ticker": market_ticker,
                "side": signal.side,
                "count": signal.count,
                "quote_price_cents": signal.quote_price_cents,
                "execution_intent": signal.execution_intent,
                "is_fallback_attempt": bool(signal.is_fallback_attempt),
                "window_id": window_id,
            },
        )
        snapshot = _paper_snapshot(curve_limit=900)
        _publish_window_participation(window_state, seconds_to_expiry=seconds_to_expiry)
        _set_execution_state(
            status="paper_risk_block",
            last_reason=risk.reason,
            current_market_ticker=market_ticker,
            open_orders=len(open_orders_market),
            open_orders_total=open_orders_total,
            market_position_contracts=market_position_contracts,
            available_balance_cents=int(snapshot.get("available_balance_cents", available_balance)),
            daily_realized_pnl_cents=int(snapshot.get("daily_realized_pnl_cents", daily_pnl)),
            last_signal=signal_payload,
            paper_profile=paper_profile,
            paper_window=_window_public_state(window_state, seconds_to_expiry=seconds_to_expiry),
            paper_account=snapshot,
            last_error=None,
        )
        return

    matching_orders = [order for order in open_orders_market if _paper_order_matches_signal(order, signal)]
    stale_matching = [order for order in matching_orders if _paper_order_age_seconds(order) >= stale_sec]

    if matching_orders and not stale_matching:
        nonmatching = [order for order in open_orders_market if order not in matching_orders]
        if nonmatching:
            _cancel_paper_orders(nonmatching, reason="cleanup_nonmatching")

        snapshot = _paper_snapshot(curve_limit=900)
        _publish_window_participation(window_state, seconds_to_expiry=seconds_to_expiry)
        _set_execution_state(
            status="paper_resting_order_kept",
            last_reason="matching_resting_order",
            current_market_ticker=market_ticker,
            open_orders=len(matching_orders),
            open_orders_total=max(0, _paper_account.get_open_orders_total()),
            market_position_contracts=market_position_contracts,
            available_balance_cents=int(snapshot.get("available_balance_cents", available_balance)),
            daily_realized_pnl_cents=int(snapshot.get("daily_realized_pnl_cents", daily_pnl)),
            last_signal=signal_payload,
            paper_profile=paper_profile,
            paper_window=_window_public_state(window_state, seconds_to_expiry=seconds_to_expiry),
            paper_account=snapshot,
            last_error=None,
        )
        return

    cancel_targets = list(open_orders_market)
    if cancel_targets:
        _cancel_paper_orders(cancel_targets, reason="refresh_before_place")

    placed_order = _paper_account.place_order(signal, window_id=window_id)
    if placed_order is None:
        record_execution_event(
            "paper_order_rejected",
            {
                "ticker": market_ticker,
                "side": signal.side,
                "count": signal.count,
                "quote_price_cents": signal.quote_price_cents,
                "reason": "insufficient_cash",
                "execution_intent": signal.execution_intent,
                "is_fallback_attempt": bool(signal.is_fallback_attempt),
                "window_id": window_id,
            },
        )
        snapshot = _paper_snapshot(curve_limit=900)
        _publish_window_participation(window_state, seconds_to_expiry=seconds_to_expiry)
        _set_execution_state(
            status="paper_order_rejected",
            last_reason="insufficient_cash",
            current_market_ticker=market_ticker,
            open_orders=max(0, _paper_account.get_open_orders_total()),
            open_orders_total=max(0, _paper_account.get_open_orders_total()),
            market_position_contracts=market_position_contracts,
            available_balance_cents=int(snapshot.get("available_balance_cents", available_balance)),
            daily_realized_pnl_cents=int(snapshot.get("daily_realized_pnl_cents", daily_pnl)),
            last_signal=signal_payload,
            paper_profile=paper_profile,
            paper_window=_window_public_state(window_state, seconds_to_expiry=seconds_to_expiry),
            paper_account=snapshot,
            last_error=None,
        )
        return

    window_state["attempts"] = int(window_state.get("attempts", 0) or 0) + 1
    if bool(signal.is_fallback_attempt):
        window_state["fallback_attempts"] = int(window_state.get("fallback_attempts", 0) or 0) + 1

    loop.signal_by_client_order_id[placed_order.client_order_id] = {
        "edge_cents": signal.edge_cents,
        "confidence": signal.confidence,
        "seconds_to_expiry": signal.seconds_to_expiry,
        "side": signal.side,
        "execution_intent": signal.execution_intent,
        "is_fallback_attempt": bool(signal.is_fallback_attempt),
        "window_id": window_id,
        "quote_price_cents": signal.quote_price_cents,
        "order_created_ts": now_ts,
    }

    record_execution_event(
        "paper_order_submitted",
        {
            "ticker": signal.market_ticker,
            "order_id": placed_order.order_id,
            "client_order_id": placed_order.client_order_id,
            "side": signal.side,
            "action": signal.action,
            "execution_intent": signal.execution_intent,
            "is_fallback_attempt": bool(signal.is_fallback_attempt),
            "fallback_armed": bool(fallback_armed),
            "window_id": window_id,
            "count": signal.count,
            "quote_price_cents": signal.quote_price_cents,
            "edge_cents": signal.edge_cents,
            "confidence": signal.confidence,
            "timing_score": signal.timing_score,
        },
    )

    immediate_fill = _simulate_paper_order_fill(placed_order)
    if isinstance(immediate_fill, dict):
        fill_ts = _safe_float(immediate_fill.get("ts")) or now_ts
        _mark_window_fill(window_state, fill_ts=fill_ts)

    _paper_account.mark_to_market(market_ticker, get_live_book())

    snapshot = _paper_snapshot(curve_limit=900)
    _publish_window_participation(window_state, seconds_to_expiry=seconds_to_expiry)
    _set_execution_state(
        status="paper_order_submitted",
        last_reason="submitted",
        current_market_ticker=market_ticker,
        open_orders=int(snapshot.get("open_orders", _paper_account.get_open_orders_total())),
        open_orders_total=int(snapshot.get("open_orders", _paper_account.get_open_orders_total())),
        market_position_contracts=_paper_account.get_market_position_contracts(market_ticker),
        available_balance_cents=int(snapshot.get("available_balance_cents", available_balance)),
        daily_realized_pnl_cents=int(snapshot.get("daily_realized_pnl_cents", daily_pnl)),
        last_signal=signal_payload,
        last_order={
            "order_id": placed_order.order_id,
            "client_order_id": placed_order.client_order_id,
            "side": placed_order.side,
            "count": placed_order.count,
            "quote_price_cents": placed_order.price_cents,
            "execution_intent": placed_order.execution_intent,
            "is_fallback_attempt": bool(placed_order.is_fallback_attempt),
            "window_id": placed_order.window_id,
            "ts": time.time(),
        },
        paper_profile=paper_profile,
        paper_window=_window_public_state(window_state, seconds_to_expiry=seconds_to_expiry),
        paper_account=snapshot,
        last_error=None,
    )


async def _run_single_cycle(loop: _LoopState, guard: ExecutionRiskGuard) -> None:
    market_info = get_live_market_info()
    market_ticker = _safe_str(market_info.get("ticker"))

    if market_ticker is None:
        _set_execution_state(status="waiting_market", last_reason="no_active_market")
        return

    if loop.market_ticker and loop.market_ticker != market_ticker:
        try:
            old_orders = await asyncio.to_thread(get_open_orders, market_ticker=loop.market_ticker, limit=200)
            canceled = await _cancel_orders(old_orders, reason="market_rotation")
            record_execution_event(
                "market_rotated",
                {
                    "previous_market_ticker": loop.market_ticker,
                    "new_market_ticker": market_ticker,
                    "canceled_orders": canceled,
                },
            )
        except Exception as exc:  # pragma: no cover - network failure path
            record_execution_event("market_rotation_error", {"error": str(exc)})
            logger.warning("Failed market-rotation cleanup: %s", exc)

        loop.signal_by_client_order_id.clear()

    loop.market_ticker = market_ticker

    strike = extract_suggested_strike(market_info)
    if isinstance(strike, (int, float)):
        loop.market_strike_by_ticker[market_ticker] = float(strike)
    close_time_iso = market_info.get("close_time") if isinstance(market_info.get("close_time"), str) else None
    pricing = compute_live_pricing_snapshot(
        strike=strike,
        market_ticker=market_ticker,
        close_time_iso=close_time_iso,
    )

    decision = build_policy_decision(
        pricing=pricing,
        market_ticker=market_ticker,
        book=get_live_book(),
    )

    signal = decision.signal
    signal_payload = _signal_payload(signal)
    record_policy_decision(reason=decision.reason, signal_payload=signal_payload)

    try:
        balance, positions, open_orders_market, open_orders_total = await asyncio.gather(
            asyncio.to_thread(get_balance_summary),
            asyncio.to_thread(get_positions, market_ticker=market_ticker, limit=200),
            asyncio.to_thread(get_open_orders, market_ticker=market_ticker, limit=200),
            asyncio.to_thread(get_open_orders, market_ticker=None, limit=200),
        )
    except Exception as exc:  # pragma: no cover - network failure path
        _set_execution_state(status="data_error", last_reason="trading_data_fetch_failed", last_error=str(exc))
        record_execution_event(
            "trading_data_fetch_error",
            {"error": str(exc), "ticker": market_ticker},
        )
        logger.warning("Failed to collect trading runtime data: %s", exc)
        return

    await _poll_and_record_fills(loop, market_ticker)

    realized_total = _realized_pnl_total(positions)
    if loop.last_realized_pnl_cents is not None:
        delta = realized_total - loop.last_realized_pnl_cents
        if delta != 0:
            record_realized_pnl_delta(
                delta_cents=delta,
                source="positions",
                market_ticker=market_ticker,
            )
    loop.last_realized_pnl_cents = realized_total

    market_position_contracts = _market_position_contracts(positions, market_ticker)
    resting_market_contracts = _resting_contracts(open_orders_market)

    if signal is None:
        canceled = await _cancel_orders(open_orders_market, reason=decision.reason)
        _set_execution_state(
            status="no_signal",
            last_reason=decision.reason,
            current_market_ticker=market_ticker,
            open_orders=max(0, len(open_orders_market) - canceled),
            open_orders_total=max(0, len(open_orders_total) - canceled),
            market_position_contracts=market_position_contracts,
            available_balance_cents=int(balance.get("balance", 0)),
            daily_realized_pnl_cents=get_daily_realized_pnl_cents(),
            last_signal=None,
            paper_account=_paper_snapshot(curve_limit=900),
            last_error=None,
        )
        return

    risk = guard.can_place(
        signal=signal,
        market_position_contracts=market_position_contracts,
        resting_market_order_contracts=resting_market_contracts,
        open_orders_total=len(open_orders_total),
        available_balance_cents=int(balance.get("balance", 0)),
        daily_realized_pnl_cents=get_daily_realized_pnl_cents(),
    )

    if not risk.ok:
        if risk.reason in {"daily_loss_limit", "cash_buffer"}:
            await _cancel_orders(open_orders_market, reason=f"risk_{risk.reason}")

        record_execution_event(
            "risk_block",
            {
                "reason": risk.reason,
                "ticker": market_ticker,
                "side": signal.side,
                "count": signal.count,
                "quote_price_cents": signal.quote_price_cents,
            },
        )
        _set_execution_state(
            status="risk_block",
            last_reason=risk.reason,
            current_market_ticker=market_ticker,
            open_orders=len(open_orders_market),
            open_orders_total=len(open_orders_total),
            market_position_contracts=market_position_contracts,
            available_balance_cents=int(balance.get("balance", 0)),
            daily_realized_pnl_cents=get_daily_realized_pnl_cents(),
            last_signal=signal_payload,
            paper_account=_paper_snapshot(curve_limit=900),
            last_error=None,
        )
        return

    matching_orders = [order for order in open_orders_market if _order_matches_signal(order, signal)]
    stale_matching = [order for order in matching_orders if _order_age_seconds(order) >= EXECUTION_ORDER_STALE_SEC]

    if matching_orders and not stale_matching:
        nonmatching = [order for order in open_orders_market if order not in matching_orders]
        if nonmatching:
            await _cancel_orders(nonmatching, reason="cleanup_nonmatching")

        _set_execution_state(
            status="resting_order_kept",
            last_reason="matching_resting_order",
            current_market_ticker=market_ticker,
            open_orders=len(matching_orders),
            open_orders_total=max(0, len(open_orders_total) - len(nonmatching)),
            market_position_contracts=market_position_contracts,
            available_balance_cents=int(balance.get("balance", 0)),
            daily_realized_pnl_cents=get_daily_realized_pnl_cents(),
            last_signal=signal_payload,
            paper_account=_paper_snapshot(curve_limit=900),
            last_error=None,
        )
        return

    cancel_targets = list(open_orders_market)
    if cancel_targets:
        await _cancel_orders(cancel_targets, reason="refresh_before_place")

    try:
        placed = await asyncio.to_thread(
            place_limit_order,
            market_ticker=signal.market_ticker,
            side=signal.side,
            count=signal.count,
            price_cents=signal.quote_price_cents,
            post_only=(EXECUTION_POST_ONLY and signal.execution_intent != "taker"),
        )
    except Exception as exc:  # pragma: no cover - network failure path
        record_execution_event(
            "order_submit_error",
            {
                "ticker": market_ticker,
                "side": signal.side,
                "count": signal.count,
                "quote_price_cents": signal.quote_price_cents,
                "execution_intent": signal.execution_intent,
                "is_fallback_attempt": bool(signal.is_fallback_attempt),
                "error": str(exc),
            },
        )
        _set_execution_state(
            status="order_submit_error",
            last_reason="submit_failed",
            current_market_ticker=market_ticker,
            open_orders=0,
            open_orders_total=max(0, len(open_orders_total) - len(cancel_targets)),
            market_position_contracts=market_position_contracts,
            available_balance_cents=int(balance.get("balance", 0)),
            daily_realized_pnl_cents=get_daily_realized_pnl_cents(),
            last_signal=signal_payload,
            paper_account=_paper_snapshot(curve_limit=900),
            last_error=str(exc),
        )
        logger.warning("Order submission failed: %s", exc)
        return

    order = placed.get("order") if isinstance(placed, dict) else None
    order_id = _safe_str(order.get("order_id")) if isinstance(order, dict) else None
    client_order_id = _safe_str(placed.get("client_order_id")) if isinstance(placed, dict) else None

    if client_order_id:
        loop.signal_by_client_order_id[client_order_id] = {
            "edge_cents": signal.edge_cents,
            "confidence": signal.confidence,
            "seconds_to_expiry": signal.seconds_to_expiry,
            "side": signal.side,
            "execution_intent": signal.execution_intent,
            "is_fallback_attempt": bool(signal.is_fallback_attempt),
            "quote_price_cents": signal.quote_price_cents,
            "order_created_ts": time.time(),
        }

    record_execution_event(
        "order_submitted",
        {
            "ticker": signal.market_ticker,
            "order_id": order_id,
            "client_order_id": client_order_id,
            "side": signal.side,
            "action": signal.action,
            "execution_intent": signal.execution_intent,
            "is_fallback_attempt": bool(signal.is_fallback_attempt),
            "count": signal.count,
            "quote_price_cents": signal.quote_price_cents,
            "edge_cents": signal.edge_cents,
            "confidence": signal.confidence,
            "timing_score": signal.timing_score,
        },
    )

    _set_execution_state(
        status="order_submitted",
        last_reason="submitted",
        current_market_ticker=market_ticker,
        open_orders=1,
        open_orders_total=max(1, len(open_orders_total) - len(cancel_targets) + 1),
        market_position_contracts=market_position_contracts,
        available_balance_cents=int(balance.get("balance", 0)),
        daily_realized_pnl_cents=get_daily_realized_pnl_cents(),
        last_signal=signal_payload,
        last_order={
            "order_id": order_id,
            "client_order_id": client_order_id,
            "side": signal.side,
            "count": signal.count,
            "quote_price_cents": signal.quote_price_cents,
            "execution_intent": signal.execution_intent,
            "is_fallback_attempt": bool(signal.is_fallback_attempt),
            "ts": time.time(),
        },
        paper_account=_paper_snapshot(curve_limit=900),
        last_error=None,
    )


async def run_live_execution_loop() -> None:
    """Autonomous execution loop that runs beside streamer + index services."""
    guard = ExecutionRiskGuard()
    loop_state = _LoopState()

    while True:
        cycle_started = time.time()

        if not EXECUTION_ENABLED:
            _set_execution_state(
                status="disabled",
                last_reason="execution_disabled",
                mode=EXECUTION_MODE,
                paper_profile=_paper_activity_profile(),
                last_cycle_ts=cycle_started,
                paper_account=_paper_snapshot(curve_limit=500),
            )
            await asyncio.sleep(1.0)
            continue

        if EXECUTION_MODE == "live" and not _live_mode_allowed():
            _set_execution_state(
                status="blocked",
                last_reason="execution_mode_live_requires_prod_env_or_demo_override",
                mode=EXECUTION_MODE,
                paper_profile=_paper_activity_profile(),
                last_cycle_ts=cycle_started,
                paper_account=_paper_snapshot(curve_limit=500),
            )
            await asyncio.sleep(2.0)
            continue

        try:
            if EXECUTION_MODE == "observe":
                await _run_single_cycle_observe(loop_state)
            elif EXECUTION_MODE == "paper":
                await _run_single_cycle_paper(loop_state, guard)
            else:
                await _run_single_cycle(loop_state, guard)
        except Exception as exc:  # pragma: no cover - top-level safety net
            logger.exception("Execution loop cycle failed: %s", exc)
            record_execution_event("execution_cycle_error", {"error": str(exc)})
            _set_execution_state(
                status="cycle_error",
                last_reason="unexpected_exception",
                mode=EXECUTION_MODE,
                paper_profile=_paper_activity_profile(),
                paper_account=_paper_snapshot(curve_limit=500),
                last_error=str(exc),
            )

        _set_execution_state(
            last_cycle_ts=cycle_started,
            mode=EXECUTION_MODE,
            paper_profile=_paper_activity_profile(),
        )
        elapsed = max(0.0, time.time() - cycle_started)
        sleep_for = max(0.05, EXECUTION_LOOP_INTERVAL_SEC - elapsed)
        await asyncio.sleep(sleep_for)
