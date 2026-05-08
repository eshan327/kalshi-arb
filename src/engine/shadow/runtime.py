from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from threading import RLock
from typing import Any

from core.asset_context import get_active_asset_context
from core.config import EXECUTION_EVENTS_MAXLEN, EXECUTION_EVENTS_PATH, EXECUTION_LOOP_INTERVAL_SEC
from core.market_metadata import extract_suggested_strike
from data.kalshi_trading import place_limit_order
from engine.execution.persistence import append_jsonl
from engine.live_pricing import compute_live_pricing_snapshot
from engine.shadow.events import build_shadow_event
from engine.shadow.fee_model import taker_fee_cents_per_contract
from engine.shadow.fill_model import simulate_taker_fill
from engine.shadow.paper_ledger import PaperLedger
from engine.shadow.settings_state import (
    get_shadow_settings_model,
    get_shadow_settings_snapshot,
    resolve_effective_mode,
)
from engine.shadow.signal_engine import apply_pricing_overrides, build_shadow_signal
from engine.streamer import get_live_book, get_live_market_info
from feeds.brti_aggregator import get_brti_settlement_proxy

logger = logging.getLogger(__name__)

_runtime_lock = RLock()
_event_log: deque[dict[str, Any]] = deque(maxlen=max(500, int(EXECUTION_EVENTS_MAXLEN)))

_last_market_ticker: str | None = None
_market_strikes: dict[str, float] = {}

_settings_boot = get_shadow_settings_model()
_ledger = PaperLedger(starting_bankroll_usd=float(_settings_boot.bankroll_usd))

_runtime_state: dict[str, Any] = {
    "status": "booting",
    "last_reason": None,
    "last_error": None,
    "last_cycle_ts": None,
    "current_market_ticker": None,
    "requested_mode": None,
    "effective_mode": None,
    "settings": get_shadow_settings_snapshot(),
    "last_signal": None,
    "last_event": None,
    "paper_ledger": _ledger.snapshot(curve_limit=300),
}


