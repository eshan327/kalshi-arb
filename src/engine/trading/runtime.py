from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from threading import RLock
from typing import Any
from zoneinfo import ZoneInfo

from core.asset_context import get_active_asset_context
from core.config import (
    EXECUTION_EVENTS_MAXLEN,
    EXECUTION_EVENTS_PATH,
    EXECUTION_LOOP_INTERVAL_SEC,
    EXECUTION_STATE_PATH,
    KALSHI_ENV,
    PAPER_STARTING_CASH_CENTS,
)
from core.market_metadata import extract_suggested_strike
from data.kalshi_rest import get_event, get_market, get_series
from data.kalshi_trading import (
    cancel_bot_orders,
    get_balance_summary,
    get_positions,
    place_limit_order,
    set_live_order_entry_enabled,
)
from engine.live_pricing import compute_live_pricing_snapshot
from engine.streamer import get_live_book, get_live_market_info
from engine.trading.fees import taker_fee_cents_per_contract
from engine.trading.models import TradeSignal
from engine.trading.paper import PaperAccount
from engine.trading.settings import (
    get_trading_settings_model,
    get_trading_settings_snapshot,
)
from engine.trading.strategy import (
    MAX_ORDERBOOK_AGE_SECONDS,
    apply_pricing_overrides,
    build_trade_signal,
    slipped_price_cents,
)

logger = logging.getLogger(__name__)
_NY = ZoneInfo("America/New_York")

_lock = RLock()
_execution_lock = RLock()
_events: deque[dict[str, Any]] = deque(maxlen=max(500, EXECUTION_EVENTS_MAXLEN))
_runtime_started_ts = time.time()
_execution_mode: str | None = None
_armed = False
_last_submission_ts = 0.0
_last_market_ticker: str | None = None
_risk_states: dict[str, dict[str, Any]] = {}
_paper_account = PaperAccount(PAPER_STARTING_CASH_CENTS)

_runtime_state: dict[str, Any] = {
    "status": "stopped",
    "armed": False,
    "execution_mode": None,
    "kalshi_env": KALSHI_ENV,
    "last_reason": None,
    "last_error": None,
    "last_cycle_ts": None,
    "current_market_ticker": None,
    "last_signal": None,
    "last_order": None,
    "account": {},
    "daily_risk": {},
    "fee_policy": {},
    "settings": get_trading_settings_snapshot(),
    "signal_monologue": {"action_intent": "Choose Paper or Live to start."},
}


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _set_state(**kwargs: Any) -> None:
    with _lock:
        _runtime_state.update(kwargs)


def _set_armed(value: bool) -> None:
    global _armed
    with _lock:
        _armed = bool(value)
        _runtime_state["armed"] = _armed
        live_enabled = _armed and _execution_mode == "live"
    set_live_order_entry_enabled(live_enabled)


def _is_armed() -> bool:
    with _lock:
        return _armed


def _get_execution_mode() -> str | None:
    with _lock:
        return _execution_mode


def _set_execution_mode(value: str) -> None:
    global _execution_mode, _last_submission_ts
    with _lock:
        _execution_mode = value
        _last_submission_ts = 0.0
        _runtime_state["execution_mode"] = value
    set_live_order_entry_enabled(False)


def _position_payload(raw: dict[str, Any]) -> dict[str, Any] | None:
    signed = _decimal(raw.get("position_fp", raw.get("position")))
    if signed == 0:
        return None
    contracts = abs(signed)
    exposure_cents = abs(_decimal(raw.get("market_exposure_dollars"))) * 100
    avg_entry = exposure_cents / contracts if contracts else Decimal("0")
    return {
        "market_ticker": str(raw.get("ticker") or ""),
        "side": "yes" if signed > 0 else "no",
        "contracts": float(contracts),
        "strategy_contracts": int(contracts),
        "avg_entry_cents": round(float(avg_entry), 4),
        "market_exposure_cents": round(float(exposure_cents), 4),
        "realized_pnl_cents": round(
            float(_decimal(raw.get("realized_pnl_dollars")) * 100), 4
        ),
        "fees_paid_cents": round(
            float(_decimal(raw.get("fees_paid_dollars")) * 100), 4
        ),
    }


