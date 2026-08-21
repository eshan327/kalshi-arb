from __future__ import annotations

import asyncio
import contextlib
import math
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import reflex as rx

from core.config import ORDERBOOK_VIEW_DEPTH
from engine.market_stream.discovery import parse_iso8601_to_epoch
from engine.settlement_sampling import extract_valid_index_points
from engine.trading.runtime import control_trading
from engine.trading.settings import reset_trading_settings, update_trading_settings
from engine.trading.strategy import MAX_ORDERBOOK_AGE_SECONDS
from engine.vol_estimator import realized_vol_from_price_points
from feeds.state.tick_store import get_brti_ticks
from ui.services.dashboard_state_service import build_dashboard_state_payload
from ui.services.runtime_services import (
    run_background_services,
    validate_auth_or_exit,
)

_PLOT_WINDOW_SECONDS = 4 * 60


def _number(value: Any) -> float | None:
    return (
        float(value)
        if isinstance(value, (int, float)) and math.isfinite(value)
        else None
    )


def _money(cents: Any) -> str:
    value = _number(cents)
    return f"${value / 100:,.2f}" if value is not None else "—"


def _cents(value: Any) -> str:
    number = _number(value)
    return f"{number:.2f}".rstrip("0").rstrip(".") + "¢" if number is not None else "—"


def _percent(value: Any, *, ratio: bool = False) -> str:
    number = _number(value)
    return f"{number * (100 if ratio else 1):.1f}%" if number is not None else "—"


def _duration(seconds: Any) -> str:
    value = _number(seconds)
    if value is None:
        return "—"
    minutes, whole_seconds = divmod(max(0, round(value)), 60)
    return f"{minutes}m {whole_seconds:02d}s" if minutes else f"{whole_seconds}s"


def _tone(value: float | None) -> str:
    return (
        "positive"
        if value and value > 0
        else "negative"
        if value and value < 0
        else ""
    )


def _fresh(timestamp: Any, max_age: float, now: float) -> bool:
    value = _number(timestamp)
    return value is not None and 0 <= now - value <= max_age


def _decision_state(
    monologue: dict[str, Any], armed: bool
) -> tuple[str, str, str]:
    if not armed:
        return "WAIT", "Engine is paused.", "neutral"
    reason = str(monologue.get("decision_reason") or "")
    reason_text = {
        "at_target_allocation": "Target allocation reached.",
        "edge_below_threshold": "Edge is below the required minimum.",
        "entry_cutoff": "Too close to settlement.",
        "fee_policy_unavailable": "Fee schedule unavailable.",
        "insufficient_available_cash": "Available cash is below reserve.",
        "invalid_model_probability": "Model probability unavailable.",
        "missing_best_quotes": "Best bid or ask unavailable.",
        "market_probability_unavailable": "Market midpoint unavailable.",
        "orderbook_market_mismatch": "Order book is rotating.",
        "p_book_direction_conflict": "Model and order book disagree.",
        "p_book_divergence_high": "Model and order book differ too much.",
        "p_book_unavailable": "Order-book signal unavailable.",
        "position_notional_cap_reached": "Position cap reached.",
        "pricing_not_ready": "Model is warming up.",
        "stale_orderbook": "Order book is stale.",
        "volatility_fallback": "Waiting for realized volatility.",
    }.get(
        reason,
        reason.replace("_", " ").capitalize() + "."
        if reason
        else "Waiting for a model decision.",
    )
    intent = str(monologue.get("action_intent") or "").upper()
    if intent.startswith("BUY"):
        side = str(monologue.get("lean_side") or "").upper()
        return f"BUY {side}".strip(), reason_text, "positive"
    if intent.startswith("SELL"):
        return "EXIT", reason_text, "warning"
    if reason == "pricing_not_ready":
        return "WARMING UP", reason_text, "neutral"
    if reason == "stale_orderbook":
        return "STALE DATA", reason_text, "danger"
    if reason in {
        "fee_policy_unavailable",
        "insufficient_available_cash",
        "position_notional_cap_reached",
    }:
        return "RISK BLOCK", reason_text, "danger"
    return "NO TRADE", reason_text, "warning"


def _book_summary(book: dict[str, Any], side: str) -> dict[str, str]:
    bids = book.get(f"{side}_bids") or []
    asks = book.get(f"{side}_asks") or []
    bid = _number(bids[0][0]) if bids else None
    ask = _number(asks[0][0]) if asks else None
    return {
        "bid": _cents(bid),
        "ask": _cents(ask),
        "spread": _cents(ask - bid) if bid is not None and ask is not None else "—",
    }


def _book_rows(book: dict[str, Any], side: str) -> list[dict[str, str]]:
    asks = book.get(f"{side}_asks") or []
    bids = book.get(f"{side}_bids") or []
    levels = [
        *(("ask", level) for level in reversed(asks)),
        *(("bid", level) for level in bids),
    ]
    maximum = max((float(level[1]) for _, level in levels), default=1.0)
    return [
        {
            "side": label.upper(),
            "tone": label,
            "price": _cents(level[0]),
            "size": f"{float(level[1]):,.2f}".rstrip("0").rstrip("."),
            "depth": f"{max(4, float(level[1]) / maximum * 100):.1f}%",
            "row_class": (
                f"{label} top-of-book"
                if (label == "ask" and asks and level is asks[0])
                or (label == "bid" and bids and level is bids[0])
                else label
            ),
        }
        for label, level in levels
    ]