def _safe_float(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _set_state(**kwargs: Any) -> None:
    with _runtime_lock:
        _runtime_state.update(kwargs)


def _signal_payload(signal: Any) -> dict[str, Any] | None:
    if signal is None:
        return None
    return {
        "ts": signal.ts,
        "market_ticker": signal.market_ticker,
        "side": signal.side,
        "intent": signal.intent,
        "count": signal.count,
        "quote_price_cents": signal.quote_price_cents,
        "fair_price_cents": signal.fair_price_cents,
        "edge_cents": signal.edge_cents,
        "edge_probability": signal.edge_probability,
        "confidence": signal.confidence,
        "model_probability": signal.model_probability,
        "market_implied_probability": signal.market_implied_probability,
        "reason": signal.reason,
        "diagnostics": dict(signal.diagnostics),
    }


def _emit_event(event: dict[str, Any]) -> None:
    with _runtime_lock:
        _event_log.append(dict(event))
        _runtime_state["last_event"] = dict(event)

    try:
        append_jsonl(EXECUTION_EVENTS_PATH, dict(event))
    except Exception as exc:  # pragma: no cover - persistence best effort
        logger.warning("Failed to persist shadow event: %s", exc)


def get_shadow_runtime_snapshot() -> dict[str, Any]:
    with _runtime_lock:
        state = dict(_runtime_state)
    state["paper_ledger"] = _ledger.snapshot(curve_limit=600)
    return state


def get_shadow_events(limit: int = 200) -> list[dict[str, Any]]:
    with _runtime_lock:
        return list(_event_log)[-max(1, int(limit)):]


def get_shadow_ledger_snapshot(curve_limit: int = 1000) -> dict[str, Any]:
    return _ledger.snapshot(curve_limit=curve_limit)


def reset_shadow_ledger() -> dict[str, Any]:
    global _ledger
    settings = get_shadow_settings_model()
    with _runtime_lock:
        _ledger = PaperLedger(starting_bankroll_usd=float(settings.bankroll_usd))
    snapshot = _ledger.snapshot(curve_limit=300)
    _set_state(paper_ledger=snapshot)
    return snapshot


async def _settle_rotated_market(previous_ticker: str, now_ts: float) -> None:
    if not previous_ticker:
        return

    strike = _market_strikes.get(previous_ticker)
    profile = get_active_asset_context().profile
    settlement_proxy = get_brti_settlement_proxy(window_seconds=profile.settlement_window_seconds)
    settlement_avg = _safe_float(settlement_proxy.get("average"))

    result = _ledger.settle_market(
        market_ticker=previous_ticker,
        strike_cents=strike,
        settlement_price_cents=settlement_avg,
        now_ts=now_ts,
    )

    event = build_shadow_event(
        kind="settlement",
        reason="market_rotation",
        intent="settlement",
        market_ticker=previous_ticker,
        side=None,
        count=int(result.get("closed_contracts") or 0),
        fill_price_cents=None,
        slippage_cents=0.0,
        is_synthetic_fill=True,
        now_ts=now_ts,
        extra={
            "strike_cents": strike,
            "settlement_price_cents": settlement_avg,
            "realized_delta_cents": result.get("realized_delta_cents"),
        },
    )
    _emit_event(event)


async def _run_single_cycle() -> None:
    global _last_market_ticker

    cycle_ts = time.time()
    settings = get_shadow_settings_model()
    settings_snapshot = get_shadow_settings_snapshot()
    effective_mode, mode_reason = resolve_effective_mode(settings.execution_mode)

    market_info = get_live_market_info()
    market_ticker = str(market_info.get("ticker") or "").strip() or None
    close_time_iso = market_info.get("close_time") if isinstance(market_info.get("close_time"), str) else None

    if market_ticker is None:
        _set_state(
            status="waiting_market",
            last_reason="no_active_market",
            requested_mode=settings.execution_mode,
            effective_mode=effective_mode,
            settings=settings_snapshot,
            paper_ledger=_ledger.snapshot(curve_limit=300),
        )
        return

    if _last_market_ticker and _last_market_ticker != market_ticker:
        await _settle_rotated_market(_last_market_ticker, cycle_ts)

    _last_market_ticker = market_ticker

    strike = extract_suggested_strike(market_info)
    if isinstance(strike, (int, float)):
        _market_strikes[market_ticker] = float(strike)

    pricing = compute_live_pricing_snapshot(
        strike=strike,
        market_ticker=market_ticker,
        close_time_iso=close_time_iso,
    )
    pricing = apply_pricing_overrides(pricing, settings)

    _ledger.mark_to_market(market_ticker=market_ticker, book=get_live_book(), now_ts=cycle_ts)
    bankroll_cents = _ledger.current_bankroll_cents()

    signal, decision_reason, diagnostics = build_shadow_signal(
        pricing=pricing,
        market_ticker=market_ticker,
        book=get_live_book(),
        settings=settings,
        bankroll_cents=bankroll_cents,
        now_ts=cycle_ts,
    )

    signal_payload = _signal_payload(signal)

    if signal is None:
        _set_state(
            status="no_signal",
            last_reason=decision_reason,
            requested_mode=settings.execution_mode,
            effective_mode=effective_mode,
            mode_reason=mode_reason,
            current_market_ticker=market_ticker,
            settings=settings_snapshot,
            last_signal=None,
            paper_ledger=_ledger.snapshot(curve_limit=300),
            pricing=pricing,
            diagnostics=diagnostics,
        )
        return

    if effective_mode == "observe":
        rejection = build_shadow_event(
            kind="rejection",
            reason="observe_mode_signal_only",
            intent=signal.intent,
            market_ticker=signal.market_ticker,
            side=signal.side,
            count=signal.count,
            fill_price_cents=None,
            slippage_cents=0.0,
            is_synthetic_fill=False,
            now_ts=cycle_ts,
            extra={
                "edge_cents": signal.edge_cents,
                "model_probability": signal.model_probability,
            },
        )
        _emit_event(rejection)
        _set_state(
            status="observe_signal",
            last_reason=rejection["reason"],
            requested_mode=settings.execution_mode,
            effective_mode=effective_mode,
            mode_reason=mode_reason,
            current_market_ticker=market_ticker,
            settings=settings_snapshot,
            last_signal=signal_payload,
            paper_ledger=_ledger.snapshot(curve_limit=300),
            pricing=pricing,
            diagnostics=diagnostics,
        )
        return

    if effective_mode == "paper":
        order_event = build_shadow_event(
            kind="order",
            reason="paper_order_submitted",
            intent=signal.intent,
            market_ticker=signal.market_ticker,
            side=signal.side,
            count=signal.count,
            fill_price_cents=None,
            slippage_cents=0.0,
            is_synthetic_fill=True,
            now_ts=cycle_ts,
            extra={
                "quote_price_cents": signal.quote_price_cents,
                "edge_cents": signal.edge_cents,
            },
        )
        _emit_event(order_event)

        fill_quote = simulate_taker_fill(
            book=get_live_book(),
            side=signal.side,
            slippage_ticks=settings.slippage_ticks,
            now_ts=cycle_ts,
        )

        if not fill_quote.can_fill or fill_quote.fill_price_cents is None:
            rejection = build_shadow_event(
                kind="rejection",
                reason=fill_quote.reason,
                intent=signal.intent,
                market_ticker=signal.market_ticker,
                side=signal.side,
                count=signal.count,
                fill_price_cents=None,
                slippage_cents=0.0,
                is_synthetic_fill=True,
                now_ts=cycle_ts,
                extra={
                    "best_bid_cents": fill_quote.best_bid_cents,
                    "best_ask_cents": fill_quote.best_ask_cents,
                },
            )
            _emit_event(rejection)
            _set_state(
                status="paper_rejected",
                last_reason=rejection["reason"],
                requested_mode=settings.execution_mode,
                effective_mode=effective_mode,
                mode_reason=mode_reason,
                current_market_ticker=market_ticker,
                settings=settings_snapshot,
                last_signal=signal_payload,
                paper_ledger=_ledger.snapshot(curve_limit=300),
                pricing=pricing,
                diagnostics=diagnostics,
            )
            return

        fee_per_contract = taker_fee_cents_per_contract(fill_quote.fill_price_cents, settings.taker_fee_curve_coeff)
        fee_total = fee_per_contract * float(signal.count)
        expected_edge_total = float(signal.edge_cents) * float(signal.count)

        fill_apply = _ledger.apply_fill(
            market_ticker=signal.market_ticker,
            side=signal.side,
            contracts=signal.count,
            fill_price_cents=fill_quote.fill_price_cents,
            fee_total_cents=fee_total,
            expected_edge_cents=expected_edge_total,
            now_ts=cycle_ts,
        )

        if fill_apply is None:
            rejection = build_shadow_event(
                kind="rejection",
                reason="insufficient_cash",
                intent=signal.intent,
                market_ticker=signal.market_ticker,
                side=signal.side,
                count=signal.count,
                fill_price_cents=fill_quote.fill_price_cents,
                slippage_cents=fill_quote.slippage_cents,
                is_synthetic_fill=True,
                now_ts=cycle_ts,
            )
            _emit_event(rejection)
            _set_state(
                status="paper_rejected",
                last_reason=rejection["reason"],
                requested_mode=settings.execution_mode,
                effective_mode=effective_mode,
                mode_reason=mode_reason,
                current_market_ticker=market_ticker,
                settings=settings_snapshot,
                last_signal=signal_payload,
                paper_ledger=_ledger.snapshot(curve_limit=300),
                pricing=pricing,
                diagnostics=diagnostics,
            )
            return

        fill_event = build_shadow_event(
            kind="fill",
            reason=fill_quote.reason,
            intent=signal.intent,
            market_ticker=signal.market_ticker,
            side=signal.side,
            count=signal.count,
            fill_price_cents=fill_quote.fill_price_cents,
            slippage_cents=fill_quote.slippage_cents,
            is_synthetic_fill=True,
            now_ts=cycle_ts,
            extra={
                "best_bid_cents": fill_quote.best_bid_cents,
                "best_ask_cents": fill_quote.best_ask_cents,
                "spread_cents": fill_quote.spread_cents,
                "quote_price_cents": signal.quote_price_cents,
                "fee_total_cents": round(fee_total, 6),
                "edge_cents": signal.edge_cents,
            },
        )
        _emit_event(fill_event)

        _set_state(
            status="paper_filled",
            last_reason=fill_event["reason"],
            requested_mode=settings.execution_mode,
            effective_mode=effective_mode,
            mode_reason=mode_reason,
            current_market_ticker=market_ticker,
            settings=settings_snapshot,
            last_signal=signal_payload,
            paper_ledger=_ledger.snapshot(curve_limit=300),
            pricing=pricing,
            diagnostics=diagnostics,
        )
        return

    # effective_mode == live
    try:
        placed = await asyncio.to_thread(
            place_limit_order,
            market_ticker=signal.market_ticker,
            side=signal.side,
            count=signal.count,
            price_cents=signal.quote_price_cents,
            post_only=False,
        )
        order_obj = placed.get("order") if isinstance(placed, dict) else {}
        order_id = order_obj.get("order_id") if isinstance(order_obj, dict) else None

        live_event = build_shadow_event(
            kind="order",
            reason="live_order_submitted",
            intent=signal.intent,
            market_ticker=signal.market_ticker,
            side=signal.side,
            count=signal.count,
            fill_price_cents=None,
            slippage_cents=0.0,
            is_synthetic_fill=False,
            now_ts=cycle_ts,
            extra={
                "order_id": order_id,
                "quote_price_cents": signal.quote_price_cents,
                "edge_cents": signal.edge_cents,
            },
        )
        _emit_event(live_event)
        _set_state(
            status="live_order_submitted",
            last_reason=live_event["reason"],
            requested_mode=settings.execution_mode,
            effective_mode=effective_mode,
            mode_reason=mode_reason,
            current_market_ticker=market_ticker,
            settings=settings_snapshot,
            last_signal=signal_payload,
            paper_ledger=_ledger.snapshot(curve_limit=300),
            pricing=pricing,
            diagnostics=diagnostics,
        )
    except Exception as exc:  # pragma: no cover - network/api failure path
        rejection = build_shadow_event(
            kind="rejection",
            reason="live_order_failed",
            intent=signal.intent,
            market_ticker=signal.market_ticker,
            side=signal.side,
            count=signal.count,
            fill_price_cents=None,
            slippage_cents=0.0,
            is_synthetic_fill=False,
            now_ts=cycle_ts,
            extra={"error": str(exc)},
        )
        _emit_event(rejection)
        _set_state(
            status="live_order_error",
            last_reason=rejection["reason"],
            last_error=str(exc),
            requested_mode=settings.execution_mode,
            effective_mode=effective_mode,
            mode_reason=mode_reason,
            current_market_ticker=market_ticker,
            settings=settings_snapshot,
            last_signal=signal_payload,
            paper_ledger=_ledger.snapshot(curve_limit=300),
            pricing=pricing,
            diagnostics=diagnostics,
        )


async def run_shadow_trading_loop() -> None:
    """Background shadow-trading loop using live production orderbooks and paper fills."""
    while True:
        cycle_started = time.time()
        try:
            await _run_single_cycle()
        except Exception as exc:  # pragma: no cover - top-level safeguard
            logger.exception("Shadow runtime cycle failed: %s", exc)
            _set_state(status="cycle_error", last_reason="unexpected_exception", last_error=str(exc))

        _set_state(last_cycle_ts=cycle_started)
        elapsed = max(0.0, time.time() - cycle_started)
        await asyncio.sleep(max(0.05, float(EXECUTION_LOOP_INTERVAL_SEC) - elapsed))