def _fetch_account_snapshot(execution_mode: str | None = None) -> dict[str, Any]:
    mode = execution_mode or _get_execution_mode()
    if mode == "paper":
        return _paper_account.snapshot()
    if mode != "live":
        raise RuntimeError("Choose Paper or Live first.")
    balance = get_balance_summary()
    positions = [
        position
        for raw in get_positions()
        if (position := _position_payload(raw)) is not None
    ]
    cash_cents = int(balance["balance"])
    portfolio_value_cents = int(balance["portfolio_value"])
    return {
        "cash_cents": cash_cents,
        "portfolio_value_cents": portfolio_value_cents,
        "equity_cents": cash_cents + portfolio_value_cents,
        "positions": positions,
        "updated_ts": int(balance["updated_ts"]),
        "refreshed_ts": time.time(),
    }


def _risk_day() -> str:
    return datetime.now(_NY).date().isoformat()


def _load_risk_state(execution_mode: str) -> dict[str, Any]:
    path = _risk_state_path(execution_mode)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_risk_state(execution_mode: str) -> None:
    path = _risk_state_path(execution_mode)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    temp_path.write_text(
        json.dumps(_risk_states[execution_mode], separators=(",", ":")),
        encoding="utf-8",
    )
    temp_path.replace(path)


def _risk_state_path(execution_mode: str) -> Path:
    return Path(
        EXECUTION_STATE_PATH
        if execution_mode == "live"
        else f"{EXECUTION_STATE_PATH}.paper"
    )


def _sync_daily_risk(
    equity_cents: int, max_daily_loss_usd: float, execution_mode: str | None = None
) -> tuple[dict[str, Any], bool]:
    mode = execution_mode or _get_execution_mode()
    if mode not in {"paper", "live"}:
        raise RuntimeError("Choose Paper or Live first.")
    with _lock:
        day = _risk_day()
        risk_state = _risk_states.get(mode) or _load_risk_state(mode)
        if risk_state.get("day") != day:
            risk_state = {
                "day": day,
                "start_equity_cents": int(equity_cents),
                "locked": False,
            }
            _risk_states[mode] = risk_state
            _save_risk_state(mode)
        else:
            _risk_states[mode] = risk_state

        start = int(risk_state.get("start_equity_cents", equity_cents))
        drawdown = int(equity_cents) - start
        was_locked = bool(risk_state.get("locked"))
        if drawdown <= -round(float(max_daily_loss_usd) * 100):
            risk_state["locked"] = True
        if bool(risk_state.get("locked")) != was_locked:
            _save_risk_state(mode)

        snapshot = {
            **risk_state,
            "current_equity_cents": int(equity_cents),
            "drawdown_cents": int(drawdown),
            "max_daily_loss_cents": round(float(max_daily_loss_usd) * 100),
        }
        return snapshot, bool(snapshot["locked"] and not was_locked)


def _market_position(
    account: dict[str, Any], market_ticker: str
) -> dict[str, Any] | None:
    for position in account.get("positions", []):
        if position.get("market_ticker") == market_ticker:
            return position
    return None


def _position_inputs(
    account: dict[str, Any], market_ticker: str
) -> tuple[int, int, float | None, float | None]:
    position = _market_position(account, market_ticker)
    if not position:
        return 0, 0, None, None
    contracts = int(position.get("strategy_contracts") or 0)
    avg_entry = float(position.get("avg_entry_cents") or 0.0)
    if position.get("side") == "yes":
        return contracts, 0, avg_entry, None
    return 0, contracts, None, avg_entry


def _signal_payload(signal: TradeSignal | None) -> dict[str, Any] | None:
    if signal is None:
        return None
    return {
        "ts": signal.ts,
        "market_ticker": signal.market_ticker,
        "side": signal.side,
        "action": signal.action,
        "count": signal.count,
        "quote_price_cents": signal.quote_price_cents,
        "fair_price_cents": signal.fair_price_cents,
        "credit_cents": signal.credit_cents,
        "edge_cents": signal.edge_cents,
        "edge_probability": signal.edge_probability,
        "confidence": signal.confidence,
        "model_probability": signal.model_probability,
        "market_implied_probability": signal.market_implied_probability,
        "reason": signal.reason,
        "diagnostics": dict(signal.diagnostics),
    }