def _position_rows(
    account: dict[str, Any], book: dict[str, Any], market_ticker: str
) -> list[dict[str, str]]:
    rows = []
    for position in account.get("positions") or []:
        side = str(position.get("side") or "").lower()
        contracts = _number(position.get("contracts")) or 0.0
        cost_basis = _number(position.get("avg_entry_cents"))
        bids = book.get(f"{side}_bids") or []
        mark = (
            _number(bids[0][0])
            if position.get("market_ticker") == market_ticker and bids
            else None
        )
        unrealized = (
            contracts * (mark - cost_basis)
            if mark is not None and cost_basis is not None
            else None
        )
        rows.append(
            {
                "side": side.upper() or "—",
                "contracts": f"{contracts:g}",
                "cost_basis": _cents(cost_basis),
                "mark": _cents(mark),
                "exposure": _money(position.get("market_exposure_cents")),
                "unrealized_pnl": _money(unrealized),
                "pnl_tone": _tone(unrealized),
            }
        )
    return rows


def recent_rows(
    rows: list[dict[str, Any]], window_seconds: int, now_ts: float | None = None
) -> list[dict[str, Any]]:
    timestamps = [_number(row.get("ts")) for row in rows]
    valid = [timestamp for timestamp in timestamps if timestamp is not None]
    if not valid:
        return []
    cutoff = (max(valid) if now_ts is None else float(now_ts)) - window_seconds
    return [
        row
        for row, timestamp in zip(rows, timestamps)
        if timestamp is not None and timestamp >= cutoff
    ]


def moving_average(
    rows: list[dict[str, Any]],
    window_seconds: int,
    average_start_ts: float | None = None,
) -> list[dict[str, Any]]:
    points = sorted(
        (
            (float(row["ts"]), float(row["brti"]))
            for row in rows
            if _number(row.get("ts")) is not None
            and _number(row.get("brti")) is not None
        ),
        key=lambda point: point[0],
    )
    result: list[dict[str, Any]] = []
    left = 0
    display_left = 0
    running_sum = 0.0
    display_sum = 0.0
    average_started = False
    for right, (timestamp, spot) in enumerate(points):
        display_sum += spot
        while timestamp - points[display_left][0] > 5:
            display_sum -= points[display_left][1]
            display_left += 1
        average = None
        if average_start_ts is None or timestamp >= average_start_ts:
            if not average_started:
                left = right
                running_sum = 0.0
                average_started = True
            running_sum += spot
            while timestamp - points[left][0] > window_seconds:
                running_sum -= points[left][1]
                left += 1
            average = round(running_sum / (right - left + 1), 2)
        result.append(
            {
                "time": datetime.fromtimestamp(timestamp).strftime("%H:%M:%S"),
                "spot": round(display_sum / (right - display_left + 1), 2),
                "average": average,
            }
        )
    return result


def market_open_time(close_time_iso: str | None) -> float | None:
    close_ts = parse_iso8601_to_epoch(close_time_iso)
    return None if close_ts is None else close_ts - 15 * 60


def market_ticks(
    rows: list[dict[str, Any]], close_time_iso: str | None
) -> list[dict[str, Any]]:
    market_open_ts = market_open_time(close_time_iso)
    if market_open_ts is None:
        return rows
    return [row for row in rows if (_number(row.get("ts")) or 0) >= market_open_ts]