def _monologue(
    signal: TradeSignal | None,
    reason: str,
    pricing: dict[str, Any],
    diagnostics: dict[str, Any],
) -> dict[str, Any]:
    p_model = pricing.get("p_model")
    edge_yes = diagnostics.get("edge_yes_cents")
    edge_no = diagnostics.get("edge_no_cents")
    lean = None
    edge = None
    if isinstance(edge_yes, (int, float)) and isinstance(edge_no, (int, float)):
        lean, edge = ("yes", edge_yes) if edge_yes >= edge_no else ("no", edge_no)
    elif signal is not None:
        lean, edge = signal.side, signal.edge_cents

    implied = signal.market_implied_probability if signal is not None else None
    if implied is None and lean in {"yes", "no"}:
        quote = diagnostics.get(f"{lean}_ask_cents")
        if isinstance(quote, (int, float)):
            implied = float(quote) / 100.0

    if signal is not None:
        verb = "SELL" if signal.action == "sell" else "BUY"
        text = (
            f"{verb} {signal.count} {signal.side.upper()} @ "
            f"{signal.quote_price_cents}c: {signal.reason}"
        )
    else:
        text = f"PASS: {reason.replace('_', ' ')}"
    return {
        "ts": time.time(),
        "action_intent": text,
        "decision_reason": reason,
        "model_fair_value_cents": (
            round(float(p_model) * 100, 4)
            if isinstance(p_model, (int, float))
            else None
        ),
        "model_probability": p_model,
        "market_implied_probability": implied,
        "lean_side": lean,
        "best_edge_cents": edge,
    }


def _emit_event(kind: str, reason: str, **extra: Any) -> dict[str, Any]:
    event = {"ts": time.time(), "kind": kind, "reason": reason, **extra}
    with _lock:
        _events.append(dict(event))
        _runtime_state["last_event"] = dict(event)
    try:
        path = Path(EXECUTION_EVENTS_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, separators=(",", ":")) + "\n")
    except OSError as exc:  # pragma: no cover - audit persistence is best effort
        logger.warning("Could not persist trading event: %s", exc)
    return event


def _place_order(
    *,
    execution_mode: str,
    market_ticker: str,
    side: str,
    action: str,
    count: int | float,
    price_cents: float,
    fee_multiplier: float = 1.0,
    allow_when_stopped: bool = False,
) -> dict[str, Any]:
    if execution_mode == "paper":
        return _paper_account.place_ioc(
            market_ticker=market_ticker,
            side=side,
            action=action,
            count=int(count),
            price_cents=price_cents,
            book=get_live_book(),
            fee_multiplier=fee_multiplier,
        )
    if execution_mode != "live":
        raise RuntimeError("Choose Paper or Live first.")
    return place_limit_order(
        market_ticker=market_ticker,
        side=side,
        action=action,
        count=count,
        price_cents=price_cents,
        allow_when_stopped=allow_when_stopped,
    )


def _current_fee_multiplier() -> float:
    with _lock:
        return float(
            (_runtime_state.get("fee_policy") or {}).get("fee_multiplier") or 1.0
        )


def _flatten_market(
    account: dict[str, Any], market_ticker: str, execution_mode: str
) -> dict[str, Any]:
    position = _market_position(account, market_ticker)
    if position is None:
        return {"ok": True, "status": "already_flat", "market_ticker": market_ticker}
    book = get_live_book()
    if book is None or book.market_ticker != market_ticker or not book.initialized:
        raise RuntimeError("Cannot flatten without a current initialized orderbook.")
    yes_bid, _, no_bid, _ = book.get_best_prices()
    side = str(position["side"])
    bid = yes_bid if side == "yes" else no_bid
    if not isinstance(bid, (int, float)):
        raise RuntimeError(f"Cannot flatten {side.upper()}: no best bid.")
    settings = get_trading_settings_model()
    market_info = get_live_market_info()
    result = _place_order(
        execution_mode=execution_mode,
        market_ticker=market_ticker,
        side=side,
        action="sell",
        count=float(position["contracts"]),
        price_cents=slipped_price_cents(
            float(bid),
            settings.slippage_ticks,
            "down",
            market_info.get("price_ranges"),
        ),
        fee_multiplier=_current_fee_multiplier(),
        allow_when_stopped=True,
    )
    _emit_event(
        "flatten",
        "operator_or_risk_flatten",
        market_ticker=market_ticker,
        result=result,
    )
    return result


def submit_manual_order(*, side: str, action: str, count: Any) -> dict[str, Any]:
    side = side.strip().lower()
    action = action.strip().lower()
    if side not in {"yes", "no"} or action not in {"buy", "sell"}:
        raise ValueError("side must be yes/no and action must be buy/sell")
    try:
        raw_quantity = Decimal(str(count))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("count must be a whole number") from exc
    if not raw_quantity.is_finite() or raw_quantity != raw_quantity.to_integral_value():
        raise ValueError("count must be a whole number")
    quantity = int(raw_quantity)
    if quantity < 1:
        raise ValueError("count must be at least 1")

    with _execution_lock:
        execution_mode = _get_execution_mode()
        if execution_mode not in {"paper", "live"}:
            raise RuntimeError("Choose Paper or Live first.")
        if not _is_armed():
            raise RuntimeError("Start trading before submitting a discretionary order.")
        settings = get_trading_settings_model()
        if quantity > settings.max_order_contracts:
            raise ValueError(
                f"count exceeds the {settings.max_order_contracts}-contract order limit"
            )

        market_info = get_live_market_info()
        market_ticker = str(market_info.get("ticker") or "").strip()
        book = get_live_book()
        if (
            not market_ticker
            or book is None
            or not book.initialized
            or book.market_ticker != market_ticker
        ):
            raise RuntimeError("A current initialized market is required.")
        book_updated_ts = getattr(book, "last_update_ts", None)
        if (
            not isinstance(book_updated_ts, (int, float))
            or time.time() - book_updated_ts > MAX_ORDERBOOK_AGE_SECONDS
        ):
            raise RuntimeError("The active orderbook is stale.")

        account = _fetch_account_snapshot(execution_mode)
        daily_risk, _ = _sync_daily_risk(
            int(account["equity_cents"]),
            settings.max_daily_loss_usd,
            execution_mode,
        )
        if daily_risk["locked"]:
            _set_armed(False)
            raise RuntimeError("Daily loss guard is locked until the next trading day.")

        position = _market_position(account, market_ticker)
        yes_bid, yes_ask, no_bid, no_ask = book.get_best_prices()
        quote = (
            (yes_ask if side == "yes" else no_ask)
            if action == "buy"
            else (yes_bid if side == "yes" else no_bid)
        )
        if not isinstance(quote, (int, float)):
            raise RuntimeError(f"No current {side.upper()} quote is available.")

        if action == "sell":
            if position is None or position.get("side") != side:
                raise ValueError(f"No {side.upper()} position is available to sell.")
            if quantity > int(position.get("strategy_contracts") or 0):
                raise ValueError("sell count exceeds the current position")
            limit_price = slipped_price_cents(
                float(quote),
                settings.slippage_ticks,
                "down",
                market_info.get("price_ranges"),
            )
        else:
            if position is not None and position.get("side") != side:
                raise ValueError("Close the opposite-side position before buying.")
            limit_price = slipped_price_cents(
                float(quote),
                settings.slippage_ticks,
                "up",
                market_info.get("price_ranges"),
            )
            current_notional = (
                float(position.get("market_exposure_cents") or 0.0)
                if position is not None
                else 0.0
            )
            if (
                current_notional + quantity * limit_price
                > settings.max_position_usd * 100
            ):
                raise ValueError("order would exceed the configured position limit")
            fee_multiplier = _current_fee_multiplier()
            worst_case_cost = (
                quantity * limit_price
                + quantity
                * taker_fee_cents_per_contract(
                    limit_price,
                    count=quantity,
                    fee_multiplier=fee_multiplier,
                )
            )
            available_cash = max(
                0.0, float(account["cash_cents"]) - settings.cash_buffer_usd * 100
            )
            if worst_case_cost > available_cash:
                raise ValueError("order would breach the configured cash buffer")

        result = _place_order(
            execution_mode=execution_mode,
            market_ticker=market_ticker,
            side=side,
            action=action,
            count=quantity,
            price_cents=limit_price,
            fee_multiplier=_current_fee_multiplier(),
        )
        order = result.get("order", {})
        filled = _decimal(order.get("fill_count")) > 0
        status = f"manual_{execution_mode}_{'filled' if filled else 'unfilled'}"
        event = _emit_event(
            "manual_order",
            status,
            side=side,
            action=action,
            count=quantity,
            limit_price_cents=limit_price,
            order=order,
        )
        state: dict[str, Any] = {"status": status, "last_order": event}
        if execution_mode == "paper":
            state["account"] = _fetch_account_snapshot(execution_mode)
        _set_state(**state)
        return {"ok": bool(result.get("ok", True)), "status": status, "result": result}