class DashboardState(rx.State):
    asset = "—"
    market = "Waiting for market"
    selected_mode = "paper"
    armed = False
    has_position = False
    pnl = "$0.00"
    pnl_tone = ""
    engine_state = "PAUSED"
    mode_label = "SIM"
    data_state = "DISCONNECTED"

    cash = "—"
    daily_capacity = "—"
    risk_state = "NORMAL"
    positions: list[dict[str, str]] = []

    spot = "—"
    strike = "—"
    expires = "—"
    volatility = "—"
    probability = "—"
    implied = "—"
    edge = "—"
    required_edge = "—"
    lean = "—"
    decision = "WARMING UP"
    decision_reason = "Waiting for market data."
    decision_tone = "neutral"
    model_history: list[dict[str, Any]] = []
    vol_history: list[dict[str, Any]] = []
    price_history: list[dict[str, Any]] = []
    yes_book: list[dict[str, str]] = []
    no_book: list[dict[str, str]] = []
    yes_quote: dict[str, str] = {"bid": "—", "ask": "—", "spread": "—"}
    no_quote: dict[str, str] = {"bid": "—", "ask": "—", "spread": "—"}

    min_edge = "2"
    deterministic_edge = "0.5"
    kelly_fraction = "0.25"
    position_fraction = "5"
    max_order = "10"
    max_position = "50"
    max_daily_loss = "10"
    cash_buffer = "25"
    cooldown = "5"
    slippage = "1"
    vol_override = ""
    vol_source = "realized"
    vol_scale = "1"
    settings_status = ""

    _snapshot: dict[str, Any] = {}
    _settings: dict[str, Any] = {}
    _history_market = ""
    _mode_touched = False

    def _apply_settings(self, settings: dict[str, Any]) -> None:
        self._settings = settings
        self.min_edge = str(settings.get("min_edge_cents", 2))
        self.deterministic_edge = str(settings.get("deterministic_min_edge_cents", 0.5))
        self.kelly_fraction = str(settings.get("kelly_fraction", 0.25))
        self.position_fraction = str(
            float(settings.get("max_position_fraction", 0.05)) * 100
        )
        self.max_order = str(settings.get("max_order_contracts", 10))
        self.max_position = str(settings.get("max_position_usd", 50))
        self.max_daily_loss = str(settings.get("max_daily_loss_usd", 10))
        self.cash_buffer = str(settings.get("cash_buffer_usd", 25))
        self.cooldown = str(settings.get("cooldown_seconds", 5))
        self.slippage = str(settings.get("slippage_ticks", 1))
        override = settings.get("volatility_override")
        self.vol_override = "" if override is None else str(override)
        self.vol_source = "realized" if override is None else "override"
        self.vol_scale = str(settings.get("volatility_scale", 1))

    def _refresh(self) -> None:
        try:
            state = build_dashboard_state_payload(depth=ORDERBOOK_VIEW_DEPTH)
            self._snapshot = state
            runtime = state.get("trading_runtime") or {}
            risk = runtime.get("daily_risk") or {}
            account = state.get("account") or runtime.get("account") or {}
            pricing = state.get("pricing") or {}
            monologue = state.get("signal_monologue") or {}
            book = state.get("orderbook") or {}
            brti = state.get("brti") or {}
            ticks = get_brti_ticks(limit=4000)
            market_info = state.get("market_info") or {}
            history_ticks = recent_rows(ticks, _PLOT_WINDOW_SECONDS)
            sigma_fit = realized_vol_from_price_points(
                extract_valid_index_points(ticks),
                window_seconds=300,
                min_samples=8,
            )

            self.asset = str(state.get("asset_display") or state.get("asset") or "—")
            market_ticker = str(
                runtime.get("current_market_ticker") or book.get("market_ticker") or ""
            )
            self.market = market_ticker or "Waiting for market"
            if market_ticker and market_ticker != self._history_market:
                self.model_history = []
                self._history_market = market_ticker
            self.armed = bool(runtime.get("armed"))
            if (
                runtime.get("execution_mode") in {"paper", "live"}
                and (self.armed or not self._mode_touched)
            ):
                self.selected_mode = runtime["execution_mode"]
            equity_change = _number(risk.get("session_pnl_cents")) or 0.0
            self.pnl = _money(equity_change)
            self.pnl_tone = _tone(equity_change)
            self.engine_state = "RUNNING" if self.armed else "PAUSED"
            self.mode_label = "LIVE" if self.selected_mode == "live" else "SIM"

            now_ts = time.time()
            book_ok = bool(book.get("initialized")) and _fresh(
                book.get("last_update_ts"), MAX_ORDERBOOK_AGE_SECONDS, now_ts
            )
            index_ok = _fresh(brti.get("timestamp"), 5, now_ts)
            model_ok = bool(pricing.get("ready")) and _fresh(
                runtime.get("last_cycle_ts"), 5, now_ts
            )
            healthy_count = sum((book_ok, index_ok, model_ok))
            self.data_state = (
                "LIVE"
                if healthy_count == 3
                else "STALE"
                if healthy_count
                else "DISCONNECTED"
            )

            self.cash = _money(account.get("cash_cents"))
            self.positions = _position_rows(account, book, market_ticker)
            self.has_position = bool(self.positions)
            max_loss = _number(risk.get("max_daily_loss_cents"))
            drawdown = _number(risk.get("drawdown_cents")) or 0.0
            remaining = max(0.0, (max_loss or 0.0) + min(0.0, drawdown))
            self.daily_capacity = _money(remaining) if max_loss is not None else "—"
            utilization = abs(min(0.0, drawdown)) / max_loss if max_loss else 0.0
            locked = bool(risk.get("locked"))
            self.risk_state = (
                "LOCKED"
                if locked
                else "NEAR LIMIT"
                if utilization >= 0.8
                else "NORMAL"
            )

            spot = _number(brti.get("brti"))
            strike = _number(pricing.get("strike_usd") or state.get("suggested_strike"))
            self.spot = f"${spot:,.2f}" if spot is not None else "—"
            self.strike = f"${strike:,.0f}" if strike is not None else "—"
            self.expires = _duration(pricing.get("seconds_to_expiry"))
            self.volatility = _percent(sigma_fit, ratio=True)
            self.probability = (
                _percent(pricing.get("p_model_pct")) if pricing.get("ready") else "—"
            )
            self.implied = _percent(
                monologue.get("market_implied_probability"), ratio=True
            )
            self.edge = _cents(monologue.get("best_edge_cents"))
            self.required_edge = _cents(
                (state.get("trading_settings") or {}).get("min_edge_cents")
            )
            self.lean = str(monologue.get("lean_side") or "—").upper()
            self.decision, self.decision_reason, self.decision_tone = _decision_state(
                monologue, self.armed
            )
            probability = _number(pricing.get("p_model_pct"))
            market_probability = _number(monologue.get("market_implied_probability"))
            history_ts = datetime.now().timestamp()
            now = datetime.fromtimestamp(history_ts).strftime("%H:%M:%S")
            if pricing.get("ready") and probability is not None:
                point = {
                    "ts": history_ts,
                    "time": now,
                    "model": round(probability, 2),
                    "market": (
                        round(market_probability * 100, 2)
                        if market_probability is not None
                        else None
                    ),
                }
                if not self.model_history or self.model_history[-1] != point:
                    self.model_history = recent_rows(
                        [*self.model_history, point],
                        _PLOT_WINDOW_SECONDS,
                        history_ts,
                    )
            sigma = _number(sigma_fit)
            if sigma is not None:
                point = {
                    "ts": history_ts,
                    "time": now,
                    "volatility": round(sigma * 100, 2),
                }
                if not self.vol_history or self.vol_history[-1] != point:
                    self.vol_history = recent_rows(
                        [*self.vol_history, point],
                        _PLOT_WINDOW_SECONDS,
                        history_ts,
                    )
            window = int(state.get("settlement_window_seconds") or 60)
            self.price_history = [
                {**point, "strike": strike}
                for point in moving_average(
                    history_ticks,
                    window,
                    average_start_ts=market_open_time(market_info.get("close_time")),
                )
            ]
            self.yes_book = _book_rows(book, "yes")
            self.no_book = _book_rows(book, "no")
            self.yes_quote = _book_summary(book, "yes")
            self.no_quote = _book_summary(book, "no")
            if not self._settings:
                self._apply_settings(state.get("trading_settings") or {})
        except Exception as exc:
            self.data_state = "DISCONNECTED"
            self.decision = "ERROR"
            self.decision_reason = str(exc)
            self.decision_tone = "danger"

    @rx.event
    def load(self) -> None:
        self._refresh()

    @rx.event
    def refresh(self, _date: str) -> None:
        self._refresh()

    @rx.event
    def choose_mode(self, mode: str) -> None:
        if mode in {"paper", "live"}:
            self.selected_mode = mode
            self.mode_label = "LIVE" if mode == "live" else "SIM"
            self._mode_touched = True

    def _control(self, operation: str) -> None:
        try:
            control_trading(
                operation, self.selected_mode if operation == "start" else ""
            )
            self.settings_status = ""
        except (ValueError, RuntimeError) as exc:
            self.settings_status = str(exc)
        self._refresh()

    @rx.event
    def start(self) -> None:
        self._control("start")

    @rx.event
    def stop(self) -> None:
        self._control("pause")

    @rx.event
    def flatten(self) -> None:
        self._control("flatten")

    @rx.event
    def save_settings(self) -> None:
        try:
            settings, errors = update_trading_settings(
                {
                    "min_edge_cents": float(self.min_edge),
                    "deterministic_min_edge_cents": float(self.deterministic_edge),
                    "kelly_fraction": float(self.kelly_fraction),
                    "max_position_fraction": float(self.position_fraction) / 100,
                    "max_order_contracts": int(self.max_order),
                    "max_position_usd": float(self.max_position),
                    "max_daily_loss_usd": float(self.max_daily_loss),
                    "cash_buffer_usd": float(self.cash_buffer),
                    "cooldown_seconds": int(self.cooldown),
                    "slippage_ticks": int(self.slippage),
                    "volatility_override": self.vol_override or None,
                    "volatility_scale": float(self.vol_scale),
                    "use_p_book_hard_gate": bool(
                        self._settings.get("use_p_book_hard_gate", False)
                    ),
                    "p_book_max_divergence": float(
                        self._settings.get("p_book_max_divergence", 0.35)
                    ),
                }
            )
            if errors:
                raise ValueError(" · ".join(errors))
            self._apply_settings(settings)
            self.settings_status = "Settings applied."
        except (TypeError, ValueError) as exc:
            self.settings_status = str(exc)

    @rx.event
    def reset_settings(self) -> None:
        self._apply_settings(reset_trading_settings())
        self.settings_status = "Defaults restored."

    @rx.event
    def set_min_edge(self, value: str) -> None:
        self.min_edge = value

    @rx.event
    def set_deterministic_edge(self, value: str) -> None:
        self.deterministic_edge = value

    @rx.event
    def set_kelly_fraction(self, value: str) -> None:
        self.kelly_fraction = value

    @rx.event
    def set_position_fraction(self, value: str) -> None:
        self.position_fraction = value

    @rx.event
    def set_max_order(self, value: str) -> None:
        self.max_order = value

    @rx.event
    def set_max_position(self, value: str) -> None:
        self.max_position = value

    @rx.event
    def set_max_daily_loss(self, value: str) -> None:
        self.max_daily_loss = value

    @rx.event
    def set_cash_buffer(self, value: str) -> None:
        self.cash_buffer = value

    @rx.event
    def set_cooldown(self, value: str) -> None:
        self.cooldown = value

    @rx.event
    def set_slippage(self, value: str) -> None:
        self.slippage = value

    @rx.event
    def set_vol_override(self, value: str) -> None:
        self.vol_override = value

    @rx.event
    def set_vol_source(self, value: str) -> None:
        self.vol_source = value
        if value == "realized":
            self.vol_override = ""

    @rx.event
    def set_vol_scale(self, value: str) -> None:
        self.vol_scale = value