def get_trading_runtime_snapshot() -> dict[str, Any]:
    with _lock:
        return dict(_runtime_state)


def get_trading_events(limit: int = 200) -> list[dict[str, Any]]:
    with _lock:
        return list(_events)[-max(1, int(limit)) :]


def control_trading(operation: str, execution_mode: str = "") -> dict[str, Any]:
    operation = operation.strip().lower()
    if operation == "start":
        mode = execution_mode.strip().lower()
        if mode not in {"paper", "live"}:
            raise ValueError("execution_mode must be paper or live")
        with _execution_lock:
            if _get_execution_mode() == "live" and mode != "live":
                cancel_bot_orders()
            account = _fetch_account_snapshot(mode)
            _set_armed(False)
            _set_execution_mode(mode)
            settings = get_trading_settings_model()
            daily_risk, _ = _sync_daily_risk(
                int(account["equity_cents"]), settings.max_daily_loss_usd, mode
            )
            if daily_risk["locked"]:
                raise RuntimeError(
                    "Daily loss guard is locked until the next trading day."
                )
            _set_armed(True)
            _set_state(
                account=account,
                daily_risk=daily_risk,
                status="running",
                last_error=None,
            )
            _emit_event("control", "started_by_operator", execution_mode=mode)
            return {"ok": True, "status": "started", "execution_mode": mode}

    if operation not in {"pause", "flatten"}:
        raise ValueError("operation must be start, pause, or flatten")

    with _execution_lock:
        mode = _get_execution_mode()
        _set_armed(False)
        cancel_error = None
        if mode == "live":
            try:
                canceled = cancel_bot_orders()
            except Exception as exc:  # cancellation failure must not re-arm the engine
                canceled, cancel_error = 0, str(exc)
        else:
            canceled = 0

        if operation == "pause":
            _set_state(status="paused", last_reason="paused_by_operator")
            _emit_event("control", "paused_by_operator", canceled_orders=canceled)
            return {
                "ok": cancel_error is None,
                "status": "paused",
                "canceled_orders": canceled,
                "cancel_error": cancel_error,
            }

        market_ticker = str(get_live_market_info().get("ticker") or "")
        if not market_ticker:
            raise RuntimeError("No active market to flatten.")
        if mode not in {"paper", "live"}:
            raise RuntimeError("Choose Paper or Live first.")
        account = _fetch_account_snapshot(mode)
        result = _flatten_market(account, market_ticker, mode)
        _set_state(status="flatten_submitted", last_reason="flattened_by_operator")
        return {
            "ok": True,
            "status": "flatten_submitted",
            "canceled_orders": canceled,
            "cancel_error": cancel_error,
            "result": result,
        }