def _panel_header(title: str, trailing: rx.Component | None = None) -> rx.Component:
    return rx.hstack(
        rx.heading(title, as_="h2"),
        trailing or rx.fragment(),
        class_name="panel-header",
    )


def _metric(label: str, value: Any, *, large: bool = False) -> rx.Component:
    return rx.box(
        rx.text(label, class_name="metric-label"),
        rx.text(
            value, class_name="metric-value hero-value" if large else "metric-value"
        ),
        class_name="metric",
    )


def _book_row(row: dict[str, Any]) -> rx.Component:
    return rx.el.tr(
        rx.el.td(rx.text(row["side"], class_name=row["tone"])),
        rx.el.td(row["price"]),
        rx.el.td(
            rx.box(class_name=f"depth-fill {row['tone']}", width=row["depth"]),
            rx.text(row["size"], class_name="depth-value"),
            class_name="depth-cell",
        ),
        class_name=row["row_class"],
    )


def _book(title: str, rows: Any, quote: Any, tone: str) -> rx.Component:
    return rx.box(
        rx.hstack(rx.heading(title, as_="h3"), class_name=f"book-title {tone}"),
        rx.hstack(
            _metric("Best bid", quote["bid"]),
            _metric("Best ask", quote["ask"]),
            _metric("Spread", quote["spread"]),
            class_name="book-summary",
        ),
        rx.box(
            rx.el.table(
                rx.el.thead(
                    rx.el.tr(rx.el.th("Side"), rx.el.th("Price"), rx.el.th("Size"))
                ),
                rx.el.tbody(rx.foreach(rows, _book_row)),
                class_name="data-table book-table",
            ),
            class_name="book-scroll",
        ),
        class_name="book-side",
    )