def _submit_signal(
    signal: TradeSignal,
    cycle_ts: float,
    cooldown_seconds: int,
    execution_mode: str | None = None,
) -> tuple[str, dict[str, Any] | None]:
    global _last_submission_ts
    with _execution_lock:
        mode = execution_mode or _get_execution_mode()
        if mode != _get_execution_mode():
            return "mode_changed", None
        if mode not in {"paper", "live"} or not _is_armed():
            return "disarmed", None
        if signal.action == "buy" and cycle_ts - _last_submission_ts < cooldown_seconds:
            return "cooldown", None
        _last_submission_ts = cycle_ts
        return "submitted", _place_order(
            execution_mode=mode,
            market_ticker=signal.market_ticker,
            side=signal.side,
            action=signal.action,
            count=signal.count,
            price_cents=signal.quote_price_cents,
            fee_multiplier=float(signal.diagnostics.get("fee_multiplier") or 1.0),
        )


_submit_live_signal = _submit_signal


async def _refresh_paper_account(
    active_market_ticker: str, execution_mode: str
) -> None:
    if execution_mode != "paper":
        return
    _paper_account.mark_to_market(active_market_ticker, get_live_book())
    for market_ticker in _paper_account.market_tickers() - {active_market_ticker}:
        try:
            market = await asyncio.to_thread(get_market, market_ticker)
            result = str(market.get("result") or "").lower()
            finalized = str(market.get("status") or "").lower() in {
                "finalized",
                "settled",
            } or bool(market.get("settlement_ts"))
            if finalized and result in {"yes", "no"}:
                settlement = _paper_account.settle(market_ticker, result)
                _emit_event("settlement", "paper_market_settled", **settlement)
        except Exception as exc:  # settlement is retried on the next cycle
            logger.warning("Could not settle paper market %s: %s", market_ticker, exc)


def _enforce_daily_loss_lock(
    execution_mode: str,
    account: dict[str, Any],
    market_ticker: str,
    daily_risk: dict[str, Any],
    newly_locked: bool,
) -> None:
    with _execution_lock:
        if execution_mode != _get_execution_mode():
            return
        was_armed = _is_armed()
        _set_armed(False)
        if newly_locked:
            _emit_event("risk", "daily_loss_limit_reached", daily_risk=daily_risk)
        if was_armed and market_ticker:
            try:
                if execution_mode == "live":
                    cancel_bot_orders()
                _flatten_market(account, market_ticker, execution_mode)
            except Exception as exc:
                _set_state(last_error=f"Risk flatten failed: {exc}")
        _set_state(status="daily_loss_locked", last_reason="daily_loss_limit_reached")


async def _run_single_cycle() -> None:
    global _last_market_ticker

    cycle_ts = time.time()
    settings = get_trading_settings_model()
    settings_snapshot = get_trading_settings_snapshot()
    execution_mode = _get_execution_mode()
    if execution_mode not in {"paper", "live"}:
        _set_state(status="stopped", settings=settings_snapshot)
        return

    market_info = get_live_market_info()
    market_ticker = str(market_info.get("ticker") or "").strip()

    await _refresh_paper_account(market_ticker, execution_mode)
    account = await asyncio.to_thread(_fetch_account_snapshot, execution_mode)
    daily_risk, newly_locked = _sync_daily_risk(
        int(account["equity_cents"]), settings.max_daily_loss_usd, execution_mode
    )
    if execution_mode != _get_execution_mode():
        return
    _set_state(account=account, daily_risk=daily_risk, settings=settings_snapshot)

    if daily_risk["locked"]:
        await asyncio.to_thread(
            _enforce_daily_loss_lock,
            execution_mode,
            account,
            market_ticker,
            daily_risk,
            newly_locked,
        )
        return

    if not market_ticker:
        _set_state(
            status="waiting_market",
            last_reason="no_active_market",
            current_market_ticker=None,
        )
        return

    if _last_market_ticker and _last_market_ticker != market_ticker:
        if execution_mode == "live":
            await asyncio.to_thread(
                cancel_bot_orders, market_ticker=_last_market_ticker
            )
    _last_market_ticker = market_ticker

    strike = extract_suggested_strike(market_info)
    pricing = apply_pricing_overrides(
        compute_live_pricing_snapshot(
            strike=strike,
            market_ticker=market_ticker,
            close_time_iso=(
                market_info.get("close_time")
                if isinstance(market_info.get("close_time"), str)
                else None
            ),
        ),
        settings,
    )

    profile = get_active_asset_context().profile
    try:
        series = await asyncio.to_thread(get_series, profile.kalshi_series_ticker)
        event_ticker = str(market_info["event_ticker"])
        event = await asyncio.to_thread(get_event, event_ticker)
        fee_type = str(event.get("fee_type_override") or series["fee_type"])
        fee_multiplier = float(
            event.get("fee_multiplier_override")
            if event.get("fee_multiplier_override") is not None
            else series["fee_multiplier"]
        )
        if fee_multiplier <= 0:
            raise ValueError("Kalshi returned a non-positive fee multiplier.")
        fee_policy = {
            "series_ticker": profile.kalshi_series_ticker,
            "event_ticker": event_ticker,
            "fee_type": fee_type,
            "fee_multiplier": fee_multiplier,
            "ready": True,
        }
    except Exception as exc:
        fee_type = None
        fee_multiplier = None
        fee_policy = {
            "series_ticker": profile.kalshi_series_ticker,
            "ready": False,
            "error": str(exc),
        }

    yes_qty, no_qty, yes_avg, no_avg = _position_inputs(account, market_ticker)
    signal, reason, diagnostics = build_trade_signal(
        pricing=pricing,
        market_ticker=market_ticker,
        book=get_live_book(),
        settings=settings,
        bankroll_cents=int(account["equity_cents"]),
        open_yes_contracts=yes_qty,
        open_no_contracts=no_qty,
        open_yes_avg_entry_cents=yes_avg,
        open_no_avg_entry_cents=no_avg,
        runtime_uptime_seconds=cycle_ts - _runtime_started_ts,
        available_cash_cents=max(
            0, int(account["cash_cents"]) - round(settings.cash_buffer_usd * 100)
        ),
        fee_multiplier=fee_multiplier,
        fee_type=fee_type,
        price_ranges=market_info.get("price_ranges"),
        now_ts=cycle_ts,
    )
    signal_payload = _signal_payload(signal)
    monologue = _monologue(signal, reason, pricing, diagnostics)

    common_state = {
        "current_market_ticker": market_ticker,
        "last_signal": signal_payload,
        "signal_monologue": monologue,
        "pricing": pricing,
        "diagnostics": diagnostics,
        "fee_policy": fee_policy,
        "last_reason": reason,
        "last_error": None,
    }
    if signal is None:
        _set_state(status="no_signal", **common_state)
        return
    try:
        submission, placed = await asyncio.to_thread(
            _submit_signal,
            signal,
            cycle_ts,
            settings.cooldown_seconds,
            execution_mode,
        )
        if submission in {"disarmed", "mode_changed"}:
            if execution_mode == _get_execution_mode():
                _set_state(status="signal_waiting_for_start", **common_state)
            return
        if submission == "cooldown":
            _set_state(
                status="cooldown",
                last_reason="entry_cooldown",
                **{
                    key: value
                    for key, value in common_state.items()
                    if key != "last_reason"
                },
            )
            return
        assert placed is not None
        order = placed.get("order", {})
        fill_count = _decimal(order.get("fill_count"))
        status = f"{execution_mode}_{'filled' if fill_count > 0 else 'unfilled'}"
        event = _emit_event(
            "order",
            status,
            signal=signal_payload,
            order=order,
            client_order_id=placed.get("client_order_id"),
        )
        _set_state(status=status, last_order=event, **common_state)
    except Exception as exc:
        _emit_event(
            "rejection",
            f"{execution_mode}_order_failed",
            signal=signal_payload,
            error=str(exc),
        )
        _set_state(
            status=f"{execution_mode}_order_error",
            **{**common_state, "last_error": str(exc)},
        )


async def run_trading_loop() -> None:
    while True:
        cycle_started = time.time()
        try:
            await _run_single_cycle()
        except Exception as exc:  # pragma: no cover - top-level process safeguard
            logger.exception("Trading cycle failed: %s", exc)
            _set_state(
                status="cycle_error",
                last_reason="unexpected_exception",
                last_error=str(exc),
            )
        _set_state(last_cycle_ts=cycle_started)
        elapsed = time.time() - cycle_started
        await asyncio.sleep(max(0.1, EXECUTION_LOOP_INTERVAL_SEC - elapsed))