def _position_row(position: dict[str, Any]) -> rx.Component:
    return rx.hstack(
        rx.text(position["side"], class_name="position-side"),
        rx.text(position["contracts"]),
        rx.text("@"),
        rx.text(position["cost_basis"]),
        rx.text("Mark", class_name="metric-label"),
        rx.text(position["mark"]),
        rx.text("Exposure", class_name="metric-label"),
        rx.text(position["exposure"]),
        rx.text("uPnL", class_name="metric-label"),
        rx.text(position["unrealized_pnl"], class_name=position["pnl_tone"]),
        class_name="position-line",
    )


def _field(label: str, value: Any, handler: Any, **props: Any) -> rx.Component:
    return rx.el.label(
        rx.text(label),
        rx.input(value=value, on_change=handler, type="number", **props),
        class_name="field",
    )


def _control_group(title: str, *fields: rx.Component) -> rx.Component:
    return rx.box(rx.heading(title, as_="h3"), *fields, class_name="control-group")


def _chart(
    data: Any,
    lines: list[rx.Component],
    *,
    domain: list[int] | None = None,
    unit: str = "",
) -> rx.Component:
    return rx.recharts.line_chart(
        rx.recharts.cartesian_grid(
            stroke="#252e37", stroke_dasharray="2 5", vertical=False
        ),
        rx.recharts.x_axis(
            data_key="time", axis_line=False, tick_line=False, min_tick_gap=44
        ),
        rx.recharts.y_axis(
            domain=domain or ["auto", "auto"],
            axis_line=False,
            tick_line=False,
            width=54,
            unit=unit,
        ),
        rx.recharts.graphing_tooltip(
            content_style={
                "background": "#171d25",
                "border": "1px solid #303946",
                "borderRadius": "6px",
                "fontFamily": "var(--mono)",
            },
            label_style={"color": "#f1f3f5"},
        ),
        rx.recharts.legend(
            align="right",
            icon_size=8,
            vertical_align="top",
            wrapper_style={"fontSize": "10px", "fontFamily": "var(--mono)"},
        ),
        *lines,
        data=data,
        height="100%",
        width="100%",
        margin={"top": 8, "right": 10, "bottom": 0, "left": 0},
    )


def _chart_header(
    title: str, primary: Any, comparison: Any | None = None
) -> rx.Component:
    return rx.hstack(
        rx.heading(title, as_="h3"),
        rx.hstack(
            rx.text(primary, class_name="chart-reading"),
            rx.cond(
                comparison is not None,
                rx.text(comparison, class_name="chart-reading comparison"),
            ),
            class_name="chart-readings",
        ),
        class_name="chart-header",
    )


def index() -> rx.Component:
    return rx.box(
        rx.moment(
            interval=1000, on_change=DashboardState.refresh.temporal, display="none"
        ),
        rx.box(
            rx.el.header(
                rx.hstack(
                    rx.heading("Autotrader", as_="h1"),
                    rx.text(DashboardState.asset, class_name="asset-label"),
                    class_name="topbar-title",
                ),
                rx.hstack(
                    rx.text(
                        DashboardState.mode_label,
                        class_name=rx.cond(
                            DashboardState.selected_mode == "live",
                            "state-badge live",
                            "state-badge sim",
                        ),
                    ),
                    rx.text(
                        DashboardState.engine_state,
                        class_name=rx.cond(
                            DashboardState.armed,
                            "state-badge running",
                            "state-badge paused",
                        ),
                    ),
                    rx.text(
                        "DATA ",
                        DashboardState.data_state,
                        class_name=rx.cond(
                            DashboardState.data_state == "LIVE",
                            "state-badge healthy",
                            rx.cond(
                                DashboardState.data_state == "STALE",
                                "state-badge warning",
                                "state-badge danger",
                            ),
                        ),
                    ),
                    rx.text(DashboardState.expires, " left", class_name="time-left"),
                    rx.hstack(
                        rx.text("P&L", class_name="metric-label"),
                        rx.text(
                            DashboardState.pnl,
                            class_name=rx.cond(
                                DashboardState.pnl_tone == "positive",
                                "pnl-value positive",
                                rx.cond(
                                    DashboardState.pnl_tone == "negative",
                                    "pnl-value negative",
                                    "pnl-value",
                                ),
                            ),
                        ),
                        class_name="pnl-block",
                    ),
                    class_name="state-strip",
                ),
                rx.hstack(
                    rx.button(
                        "Sim",
                        on_click=lambda: DashboardState.choose_mode("paper"),
                        class_name=rx.cond(
                            DashboardState.selected_mode == "paper",
                            "mode active",
                            "mode",
                        ),
                        disabled=DashboardState.armed,
                    ),
                    rx.button(
                        "Live",
                        on_click=lambda: DashboardState.choose_mode("live"),
                        class_name=rx.cond(
                            DashboardState.selected_mode == "live",
                            "mode active live",
                            "mode",
                        ),
                        disabled=DashboardState.armed,
                    ),
                    rx.cond(
                        DashboardState.selected_mode == "live",
                        rx.alert_dialog.root(
                            rx.alert_dialog.trigger(
                                rx.button(
                                    "Start Live",
                                    disabled=DashboardState.armed,
                                    class_name="button danger",
                                )
                            ),
                            rx.alert_dialog.content(
                                rx.alert_dialog.title("Start live trading?"),
                                rx.alert_dialog.description(
                                    "Real orders can be submitted immediately using the active risk settings."
                                ),
                                rx.hstack(
                                    rx.alert_dialog.cancel(
                                        rx.button("Cancel", class_name="button")
                                    ),
                                    rx.alert_dialog.action(
                                        rx.button(
                                            "Start Live",
                                            on_click=DashboardState.start,
                                            class_name="button danger",
                                        )
                                    ),
                                    class_name="dialog-actions",
                                ),
                                class_name="confirm-dialog",
                            ),
                        ),
                        rx.button(
                            "Start Sim",
                            on_click=DashboardState.start,
                            disabled=DashboardState.armed,
                            class_name="button start",
                        ),
                    ),
                    rx.button(
                        "Stop",
                        on_click=DashboardState.stop,
                        disabled=~DashboardState.armed,
                        class_name="button stop",
                    ),
                    rx.alert_dialog.root(
                        rx.alert_dialog.trigger(
                            rx.button(
                                "Flatten",
                                disabled=~DashboardState.has_position,
                                class_name="button danger",
                            )
                        ),
                        rx.alert_dialog.content(
                            rx.alert_dialog.title("Flatten open position?"),
                            rx.alert_dialog.description(
                                "This submits an immediate exit for the current market position."
                            ),
                            rx.hstack(
                                rx.alert_dialog.cancel(
                                    rx.button("Cancel", class_name="button")
                                ),
                                rx.alert_dialog.action(
                                    rx.button(
                                        "Flatten position",
                                        on_click=DashboardState.flatten,
                                        class_name="button danger",
                                    )
                                ),
                                class_name="dialog-actions",
                            ),
                            class_name="confirm-dialog",
                        ),
                    ),
                    class_name="command-bar",
                ),
                class_name="topbar",
            ),
            rx.el.main(
                rx.box(
                    rx.hstack(
                        rx.text("POSITION", class_name="strip-title"),
                        rx.cond(
                            DashboardState.positions.length() > 0,
                            rx.foreach(DashboardState.positions, _position_row),
                            rx.text("Flat", class_name="flat-position"),
                        ),
                        rx.box(class_name="strip-spacer"),
                        _metric("Cash", DashboardState.cash),
                        _metric("Loss capacity", DashboardState.daily_capacity),
                        rx.text(
                            DashboardState.risk_state,
                            class_name=rx.cond(
                                DashboardState.risk_state == "NORMAL",
                                "state-badge healthy",
                                rx.cond(
                                    DashboardState.risk_state == "NEAR LIMIT",
                                    "state-badge warning",
                                    "state-badge danger",
                                ),
                            ),
                        ),
                        class_name="position-strip",
                    ),
                    class_name="surface risk-strip",
                ),
                rx.box(
                    rx.box(
                        _panel_header(
                            "Market & Model",
                            rx.text(DashboardState.market, class_name="market-tag"),
                        ),
                        rx.box(
                            rx.box(
                                rx.hstack(
                                    _metric(
                                        "Model probability",
                                        DashboardState.probability,
                                        large=True,
                                    ),
                                    rx.text(
                                        DashboardState.lean, class_name="lean-pill"
                                    ),
                                    class_name="probability-row",
                                ),
                                rx.hstack(
                                    _metric("Market implied", DashboardState.implied),
                                    rx.box(
                                        rx.text("Net edge", class_name="metric-label"),
                                        rx.text(
                                            DashboardState.edge,
                                            class_name="metric-value edge-value",
                                        ),
                                        class_name="metric",
                                    ),
                                    _metric("Required edge", DashboardState.required_edge),
                                    class_name="model-metrics",
                                ),
                                rx.box(
                                    rx.text(
                                        DashboardState.decision,
                                        class_name=rx.cond(
                                            DashboardState.decision_tone == "positive",
                                            "decision-label positive",
                                            rx.cond(
                                                DashboardState.decision_tone == "danger",
                                                "decision-label danger",
                                                rx.cond(
                                                    DashboardState.decision_tone == "warning",
                                                    "decision-label warning",
                                                    "decision-label",
                                                ),
                                            ),
                                        ),
                                    ),
                                    rx.text(
                                        DashboardState.decision_reason,
                                        class_name="decision-reason",
                                    ),
                                    class_name="decision-block",
                                ),
                                class_name="model-block",
                            ),
                            rx.box(
                                _metric(
                                    "Composite index", DashboardState.spot, large=True
                                ),
                                rx.hstack(
                                    _metric("Strike", DashboardState.strike),
                                    _metric("Expires", DashboardState.expires),
                                    _metric("Volatility", DashboardState.volatility),
                                    class_name="market-metrics",
                                ),
                                class_name="spot-block",
                            ),
                            class_name="market-overview",
                        ),
                        rx.box(
                            rx.box(
                                _chart_header(
                                    "Model vs market",
                                    DashboardState.probability,
                                    DashboardState.implied,
                                ),
                                _chart(
                                    DashboardState.model_history,
                                    [
                                        rx.recharts.line(
                                            data_key="model",
                                            name="Model",
                                            stroke="#55c2b1",
                                            stroke_width=2,
                                            dot=False,
                                            type_="monotone",
                                        ),
                                        rx.recharts.line(
                                            data_key="market",
                                            name="Market",
                                            stroke="#d8ad63",
                                            stroke_width=2,
                                            stroke_dasharray="6 4",
                                            dot=False,
                                            type_="monotone",
                                        ),
                                    ],
                                    domain=[0, 100],
                                    unit="%",
                                ),
                                class_name="chart",
                            ),
                            rx.box(
                                _chart_header(
                                    "Index vs strike",
                                    DashboardState.spot,
                                    DashboardState.strike,
                                ),
                                _chart(
                                    DashboardState.price_history,
                                    [
                                        rx.recharts.line(
                                            data_key="spot",
                                            name="Index",
                                            stroke="#d4dae1",
                                            stroke_width=2,
                                            dot=False,
                                            type_="monotone",
                                        ),
                                        rx.recharts.line(
                                            data_key="average",
                                            name="Settlement average",
                                            stroke="#d8ad63",
                                            stroke_width=2,
                                            dot=False,
                                            type_="monotone",
                                        ),
                                        rx.recharts.line(
                                            data_key="strike",
                                            name="Strike",
                                            stroke="#7f8a96",
                                            stroke_width=1,
                                            stroke_dasharray="4 4",
                                            dot=False,
                                            type_="linear",
                                        ),
                                    ],
                                    unit="$",
                                ),
                                class_name="chart",
                            ),
                            rx.box(
                                _chart_header(
                                    "Realized volatility", DashboardState.volatility
                                ),
                                _chart(
                                    DashboardState.vol_history,
                                    [
                                        rx.recharts.line(
                                            data_key="volatility",
                                            name="Realized",
                                            stroke="#91a7b3",
                                            stroke_width=2,
                                            dot=False,
                                            type_="monotone",
                                        )
                                    ],
                                    unit="%",
                                ),
                                class_name="chart",
                            ),
                            class_name="charts",
                        ),
                        class_name="surface market-panel",
                    ),
                    rx.box(
                        _panel_header("Orderbook"),
                        rx.box(
                            _book(
                                "YES",
                                DashboardState.yes_book,
                                DashboardState.yes_quote,
                                "yes",
                            ),
                            _book(
                                "NO",
                                DashboardState.no_book,
                                DashboardState.no_quote,
                                "no",
                            ),
                            class_name="book-grid",
                        ),
                        class_name="surface book-panel desktop-book",
                    ),
                    rx.el.details(
                        rx.el.summary(
                            rx.hstack(
                                rx.box(
                                    rx.heading("Orderbook", as_="h2"),
                                    rx.text(
                                        "YES ",
                                        DashboardState.yes_quote["bid"],
                                        " / ",
                                        DashboardState.yes_quote["ask"],
                                        " · NO ",
                                        DashboardState.no_quote["bid"],
                                        " / ",
                                        DashboardState.no_quote["ask"],
                                        class_name="status-note book-inline-quote",
                                    ),
                                ),
                                rx.text("Depth", class_name="summary-action"),
                                class_name="controls-summary",
                            )
                        ),
                        rx.box(
                            _book(
                                "YES",
                                DashboardState.yes_book,
                                DashboardState.yes_quote,
                                "yes",
                            ),
                            _book(
                                "NO",
                                DashboardState.no_book,
                                DashboardState.no_quote,
                                "no",
                            ),
                            class_name="book-grid mobile-book-grid",
                        ),
                        class_name="surface book-panel mobile-book",
                    ),
                    class_name="workspace",
                ),
                rx.el.details(
                    rx.el.summary(
                        rx.hstack(
                            rx.box(
                                rx.heading("Operator Controls", as_="h2"),
                                rx.text(
                                    "Entry, risk, and model parameters",
                                    class_name="status-note",
                                ),
                            ),
                            rx.text("Configure", class_name="summary-action"),
                            class_name="controls-summary",
                        )
                    ),
                    rx.box(
                        rx.box(
                            _control_group(
                                "Entry",
                                _field(
                                    "Minimum net edge (¢)",
                                    DashboardState.min_edge,
                                    DashboardState.set_min_edge,
                                    step="0.5",
                                    min="0.5",
                                    max="25",
                                ),
                                _field(
                                    "Locked-outcome edge (¢)",
                                    DashboardState.deterministic_edge,
                                    DashboardState.set_deterministic_edge,
                                    step="0.1",
                                    min="0",
                                    max="5",
                                ),
                                _field(
                                    "Contracts per order",
                                    DashboardState.max_order,
                                    DashboardState.set_max_order,
                                    step="1",
                                    min="1",
                                    max="25",
                                ),
                                _field(
                                    "Re-entry cooldown (s)",
                                    DashboardState.cooldown,
                                    DashboardState.set_cooldown,
                                    step="1",
                                    min="1",
                                    max="900",
                                ),
                                _field(
                                    "IOC tolerance (ticks)",
                                    DashboardState.slippage,
                                    DashboardState.set_slippage,
                                    step="1",
                                    min="0",
                                    max="5",
                                ),
                            ),
                            _control_group(
                                "Risk",
                                _field(
                                    "Kelly fraction",
                                    DashboardState.kelly_fraction,
                                    DashboardState.set_kelly_fraction,
                                    step="0.01",
                                    min="0.01",
                                    max="0.5",
                                ),
                                _field(
                                    "Bankroll cap per market (%)",
                                    DashboardState.position_fraction,
                                    DashboardState.set_position_fraction,
                                    step="0.5",
                                    min="0.5",
                                    max="25",
                                ),
                                _field(
                                    "Absolute position cap ($)",
                                    DashboardState.max_position,
                                    DashboardState.set_max_position,
                                    step="1",
                                    min="1",
                                    max="50",
                                ),
                                _field(
                                    "Daily loss cap ($)",
                                    DashboardState.max_daily_loss,
                                    DashboardState.set_max_daily_loss,
                                    step="1",
                                    min="1",
                                    max="100",
                                ),
                                _field(
                                    "Reserve cash ($)",
                                    DashboardState.cash_buffer,
                                    DashboardState.set_cash_buffer,
                                    step="1",
                                    min="0",
                                ),
                            ),
                            _control_group(
                                "Model",
                                rx.el.label(
                                    rx.text("Volatility source"),
                                    rx.el.select(
                                        rx.el.option("Realized", value="realized"),
                                        rx.el.option("Override", value="override"),
                                        value=DashboardState.vol_source,
                                        on_change=DashboardState.set_vol_source,
                                    ),
                                    class_name="field",
                                ),
                                rx.cond(
                                    DashboardState.vol_source == "override",
                                    _field(
                                        "Override volatility",
                                        DashboardState.vol_override,
                                        DashboardState.set_vol_override,
                                        step="0.01",
                                        min="0.01",
                                        max="5",
                                        placeholder="0.45",
                                    ),
                                ),
                                _field(
                                    "Volatility adjustment",
                                    DashboardState.vol_scale,
                                    DashboardState.set_vol_scale,
                                    step="0.05",
                                    min="0.5",
                                    max="2",
                                ),
                            ),
                            class_name="control-grid",
                        ),
                        rx.hstack(
                            rx.button(
                                "Apply settings",
                                on_click=DashboardState.save_settings,
                                class_name="button primary",
                            ),
                            rx.button(
                                "Reset",
                                on_click=DashboardState.reset_settings,
                                class_name="button",
                            ),
                            rx.text(
                                DashboardState.settings_status, class_name="status-note"
                            ),
                            class_name="button-row",
                        ),
                        class_name="risk-controls",
                    ),
                    class_name="surface controls-panel",
                ),
                class_name="dashboard",
            ),
            class_name="shell",
        ),
        class_name="page",
    )


@asynccontextmanager
async def runtime_lifespan():
    validate_auth_or_exit()
    task = asyncio.create_task(run_background_services())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = rx.App(stylesheets=["/dashboard.css"])
app.add_page(index, route="/", title="Autotrader", on_load=DashboardState.load)
app.register_lifespan_task(runtime_lifespan)
